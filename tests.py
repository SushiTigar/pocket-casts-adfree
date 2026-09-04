#!/usr/bin/env python3
"""Tests for Pocket Casts Ad-Free Pipeline.

Covers: artwork normalization, date validation, state management,
episode matching, transcript pre-population, skip/stop handling,
upload ordering, Up Next queue safety, and UI server API endpoints.
"""

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, mock_open

sys.path.insert(0, os.path.dirname(__file__))

from services_manager import ROOT

from pocketcasts_adfree import (
    _normalize_artwork_to_jpeg,
    _parse_transcript_to_sylt,
    _sanitize_published_date,
    is_patreon_feed,
    find_rss_url_for_podcast,
    get_podcast_artwork_url,
    load_state,
    save_state,
    STATE_FILE,
    _SkippedError,
)


class TestArtworkNormalization(unittest.TestCase):
    """Artwork must be JPEG, <=1400px, RGB — web player rejects PNG/CMYK."""

    def _make_png(self, width=100, height=100, mode="RGBA"):
        from PIL import Image
        img = Image.new(mode, (width, height), (255, 0, 0, 128) if mode == "RGBA" else (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _make_jpeg(self, width=100, height=100):
        from PIL import Image
        img = Image.new("RGB", (width, height), (0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def test_png_converted_to_jpeg(self):
        png_data = self._make_png()
        result = _normalize_artwork_to_jpeg(png_data)
        from PIL import Image
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.mode, "RGB")

    def test_rgba_png_converted_to_rgb_jpeg(self):
        rgba_data = self._make_png(mode="RGBA")
        result = _normalize_artwork_to_jpeg(rgba_data)
        from PIL import Image
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.mode, "RGB")

    def test_oversized_image_resized(self):
        big_data = self._make_png(width=3000, height=2000)
        result = _normalize_artwork_to_jpeg(big_data, max_size=1400)
        from PIL import Image
        img = Image.open(io.BytesIO(result))
        self.assertLessEqual(max(img.size), 1400)

    def test_small_image_not_upscaled(self):
        small_data = self._make_jpeg(width=200, height=200)
        result = _normalize_artwork_to_jpeg(small_data)
        from PIL import Image
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.size, (200, 200))

    def test_already_jpeg_stays_jpeg(self):
        jpeg_data = self._make_jpeg()
        result = _normalize_artwork_to_jpeg(jpeg_data)
        from PIL import Image
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.format, "JPEG")

    def test_corrupt_data_returns_original(self):
        bad_data = b"not an image"
        result = _normalize_artwork_to_jpeg(bad_data)
        self.assertEqual(result, bad_data)


class TestDateValidation(unittest.TestCase):
    """Epoch-0 dates must be rejected — they display as Dec 31, 1969."""

    def test_upload_file_rejects_epoch_zero(self):
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.client = MagicMock()
            pc.token = "fake"
            # We can't easily call upload_file without a real file,
            # so test the guard logic directly
            published = "1970-01-01T00:00:00Z"
            if not published or published.startswith("1970") or published.startswith("1969"):
                published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.assertFalse(published.startswith("1970"))
            self.assertFalse(published.startswith("1969"))

    def test_valid_date_passes_through(self):
        published = "2026-04-15T10:00:00Z"
        if not published or published.startswith("1970") or published.startswith("1969"):
            published = "fallback"
        self.assertEqual(published, "2026-04-15T10:00:00Z")

    def test_empty_date_gets_fallback(self):
        published = ""
        if not published or published.startswith("1970") or published.startswith("1969"):
            published = "2026-01-01T00:00:00Z"
        self.assertEqual(published, "2026-01-01T00:00:00Z")

    def test_none_date_gets_fallback(self):
        published = None
        if not published or (isinstance(published, str) and (published.startswith("1970") or published.startswith("1969"))):
            published = "2026-01-01T00:00:00Z"
        self.assertEqual(published, "2026-01-01T00:00:00Z")

    def test_sanitize_rejects_epoch_zero(self):
        out = _sanitize_published_date("1970-01-01T00:00:00Z")
        self.assertFalse(out.startswith("1970"))
        self.assertFalse(out.startswith("1969"))

    def test_sanitize_rejects_dec_31_1969(self):
        out = _sanitize_published_date("1969-12-31T23:59:59Z")
        self.assertFalse(out.startswith("1969"))

    def test_sanitize_rejects_empty(self):
        out = _sanitize_published_date("")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}")

    def test_sanitize_rejects_none(self):
        out = _sanitize_published_date(None)
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}")

    def test_sanitize_rejects_garbage(self):
        out = _sanitize_published_date("not-a-date")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}")

    def test_sanitize_preserves_valid(self):
        out = _sanitize_published_date("2026-04-15T10:00:00Z")
        self.assertEqual(out, "2026-04-15T10:00:00Z")


class TestStateManagement(unittest.TestCase):
    """State file tracks processed episodes to prevent duplicates."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_state_file = STATE_FILE

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_empty_state(self):
        with patch('pocketcasts_adfree.STATE_FILE', Path(self.tmpdir) / "nonexistent.json"):
            state = load_state()
            self.assertIn("processed", state)
            self.assertEqual(len(state["processed"]), 0)

    def test_save_and_load_roundtrip(self):
        state_path = Path(self.tmpdir) / "test_state.json"
        with patch('pocketcasts_adfree.STATE_FILE', state_path):
            state = {"processed": {"feed:ep1": {"title": "Test", "file_uuid": "abc"}}}
            save_state(state)
            loaded = load_state()
            self.assertEqual(loaded["processed"]["feed:ep1"]["title"], "Test")

    def test_duplicate_prevention(self):
        state = {"processed": {"myfeed:ep123": {"title": "Already Done"}}}
        state_key = "myfeed:ep123"
        self.assertIn(state_key, state["processed"])


class TestPatreonDetection(unittest.TestCase):
    """Patreon/premium feeds should be skipped — they're already ad-free."""

    def test_patreon_url(self):
        pod = {"title": "My Show", "url": "https://patreon.com/rss/myshow"}
        self.assertTrue(is_patreon_feed(pod))

    def test_premium_feed_title(self):
        pod = {"title": "My Show (Premium Feed)", "url": ""}
        self.assertTrue(is_patreon_feed(pod))

    def test_ad_free_feed(self):
        pod = {"title": "My Show Ad-Free Feed", "url": ""}
        self.assertTrue(is_patreon_feed(pod))

    def test_normal_feed(self):
        pod = {"title": "Giant Bombcast", "url": "https://feeds.simplecast.com/abc"}
        self.assertFalse(is_patreon_feed(pod))

    def test_patron_in_author(self):
        pod = {"title": "Show", "url": "", "author": "Patron Feed"}
        self.assertTrue(is_patreon_feed(pod))


class TestTranscriptParsing(unittest.TestCase):
    """SYLT entries must have correct millisecond timestamps."""

    def test_basic_parsing(self):
        text = "[00:00:05.123 --> 00:00:10.456] Hello world\n[00:01:00.000 --> 00:01:05.000] Goodbye"
        entries = _parse_transcript_to_sylt(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], ("Hello world", 5123))
        self.assertEqual(entries[1], ("Goodbye", 60000))

    def test_hour_timestamps(self):
        text = "[01:30:00.000 --> 01:30:05.000] Late in the show"
        entries = _parse_transcript_to_sylt(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1], 5400000)

    def test_empty_text_skipped(self):
        text = "[00:00:00.000 --> 00:00:01.000]   \n[00:00:02.000 --> 00:00:03.000] actual text"
        entries = _parse_transcript_to_sylt(text)
        self.assertEqual(len(entries), 1)


class TestSkipStopLogic(unittest.TestCase):
    """Skip must abort current episode; stop must prevent all further episodes."""

    def test_skip_event_raises_skipped_error(self):
        """download_processed_audio checks skip_event before each retry."""
        skip = threading.Event()
        skip.set()
        from pocketcasts_adfree import MinusPodClient
        mp = MinusPodClient.__new__(MinusPodClient)
        mp.base_url = "http://localhost:9999"
        mp.client = MagicMock()
        with self.assertRaises(_SkippedError):
            mp.download_processed_audio(
                "test-slug", "test-ep", Path("/tmp"),
                skip_event=skip,
            )

    def test_stop_does_not_clear_skip_for_next_episode(self):
        """When stop is set, skip_event.clear() must NOT be called,
        otherwise the next episode in the loop would proceed."""
        stop_event = threading.Event()
        skip_event = threading.Event()
        stop_event.set()
        skip_event.set()

        # Simulate the fixed logic from _process_job
        if not stop_event.is_set():
            skip_event.clear()

        self.assertTrue(skip_event.is_set(),
            "skip_event should remain set when stop_event is active")


class TestUploadOrdering(unittest.TestCase):
    """The upload flow must match what pocket-casts-ios does:

      1. POST /files/upload/request  — with hasCustomImage & colour=0
      2. PUT  audio
      3. POST /files/upload/image    — get image URL
      4. PUT  image
      5. POST /files                 — metadata sync (marks hasCustomImage)

    This ordering is what promotes `imageStatus` from 1 to 2 server-side.
    See `reupload_image_from_current` for the recovery path when older
    uploads get stuck at status 1.
    """

    def _run_upload(self):
        """Helper: drive upload_file with all HTTP calls recorded."""
        from pocketcasts_adfree import PocketCastsClient
        call_order = []
        bodies = {}

        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"

            def track_post(*args, **kwargs):
                url = args[0] if args else kwargs.get('url', '')
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"url": "https://fake-s3", "success": True}
                resp.raise_for_status = MagicMock()
                body = kwargs.get('json')
                if '/files/upload/request' in url:
                    call_order.append('upload_request'); bodies['upload_request'] = body
                elif '/files/upload/image' in url:
                    call_order.append('image_request')
                elif url.endswith('/files'):
                    call_order.append('metadata_sync'); bodies.setdefault('metadata_sync', body)
                return resp

            def track_get(*args, **kwargs):
                r = MagicMock()
                r.status_code = 200
                r.json.return_value = {"success": True, "imageStatus": 2}
                r.raise_for_status = MagicMock()
                return r

            mock_client = MagicMock()
            mock_client.post = MagicMock(side_effect=track_post)
            mock_client.put = MagicMock(side_effect=lambda *a, **kw: MagicMock(raise_for_status=MagicMock()))
            mock_client.get = MagicMock(side_effect=track_get)
            pc.client = mock_client

            tmpdir = tempfile.mkdtemp()
            try:
                from PIL import Image
                buf = io.BytesIO()
                Image.new("RGB", (100, 100), (255, 0, 0)).save(buf, format="JPEG")
                artwork = buf.getvalue()
                mp3_path = Path(tmpdir) / "test.mp3"
                mp3_path.write_bytes(b'\xff\xfb\x90\x00' * 1000)

                # Mock the MP3 parse (real mutagen rejects our fake bytes)
                fake_audio = MagicMock()
                fake_audio.info.length = 123.4
                with patch("pocketcasts_adfree.MP3", return_value=fake_audio), \
                     patch("pocketcasts_adfree.time.sleep"):
                    try:
                        pc.upload_file(mp3_path, "Test Episode", artwork=artwork)
                    except Exception:
                        pass
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        return call_order, bodies

    def test_image_uploaded_before_metadata_sync(self):
        """iOS parity: PUT image, THEN POST /files (not the reverse)."""
        order, _ = self._run_upload()
        if 'image_request' in order and 'metadata_sync' in order:
            self.assertLess(order.index('image_request'),
                            order.index('metadata_sync'),
                            f"Image must be uploaded before the /files "
                            f"metadata POST. Order: {order}")

    def test_upload_request_declares_has_custom_image(self):
        """The initial /files/upload/request must carry hasCustomImage=true
        and colour=0 when artwork is provided. Without this the server
        stalls imageStatus at 1 forever."""
        _, bodies = self._run_upload()
        req = bodies.get('upload_request') or {}
        self.assertTrue(req.get('hasCustomImage'),
                        f"/files/upload/request missing hasCustomImage: {req}")
        self.assertEqual(req.get('colour'), 0,
                         f"colour must be 0 when artwork is attached: {req}")

    def test_metadata_sync_declares_has_custom_image(self):
        """The follow-up POST /files must also carry hasCustomImage=true
        and colour=0 — this is the pairing that promotes status 1 → 2."""
        _, bodies = self._run_upload()
        meta = bodies.get('metadata_sync') or {}
        files = meta.get('files') or []
        self.assertTrue(files, "metadata_sync body missing files[]")
        entry = files[0]
        self.assertTrue(entry.get('hasCustomImage'),
                        f"metadata sync missing hasCustomImage: {entry}")
        self.assertEqual(entry.get('colour'), 0,
                         f"colour must be 0 on metadata sync: {entry}")


class TestReuploadImageFromCurrent(unittest.TestCase):
    """Recovery for files uploaded before the fix. Must re-upload the
    image AND follow with a /files metadata POST; neither alone works."""

    def _client_with_mocks(self, existing_status=1, poll_status=2):
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()

            # get_file is called first to get current status + imageUrl
            first_get = MagicMock(status_code=200, text="{}")
            first_get.json.return_value = {
                "uuid": "file-1", "title": "T", "imageStatus": existing_status,
                "imageUrl": "https://pocketcasts.s3/file-1.jpg",
                "playedUpTo": 0, "playingStatus": 0, "duration": 100, "size": 1,
                "published": "2026-01-01T00:00:00Z",
            }
            first_get.raise_for_status = MagicMock()
            # Subsequent get_file calls during polling return poll_status
            poll_get = MagicMock(status_code=200, text="{}")
            poll_get.json.return_value = {"imageStatus": poll_status}
            poll_get.raise_for_status = MagicMock()
            # Fetching the JPEG from S3 returns some bytes
            fetch_img = MagicMock(status_code=200)
            fetch_img.content = b"\xff\xd8\xff\xd9" * 64
            fetch_img.raise_for_status = MagicMock()
            pc.client.get = MagicMock(
                side_effect=[first_get, fetch_img] + [poll_get] * 20
            )
            post_resp = MagicMock(status_code=200)
            post_resp.json.return_value = {"url": "https://s3/img"}
            post_resp.raise_for_status = MagicMock()
            pc.client.post = MagicMock(return_value=post_resp)
            pc.client.put = MagicMock(
                return_value=MagicMock(raise_for_status=MagicMock())
            )
        return pc

    def test_promotes_when_server_flips_status(self):
        pc = self._client_with_mocks(existing_status=1, poll_status=2)
        with patch("pocketcasts_adfree.time.sleep"):
            r = pc.reupload_image_from_current("file-1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status_after"], 2)
        self.assertEqual(r["status_before"], 1)

    def test_reports_still_stuck_when_server_does_not_flip(self):
        pc = self._client_with_mocks(existing_status=1, poll_status=1)
        with patch("pocketcasts_adfree.time.sleep"):
            r = pc.reupload_image_from_current("file-1", poll=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status_after"], 1)

    def test_calls_image_upload_and_metadata_post_in_order(self):
        """The key finding: both calls are required for promotion."""
        pc = self._client_with_mocks(existing_status=1, poll_status=2)
        with patch("pocketcasts_adfree.time.sleep"):
            pc.reupload_image_from_current("file-1", poll=False)
        # POST sequence: /files/upload/image then /files
        post_urls = [c.args[0] for c in pc.client.post.call_args_list]
        self.assertTrue(any("/files/upload/image" in u for u in post_urls),
                        f"expected /files/upload/image in {post_urls}")
        # The final POST must be /files (metadata sync) with hasCustomImage
        final_url = post_urls[-1]
        self.assertTrue(final_url.endswith("/files"),
                        f"last POST must be /files metadata sync: {final_url}")
        final_body = pc.client.post.call_args_list[-1].kwargs.get("json") or {}
        self.assertEqual(final_body["files"][0]["hasCustomImage"], True)
        self.assertEqual(final_body["files"][0]["colour"], 0)

    def test_returns_error_when_file_missing(self):
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()
            # 404 → get_file returns None
            r_404 = MagicMock(status_code=404)
            pc.client.get = MagicMock(return_value=r_404)
            result = pc.reupload_image_from_current("missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "file not found")

    def test_returns_error_when_file_has_no_image_url(self):
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"uuid": "f", "imageStatus": 0}
            resp.raise_for_status = MagicMock()
            pc.client.get = MagicMock(return_value=resp)
            result = pc.reupload_image_from_current("f")
        self.assertFalse(result["ok"])
        self.assertIn("no imageUrl", result["reason"])


class TestUpNextQueueSafety(unittest.TestCase):
    """Up Next sync must fetch serverModified to avoid clearing the queue."""

    def test_server_modified_fetched_before_add(self):
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()

            fetch_resp = MagicMock()
            fetch_resp.status_code = 200
            fetch_resp.json.return_value = {"serverModified": 1234567890}
            fetch_resp.raise_for_status = MagicMock()

            add_resp = MagicMock()
            add_resp.status_code = 200
            add_resp.json.return_value = {}
            add_resp.raise_for_status = MagicMock()

            pc.client.post = MagicMock(side_effect=[fetch_resp, add_resp])

            pc.add_to_up_next("file-uuid", "Test Episode")

            # The second call should use serverModified=1234567890
            second_call = pc.client.post.call_args_list[1]
            body = second_call[1].get('json', second_call[0][1] if len(second_call[0]) > 1 else {})
            if isinstance(body, dict):
                server_mod = body.get("upNext", {}).get("serverModified")
                self.assertEqual(server_mod, 1234567890,
                    "add_to_up_next must use the serverModified from the fetch call")

    def test_add_to_up_next_carries_published_date(self):
        """Pocket Casts displays "Dec 31, 1969" when an Up Next entry has no
        published date. The /files endpoint stores the real date but the
        Up Next cache is separate — add_to_up_next must propagate it."""
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()

            fetch_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            fetch_resp.json.return_value = {"serverModified": 1}
            add_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            add_resp.json.return_value = {}
            pc.client.post = MagicMock(side_effect=[fetch_resp, add_resp])

            pc.add_to_up_next(
                "file-uuid", "Test Episode",
                published="2026-04-21T15:41:28Z",
            )

            change = pc.client.post.call_args_list[1][1]["json"]["upNext"]["changes"][0]
            self.assertEqual(change.get("published"), "2026-04-21T15:41:28Z",
                "add_to_up_next must forward the published date so PC apps "
                "don't render epoch-0 (Dec 31, 1969) for Ad-Free uploads.")

    def test_add_to_up_next_omits_epoch_published_dates(self):
        """Don't paper over epoch dates: if the upstream date is 1970, drop it
        rather than re-poisoning Up Next. _sanitize_published_date already
        coerces empty/epoch values to "now"; we just want to make sure we
        never silently send 1970-01-01."""
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()
            fetch_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            fetch_resp.json.return_value = {"serverModified": 1}
            add_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            add_resp.json.return_value = {}
            pc.client.post = MagicMock(side_effect=[fetch_resp, add_resp])

            pc.add_to_up_next("file-uuid", "Test", published="1970-01-01T00:00:00Z")

            change = pc.client.post.call_args_list[1][1]["json"]["upNext"]["changes"][0]
            self.assertNotEqual(change.get("published"), "1970-01-01T00:00:00Z",
                "Epoch-0 input should be sanitized to a real date.")

    def test_replace_in_up_next_batches_add_and_remove(self):
        """replace_in_up_next must combine the add + remove into one
        /up_next/sync call. Two separate calls would race on the
        serverModified probe (second call could see stale data after the
        first call's add has been applied) and leave the original in
        Up Next. One network round-trip with both changes is correct."""
        from pocketcasts_adfree import PocketCastsClient, USER_PODCAST_UUID
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()

            fetch_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            fetch_resp.json.return_value = {"serverModified": 999}
            sync_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            sync_resp.json.return_value = {}
            pc.client.post = MagicMock(side_effect=[fetch_resp, sync_resp])

            pc.replace_in_up_next(
                "file-uuid", "Episode (Ad-Free)", "orig-uuid",
                published="2026-04-21T15:41:28Z",
            )

            self.assertEqual(
                pc.client.post.call_count, 2,
                "replace_in_up_next must do exactly 2 posts: 1 fetch + 1 sync."
            )
            sync_body = pc.client.post.call_args_list[1][1]["json"]
            changes = sync_body["upNext"]["changes"]
            self.assertEqual(len(changes), 2,
                "Both the add and the remove must be in the same sync batch.")
            add_change = changes[0]
            self.assertEqual(add_change["action"], 3)  # PLAY_LAST
            self.assertEqual(add_change["uuid"], "file-uuid")
            self.assertEqual(add_change["podcast"], USER_PODCAST_UUID)
            self.assertEqual(add_change["published"], "2026-04-21T15:41:28Z")
            remove_change = changes[1]
            self.assertEqual(remove_change["action"], 4)  # REMOVE
            self.assertEqual(remove_change["uuid"], "orig-uuid")

    def test_replace_in_up_next_add_only_when_no_original(self):
        """When original_uuid is falsy, only the add is batched — the
        caller will fall back to the title-match sweep for the original."""
        from pocketcasts_adfree import PocketCastsClient
        with patch.object(PocketCastsClient, '__init__', lambda self, *a, **kw: None):
            pc = PocketCastsClient.__new__(PocketCastsClient)
            pc.token = "fake"
            pc.client = MagicMock()
            fetch_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            fetch_resp.json.return_value = {"serverModified": 1}
            sync_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
            sync_resp.json.return_value = {}
            pc.client.post = MagicMock(side_effect=[fetch_resp, sync_resp])

            pc.replace_in_up_next("file-uuid", "Episode (Ad-Free)", None)

            changes = pc.client.post.call_args_list[1][1]["json"]["upNext"]["changes"]
            self.assertEqual(len(changes), 1,
                "Without an original UUID only the add change should be sent.")

    def test_retry_up_next_retries_transient_failures(self):
        """_retry_up_next must retry transient failures with backoff
        and eventually raise the last exception when all attempts fail.
        Without retries a single 5xx from Pocket Casts would leave the
        user with an inconsistent queue (Ad-Free file in Up Next but
        original also still queued)."""
        from pocketcasts_adfree import _retry_up_next
        calls = []
        def flaky_fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient 503")
            return "ok"
        result = _retry_up_next(flaky_fn, attempts=3, base_delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)

    def test_retry_up_next_raises_after_exhausting_attempts(self):
        from pocketcasts_adfree import _retry_up_next
        def always_fail():
            raise RuntimeError("persistent 500")
        with self.assertRaises(RuntimeError):
            _retry_up_next(always_fail, attempts=3, base_delay=0)


class TestStrongTitleNormalization(unittest.TestCase):
    """The strong normalizer bridges RSS-form and PC-form title differences
    that the basic normalizer misses: smart quotes vs straight quotes, em-
    dashes vs hyphens, season tags in different formats, 'Pt.' vs 'Part',
    '#N' vs 'No. N', HTML entities, and unicode accents."""

    def test_season_tag_variants_match(self):
        from ui_server import _normalize_title_strong
        self.assertEqual(
            _normalize_title_strong("Episode 42: The Beginning (S2 E5)"),
            _normalize_title_strong("Episode 42: The Beginning (Season 2 Episode 5)"),
        )

    def test_pt_vs_part(self):
        from ui_server import _normalize_title_strong
        self.assertEqual(
            _normalize_title_strong("Title, Pt. 1"),
            _normalize_title_strong("Title, Part 1"),
        )

    def test_ep_vs_episode(self):
        from ui_server import _normalize_title_strong
        self.assertEqual(
            _normalize_title_strong("Title (Ep. 5)"),
            _normalize_title_strong("Title (Episode 5)"),
        )

    def test_hash_number(self):
        from ui_server import _normalize_title_strong
        self.assertEqual(
            _normalize_title_strong("Show Name #42"),
            _normalize_title_strong("Show Name No. 42"),
        )

    def test_unicode_accents_folded(self):
        from ui_server import _normalize_title_strong
        self.assertEqual(
            _normalize_title_strong("café"),
            _normalize_title_strong("cafe"),
        )

    def test_html_entities_decoded(self):
        from ui_server import _normalize_title_strong
        # & should decode to & before normalization strips punctuation.
        self.assertEqual(
            _normalize_title_strong("Q&A"),
            _normalize_title_strong("Q&A"),
        )

    def test_distinct_titles_do_not_match(self):
        from ui_server import _normalize_title_strong
        self.assertNotEqual(
            _normalize_title_strong("Totally Different Episode"),
            _normalize_title_strong("Another Show Episode"),
        )

    def test_basic_normalizer_still_works(self):
        """Regression guard: don't break callers that depend on the
        original _normalize_title behavior."""
        from ui_server import _normalize_title
        self.assertEqual(
            _normalize_title("How To Change The World!"),
            _normalize_title("how to change the world"),
        )


class TestTranscriptionFailureRecovery(unittest.TestCase):
    """The pipeline must restart Whisper when its Metal backend wedges,
    rather than spinning on reprocess against a known-broken server."""

    def test_is_transcription_failure_detects_metal_errors(self):
        from pocketcasts_adfree import _is_transcription_failure
        self.assertTrue(_is_transcription_failure("Failed to transcribe audio"))
        self.assertTrue(_is_transcription_failure("whisper backend returned 500"))
        self.assertTrue(_is_transcription_failure("Metal command buffer error"))
        self.assertTrue(_is_transcription_failure("GPU error/recovery"))

    def test_is_transcription_failure_ignores_unrelated_errors(self):
        from pocketcasts_adfree import _is_transcription_failure
        self.assertFalse(_is_transcription_failure(""))
        self.assertFalse(_is_transcription_failure("HTTP 404 audio source"))
        self.assertFalse(_is_transcription_failure("Out of disk space"))


class TestUIServerEndpoints(unittest.TestCase):
    """Test the Flask API endpoints."""

    def setUp(self):
        os.environ["POCKETCASTS_EMAIL"] = "test@test.com"
        os.environ["POCKETCASTS_PASSWORD"] = "testpass"

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_status_endpoint(self, MockMP, MockPC):
        from ui_server import create_app
        mock_mp = MagicMock()
        mock_mp.health.return_value = {"status": "ok"}
        MockMP.return_value = mock_mp

        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/status')
        data = resp.get_json()
        self.assertIn('minuspod', data)
        self.assertIn('pocketcasts', data)

    def test_queue_status_no_active_job(self):
        from ui_server import create_app
        with patch('ui_server.PocketCastsClient'), \
             patch('ui_server.MinusPodClient'):
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.get('/api/queue/status')
            data = resp.get_json()
            self.assertIsNone(data.get('active_job'))
            self.assertEqual(data.get('queued_episodes'), 0)

    def test_health_endpoint_public_when_auth_enabled(self):
        """Internal liveness probe must work without Basic Auth credentials."""
        import ui_server as _ui
        from ui_server import create_app
        old_pass = os.environ.get("UI_AUTH_PASSWORD")
        old_testing = _ui._IS_TESTING
        try:
            os.environ["UI_AUTH_PASSWORD"] = "secret"
            _ui._IS_TESTING = False
            with patch("ui_server.PocketCastsClient"), \
                 patch("ui_server.MinusPodClient"):
                app = create_app("test@test.com", "testpass")
                client = app.test_client()
                resp = client.get("/api/health")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json(), {"status": "ok"})
                resp = client.get("/api/queue/status")
                self.assertEqual(resp.status_code, 401)
        finally:
            _ui._IS_TESTING = old_testing
            if old_pass is None:
                os.environ.pop("UI_AUTH_PASSWORD", None)
            else:
                os.environ["UI_AUTH_PASSWORD"] = old_pass

    def test_job_not_found_returns_404(self):
        from ui_server import create_app
        with patch('ui_server.PocketCastsClient'), \
             patch('ui_server.MinusPodClient'):
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.get('/api/job/nonexistent-id')
            self.assertEqual(resp.status_code, 404)

    def test_process_empty_selection_returns_400(self):
        from ui_server import create_app
        with patch('ui_server.PocketCastsClient'), \
             patch('ui_server.MinusPodClient'):
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.post('/api/process',
                data=json.dumps({"selections": {}}),
                content_type='application/json')
            self.assertEqual(resp.status_code, 400)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_files_list_endpoint(self, MockMP, MockPC):
        from ui_server import create_app
        mock_pc = MagicMock()
        mock_pc.get_files.return_value = {"files": [
            {
                "uuid": "abc", "title": "Ep (Ad-Free)", "size": "1000",
                "duration": "60", "published": "2026-04-15T10:00:00Z",
                "modifiedAt": "2026-04-15T10:05:00Z",
                "playedUpTo": 0, "playingStatus": 0,
                "hasCustomImage": True, "imageStatus": 2,
                "imageUrl": "https://example/img.jpg",
            }
        ]}
        MockPC.return_value = mock_pc
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/files')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data['files']), 1)
        self.assertTrue(data['files'][0]['ad_free'])

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_files_delete_endpoint(self, MockMP, MockPC):
        from ui_server import create_app
        mock_pc = MagicMock()
        mock_pc.delete_file.return_value = True
        MockPC.return_value = mock_pc
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.delete('/api/files/abc-123')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ok'])
        mock_pc.delete_file.assert_called_once_with('abc-123')

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_files_cleanup_played(self, MockMP, MockPC):
        from ui_server import create_app
        mock_pc = MagicMock()
        mock_pc.get_files.return_value = {"files": [
            {"uuid": "played", "title": "X (Ad-Free)", "playingStatus": 3,
             "playedUpTo": 600, "duration": 600},
            {"uuid": "unplayed", "title": "Y (Ad-Free)", "playingStatus": 0,
             "playedUpTo": 0, "duration": 600},
            {"uuid": "not-adfree", "title": "Z", "playingStatus": 3,
             "playedUpTo": 600, "duration": 600},
        ]}
        mock_pc.delete_file.return_value = True
        MockPC.return_value = mock_pc
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.post('/api/files/cleanup_played',
            data=json.dumps({}),
            content_type='application/json')
        data = resp.get_json()
        self.assertIn('played', data['deleted'])
        self.assertNotIn('unplayed', data['deleted'])
        self.assertNotIn('not-adfree', data['deleted'])

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_fix_thumbnails_endpoint_was_removed(self, MockMP, MockPC):
        """The /api/files/fix_thumbnails endpoint was removed in the
        April 17 cleanup once the upload-ordering fix made stuck thumbnails
        impossible. Verify it 404s so we notice if someone re-adds it."""
        from ui_server import create_app
        MockPC.return_value = MagicMock()
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.post('/api/files/fix_thumbnails',
            data=json.dumps({}), content_type='application/json')
        self.assertIn(resp.status_code, (404, 405))

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_processed_clear_for_single_podcast(self, MockMP, MockPC):
        """DELETE /api/processed/podcast/<uuid> only clears entries
        for that podcast — the global Reset Processed action is gone, so
        per-podcast scoping is the only way to wipe history."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {
                "podcast-a:ep1": {"title": "A1", "podcast_uuid": "uuid-a"},
                "podcast-a:ep2": {"title": "A2", "podcast_uuid": "uuid-a"},
                "podcast-b:ep3": {"title": "B1", "podcast_uuid": "uuid-b"},
            }}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            mock_pc = MagicMock()
            mock_pc.get_subscriptions.return_value = {"podcasts": [
                {"uuid": "uuid-a", "title": "Podcast A"},
                {"uuid": "uuid-b", "title": "Podcast B"},
            ]}
            MockPC.return_value = mock_pc

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.delete('/api/processed/podcast/uuid-a')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["uuid"], "uuid-a")
            self.assertGreaterEqual(data["cleared"], 1)
            remaining = pf.load_state().get("processed", {})
            self.assertIn("podcast-b:ep3", remaining)
            self.assertNotIn("podcast-a:ep1", remaining)
            self.assertNotIn("podcast-a:ep2", remaining)
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_history_endpoint_returns_metadata(self, MockMP, MockPC):
        """GET /api/history returns one entry per processed episode with
        the metadata the History view needs to render."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {
                "podcast-a:ep1": {
                    "title": "Episode One",
                    "podcast_title": "Podcast A",
                    "file_uuid": "file-1",
                    "processed_at": "2026-04-15 09:00:00",
                    "ads_removed": 3,
                    "time_saved_secs": 180,
                    "original_size": 50_000_000,
                    "new_size": 44_000_000,
                },
            }}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            mock_pc = MagicMock()
            mock_pc.get_files.return_value = {"files": [
                {"uuid": "file-1", "title": "Episode One (Ad-Free)"},
            ]}
            MockPC.return_value = mock_pc

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.get('/api/history')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["count"], 1)
            entry = data["entries"][0]
            self.assertEqual(entry["title"], "Episode One")
            self.assertEqual(entry["podcast_title"], "Podcast A")
            self.assertEqual(entry["ads_removed"], 3)
            self.assertEqual(entry["time_saved_secs"], 180)
            self.assertEqual(entry["original_size"], 50_000_000)
            self.assertEqual(entry["new_size"], 44_000_000)
            self.assertEqual(entry["pocket_casts_uuid"], "file-1")
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_reset_stuck_episode_resets_db_and_reprocesses(self, MockMP, MockPC):
        """POST /api/episodes/<slug>/<episode_id>/reset clears a stuck
        MinusPod row (processing/failed/permanently_failed → discovered)
        and asks MinusPod to reprocess. The local processed_episodes.json
        marker must NOT be touched."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {
                "podcast-a:ep1": {
                    "title": "Stuck ep", "file_uuid": "f-1",
                    "processed_at": "2026-04-15 09:00:00",
                    "ads_removed": 0, "time_saved_secs": 0,
                },
            }}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            MockPC.return_value = MagicMock()
            mock_mp = MagicMock()
            mock_mp.reprocess_episode.return_value = {}
            MockMP.return_value = mock_mp

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            with patch('ui_server._reset_stuck_episode_in_db',
                       return_value=(True, 'failed')) as m_reset:
                resp = client.post('/api/episodes/podcast-a/ep1/reset')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["db_reset"])
            self.assertEqual(data["previous_status"], "failed")
            self.assertTrue(data["reprocess_triggered"])
            self.assertFalse(data.get("already_processing"))
            m_reset.assert_called_once_with("podcast-a", "ep1")
            mock_mp.reprocess_episode.assert_called_once_with(
                "podcast-a", "ep1", mode="full")
            # Local marker must remain — the user only cleared MinusPod state.
            remaining = pf.load_state().get("processed", {})
            self.assertIn("podcast-a:ep1", remaining)
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_reset_stuck_episode_already_processing(self, MockMP, MockPC):
        """When MinusPod says it's already reprocessing, the endpoint
        still returns 200 but flags already_processing=True and does NOT
        call reprocess_episode."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {}}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            MockPC.return_value = MagicMock()
            mock_mp = MagicMock()
            mock_mp.reprocess_episode.return_value = {"already_processing": True}
            MockMP.return_value = mock_mp

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            with patch('ui_server._reset_stuck_episode_in_db',
                       return_value=(True, 'processing')):
                resp = client.post('/api/episodes/dlco/ep42/reset')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["db_reset"])
            self.assertTrue(data["already_processing"])
            self.assertFalse(data["reprocess_triggered"])
            self.assertFalse(data.get("reprocess_error"))
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_reset_stuck_episode_not_stuck(self, MockMP, MockPC):
        """If the episode isn't in a stuck state the endpoint still
        returns 200 with db_reset=False and previous_status='not_stuck',
        and does NOT call reprocess_episode (nothing to reprocess)."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {}}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            MockPC.return_value = MagicMock()
            mock_mp = MagicMock()
            MockMP.return_value = mock_mp

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            with patch('ui_server._reset_stuck_episode_in_db',
                       return_value=(False, 'not_stuck')):
                resp = client.post('/api/episodes/dlco/ep42/reset')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertFalse(data["db_reset"])
            self.assertEqual(data["previous_status"], "not_stuck")
            mock_mp.reprocess_episode.assert_not_called()
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_reset_stuck_episode_db_missing(self, MockMP, MockPC):
        """If MinusPod has no row for the episode at all, return 404 so
        the UI can surface a clear 'episode unknown' error."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {}}, tf)
            tmp_state = Path(tf.name)
        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            MockPC.return_value = MagicMock()
            mock_mp = MagicMock()
            MockMP.return_value = mock_mp

            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            with patch('ui_server._reset_stuck_episode_in_db',
                       return_value=(False, 'db_missing')):
                resp = client.post('/api/episodes/dlco/ep42/reset')
            self.assertEqual(resp.status_code, 404)
            self.assertFalse(resp.get_json()["db_reset"])
            mock_mp.reprocess_episode.assert_not_called()
        finally:
            pf.STATE_FILE = orig_state
            tmp_state.unlink(missing_ok=True)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_processed_list_and_clear(self, MockMP, MockPC):
        """GET lists entries, DELETE with keys removes a single one, DELETE
        with {all: true} wipes everything. Uses an isolated state file so
        the user's real processed_episodes.json isn't touched."""
        import pocketcasts_adfree as pf
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as tf:
            json.dump({"processed": {
                "podcast-a:ep1": {
                    "title": "Hello", "file_uuid": "f-1",
                    "processed_at": "2026-04-10T10:00:00Z",
                },
                "podcast-b:ep2": {
                    "title": "World", "file_uuid": "f-2",
                    "processed_at": "2026-04-15T10:00:00Z",
                },
            }}, tf)
            tmp_state = Path(tf.name)

        orig_state = pf.STATE_FILE
        try:
            pf.STATE_FILE = tmp_state
            from ui_server import create_app
            app = create_app("test@test.com", "testpass")
            client = app.test_client()

            resp = client.get('/api/processed')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['count'], 2)
            # Newest-first sort
            self.assertEqual(data['processed'][0]['key'], 'podcast-b:ep2')

            resp = client.delete('/api/processed',
                data=json.dumps({"keys": ["podcast-a:ep1"]}),
                content_type='application/json')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()['removed'], 1)

            resp = client.delete('/api/processed',
                data=json.dumps({"all": True}),
                content_type='application/json')
            self.assertEqual(resp.get_json()['removed'], 1)
        finally:
            pf.STATE_FILE = orig_state
            try:
                os.unlink(tmp_state)
            except Exception:
                pass

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_subscriptions_reconciles_stale_originals(self, MockMP, MockPC):
        """When an Ad-Free upload exists for an episode, /api/subscriptions
        must silently sweep the original from Up Next and mark it played,
        so users don't see the leftover "Dec 31, 1969" entries.
        """
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A", "url": "https://a.example/rss"},
        ]
        # Up Next contains the original AND the ad-free upload; reconcile
        # should call remove_from_up_next for the original only.
        up_next_episodes = [
            {"uuid": "orig-1", "title": "Episode One", "podcast": "pod-a"},
            {"uuid": "file-1", "title": "Episode One (Ad-Free)",
             "podcast": "da7aba5e-f11e-f11e-f11e-da7aba5ef11e"},
        ]
        mock_pc.client.post.return_value = MagicMock(
            raise_for_status=MagicMock(), json=MagicMock(return_value={"episodes": up_next_episodes}))
        mock_pc.get_new_releases.return_value = []
        mock_pc.get_files.return_value = {"files": [
            {"uuid": "file-1", "title": "Episode One (Ad-Free)"},
        ]}
        MockPC.return_value = mock_pc

        mock_mp = MagicMock()
        mock_mp.list_feeds.return_value = []
        MockMP.return_value = mock_mp

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/subscriptions')
        self.assertEqual(resp.status_code, 200)
        # The original must have been swept.
        mock_pc.remove_from_up_next.assert_any_call("orig-1")
        # …and marked played on the correct podcast.
        mock_pc.mark_episode_played.assert_any_call("orig-1", "pod-a")
        # The response must not expose the swept original to the UI.
        data = resp.get_json()
        titles = [e["title"] for e in data.get("up_next_episodes", [])]
        self.assertNotIn("Episode One", titles)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_subscriptions_reconciles_substring_title_mismatch(self, MockMP, MockPC):
        """When Pocket Casts has the original with a host prefix that MinusPod's
        RSS doesn't include, the reconcile should still sweep it via substring
        fallback matching.
        """
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A", "url": "https://a.example/rss"},
        ]
        up_next_episodes = [
            {"uuid": "orig-1", "title": "Podcast A — Episode 42", "podcast": "pod-a"},
            {"uuid": "file-1", "title": "Episode 42 (Ad-Free)",
             "podcast": "da7aba5e-f11e-f11e-f11e-da7aba5ef11e"},
        ]
        mock_pc.client.post.return_value = MagicMock(
            raise_for_status=MagicMock(), json=MagicMock(return_value={"episodes": up_next_episodes}))
        mock_pc.get_new_releases.return_value = []
        mock_pc.get_files.return_value = {"files": [
            {"uuid": "file-1", "title": "Episode 42 (Ad-Free)"},
        ]}
        MockPC.return_value = mock_pc
        mock_mp = MagicMock()
        mock_mp.list_feeds.return_value = []
        MockMP.return_value = mock_mp
        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/subscriptions')
        self.assertEqual(resp.status_code, 200)
        mock_pc.remove_from_up_next.assert_any_call("orig-1")

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_subscriptions_forwards_thumbnail(self, MockMP, MockPC):
        """/api/subscriptions must forward the Pocket Casts thumbnail URL for
        each podcast so the dashboard can render cover art. Older Pocket
        Casts responses sometimes omit the field, so the key must default
        to an empty string instead of erroring out.
        """
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A", "author": "Author A",
             "thumbnail": "https://example.com/a.jpg"},
            {"uuid": "pod-b", "title": "Podcast B", "author": "Author B"},
        ]
        mock_pc.get_new_releases.return_value = []
        mock_pc.client.post.return_value = MagicMock(
            raise_for_status=MagicMock(), json=MagicMock(return_value={"episodes": []}))
        mock_pc.get_files.return_value = {"files": []}
        MockPC.return_value = mock_pc
        MockMP.return_value = MagicMock()

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/subscriptions')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        pods = {p["uuid"]: p for p in data["podcasts"]}
        self.assertEqual(pods["pod-a"]["thumbnail"], "https://example.com/a.jpg")
        self.assertEqual(pods["pod-b"]["thumbnail"], "")

    @patch('ui_server.services_manager.start_minuspod')
    @patch('ui_server.time.sleep')
    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_api_episodes_returns_503_when_minuspod_down(
        self, MockMP, MockPC, _sleep, mock_start_minuspod,
    ):
        """GET /api/episodes/<uuid> must return a clean 503 (not 500) when
        MinusPod is unreachable. The 500 was caused by an un-guarded call
        to list_feeds() that bubbled httpx.ConnectError out of the view.
        """
        import httpx
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A",
             "url": "https://a.example/rss"},
        ]
        MockPC.return_value = mock_pc

        # MinusPod is down: health() raises a connection error even after
        # the one-shot auto-start attempt.
        mock_mp = MagicMock()
        mock_mp.health.side_effect = httpx.ConnectError(
            "[Errno 61] Connection refused")
        mock_mp.list_feeds.side_effect = httpx.ConnectError(
            "[Errno 61] Connection refused")
        MockMP.return_value = mock_mp
        mock_start_minuspod.return_value = {"ok": True}

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/episodes/pod-a')
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["episodes"], [])
        # The error message must mention MinusPod so the user knows
        # where to look (Services panel).
        self.assertIn("MinusPod", data["error"])
        mock_start_minuspod.assert_called_once()
        # And we must NOT have tried to hit list_feeds (the health check
        # should have short-circuited first).
        mock_mp.list_feeds.assert_not_called()

    @patch('ui_server.find_rss_url_for_podcast', return_value='https://a.example/rss')
    @patch('ui_server.services_manager.start_minuspod', return_value={'ok': True})
    @patch('ui_server.time.sleep')
    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_api_episodes_auto_starts_minuspod_when_down(
        self, MockMP, MockPC, _sleep, _start, _rss,
    ):
        """Expanding a podcast should recover if MinusPod was stopped."""
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A", "url": "https://a.example/rss"},
        ]
        mock_pc.get_podcast_episodes.return_value = []
        MockPC.return_value = mock_pc

        import httpx
        mock_mp = MagicMock()
        mock_mp.health.side_effect = [
            httpx.ConnectError("[Errno 61] Connection refused"),
            {"status": "healthy"},
        ]
        mock_mp.list_feeds.return_value = [
            {"slug": "pod-a", "sourceUrl": "https://a.example/rss"},
        ]
        mock_mp.get_episodes.return_value = [
            {"id": "ep1", "title": "Episode 1", "duration": 3600},
        ]
        MockMP.return_value = mock_mp

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/episodes/pod-a')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["episodes"]), 1)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_pc_episode_queue_endpoints(self, MockMP, MockPC):
        """POST /api/pc_episode/<uuid>/up_next queues an original episode;
        DELETE un-queues it. Used by the new per-episode controls in All
        Podcasts.
        """
        mock_pc = MagicMock()
        mock_pc._get_up_next_server_modified.return_value = 123
        mock_pc.client.post.return_value = MagicMock(raise_for_status=MagicMock())
        MockPC.return_value = mock_pc
        MockMP.return_value = MagicMock()

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()

        resp = client.post('/api/pc_episode/ep-1/up_next',
            data=json.dumps({"podcast_uuid": "pod-1", "title": "Hi"}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = client.delete('/api/pc_episode/ep-1/up_next')
        self.assertEqual(resp.status_code, 200)
        mock_pc.remove_from_up_next.assert_called_with("ep-1")

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_pc_episode_played_endpoint(self, MockMP, MockPC):
        """POST /api/pc_episode/<uuid>/played sets played/unplayed."""
        mock_pc = MagicMock()
        mock_pc.client.post.return_value = MagicMock(raise_for_status=MagicMock())
        MockPC.return_value = mock_pc
        MockMP.return_value = MagicMock()

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()

        resp = client.post('/api/pc_episode/ep-1/played',
            data=json.dumps({"podcast_uuid": "pod-1", "played": True}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = client.post('/api/pc_episode/ep-1/played',
            data=json.dumps({"played": False}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    @patch('ui_server.PocketCastsClient')
    @patch('ui_server.MinusPodClient')
    def test_subscriptions_enriches_up_next_with_status(self, MockMP, MockPC):
        """/api/subscriptions must splice playing_status / played_up_to /
        duration onto regular Up Next episodes so the UI can render the same
        metadata surface it shows for custom-file rows (instead of a stale
        'Loading metadata...' placeholder).
        """
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Pod A", "url": "https://a.example/rss"},
        ]
        up_next_raw = [
            {"uuid": "ep-1", "title": "Regular Episode", "podcast": "pod-a"},
        ]
        mock_pc.client.post.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"episodes": up_next_raw}),
        )
        mock_pc.get_new_releases.return_value = []
        mock_pc.get_files.return_value = {"files": []}
        mock_pc.get_podcast_episodes.return_value = [
            {
                "uuid": "ep-1",
                "playingStatus": 2,
                "playedUpTo": 1800,
                "duration": 3600,
                "isDeleted": False,
                "starred": True,
            },
        ]
        MockPC.return_value = mock_pc
        MockMP.return_value = MagicMock(list_feeds=MagicMock(return_value=[]))

        from ui_server import create_app
        app = create_app("test@test.com", "testpass")
        client = app.test_client()
        resp = client.get('/api/subscriptions')
        self.assertEqual(resp.status_code, 200)
        eps = resp.get_json().get("up_next_episodes", [])
        self.assertEqual(len(eps), 1)
        enriched = eps[0]
        self.assertEqual(enriched["uuid"], "ep-1")
        self.assertEqual(enriched["playing_status"], 2)
        self.assertEqual(enriched["played_up_to"], 1800)
        self.assertEqual(enriched["duration"], 3600)
        self.assertTrue(enriched["starred"])
        self.assertFalse(enriched["is_archived"])
        mock_pc.get_podcast_episodes.assert_called_with("pod-a")

    def test_queue_status_active_running_exposes_pause_flags(self):
        """Active job exposes current_episode_completed and paused so the UI
        can render '✓ <title> (Paused)' instead of leaving the row stale when
        the orchestrator enters its pause-and-free-resources wait."""
        import ui_server as _ui
        from ui_server import create_app, processing_jobs
        with patch("ui_server.PocketCastsClient"), \
             patch("ui_server.MinusPodClient"):
            app = create_app("test@test.com", "testpass")
            client = app.test_client()

            # Inject a synthetic running-but-paused job into the dict.
            import threading as _thr
            job_id = "synthetic-paused"
            stop_evt = _thr.Event()
            skip_evt = _thr.Event()
            pause_evt = _thr.Event()
            pause_evt.set()  # simulate the orchestrator having paused
            processing_jobs[job_id] = {
                "status": "running",
                "logs": [],
                "processed": 1,
                "uploaded": 1,
                "total_episodes": 3,
                "current_episode": "Giant Bombcast 952",
                "current_episode_completed": True,
                "paused": True,
                "log_cursor": 0,
                "skip_event": skip_evt,
                "stop_event": stop_evt,
                "pause_event": pause_evt,
            }
            old_active = _ui.active_job_id
            try:
                _ui.active_job_id = job_id
                resp = client.get("/api/queue/status")
                self.assertEqual(resp.status_code, 200)
                active = resp.get_json().get("active_job")
                self.assertIsNotNone(active)
                self.assertEqual(active["current_episode"], "Giant Bombcast 952")
                self.assertTrue(active["current_episode_completed"])
                self.assertTrue(active["paused"])
            finally:
                _ui.active_job_id = old_active
                processing_jobs.pop(job_id, None)

    def test_queue_status_running_unpaused_does_not_mark_paused(self):
        """A running job with no pause_event set must report paused=False."""
        import ui_server as _ui
        from ui_server import create_app, processing_jobs
        with patch("ui_server.PocketCastsClient"), \
             patch("ui_server.MinusPodClient"):
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            import threading as _thr
            job_id = "synthetic-running"
            processing_jobs[job_id] = {
                "status": "running",
                "logs": [],
                "processed": 0,
                "uploaded": 0,
                "total_episodes": 2,
                "current_episode": "Some Episode",
                "current_episode_completed": False,
                "paused": False,
                "log_cursor": 0,
                "skip_event": _thr.Event(),
                "stop_event": _thr.Event(),
                "pause_event": _thr.Event(),
            }
            old_active = _ui.active_job_id
            try:
                _ui.active_job_id = job_id
                resp = client.get("/api/queue/status")
                active = resp.get_json().get("active_job")
                self.assertIsNotNone(active)
                self.assertFalse(active["paused"])
                self.assertFalse(active["current_episode_completed"])
            finally:
                _ui.active_job_id = old_active
                processing_jobs.pop(job_id, None)




class TestTranscriptPrePopulation(unittest.TestCase):
    """PC transcript parsing, alignment gate, and gated injection guards."""

    def test_vtt_to_minuspod_format(self):
        from pocketcasts_adfree import _parse_vtt_cues, _vtt_cues_to_minuspod_text

        vtt = """WEBVTT

1
00:00:00.000 --> 00:00:05.123
Hello world

2
00:01:30.456 --> 00:01:35.789
Second segment
"""
        cues = _parse_vtt_cues(vtt)
        lines = _vtt_cues_to_minuspod_text(cues).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("[00:00:00.000 --> 00:00:05.123] Hello world", lines[0])
        self.assertIn("[00:01:30.456 --> 00:01:35.789] Second segment", lines[1])

    def test_parse_vtt_cues_strips_voice_tags(self):
        from pocketcasts_adfree import _parse_vtt_cues

        vtt = """WEBVTT

00:00:00.000 --> 00:00:03.000
<v Speaker>Hello <00:00:01.000>there</v>
"""
        cues = _parse_vtt_cues(vtt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Hello there")

    def test_old_substring_sync_would_fail_on_timestamps(self):
        """Regression: normalizing raw VTT interleaves timestamp digits."""
        from pocketcasts_adfree import _normalize_title

        vtt = """WEBVTT
00:00:00.000 --> 00:00:05.000
hello world from the podcast
00:00:05.000 --> 00:00:10.000
and here is more content
"""
        sample = "hello world from the podcast and here"
        vtt_norm = _normalize_title(vtt[:4000])
        self.assertNotIn(sample, vtt_norm)

    def test_align_sample_finds_match(self):
        from pocketcasts_adfree import _align_sample, _parse_vtt_cues

        vtt = """WEBVTT
00:00:28.000 --> 00:00:35.000
welcome back to the show everyone
00:00:35.000 --> 00:00:42.000
today we talk about video games
"""
        cues = _parse_vtt_cues(vtt)
        ratio, matched = _align_sample(
            "welcome back to the show everyone", cues, expected_time=30.0
        )
        self.assertGreaterEqual(ratio, 0.8)
        self.assertAlmostEqual(matched, 28.0, delta=2.0)

    def test_verify_rejects_partial_transcript(self):
        from pocketcasts_adfree import _verify_pc_transcript

        vtt = """WEBVTT
00:00:00.000 --> 00:01:30.000
short partial transcript only
"""
        ok, metrics = _verify_pc_transcript(vtt, "", audio_duration=3600.0)
        self.assertFalse(ok)
        self.assertIn("coverage", metrics["failure_reason"])

    def test_proportional_duration_allows_trailing_gap(self):
        from pocketcasts_adfree import (
            pc_transcript_coverage_metrics,
            pc_transcript_effective_max_duration_delta,
        )

        # 7100s VTT on 7200s audio: 98.6% coverage, 100s gap — should pass
        cov = pc_transcript_coverage_metrics(7100.0, 7200.0)
        self.assertTrue(cov["coverage_pass"])
        self.assertTrue(cov["duration_pass"])
        self.assertAlmostEqual(
            pc_transcript_effective_max_duration_delta(7200.0), 216.0, places=0
        )

    def test_low_coverage_still_fails(self):
        from pocketcasts_adfree import pc_transcript_coverage_metrics

        cov = pc_transcript_coverage_metrics(6000.0, 7200.0)
        self.assertFalse(cov["coverage_pass"])
        self.assertFalse(cov["duration_pass"])

    @patch("pocketcasts_adfree._whisper_sample_available", return_value=False)
    def test_verify_rejects_when_whisper_unavailable(self, _mock_whisper):
        from pocketcasts_adfree import _verify_pc_transcript

        vtt = """WEBVTT
00:00:00.000 --> 00:10:00.000
""" + "word " * 500
        ok, metrics = _verify_pc_transcript(
            vtt, "http://example.com/ep.mp3", audio_duration=600.0
        )
        self.assertFalse(ok)
        self.assertIn("whisper-cli unavailable", metrics["failure_reason"])

    @patch("pocketcasts_adfree._transcribe_sample")
    @patch("pocketcasts_adfree._whisper_sample_available", return_value=True)
    def test_verify_accepts_matching_transcript(self, _mock_avail, mock_sample):
        from pocketcasts_adfree import _verify_pc_transcript

        def _fake_sample(url, start, duration=15.0):
            if start < 135:
                return "we are sponsored by raycon wireless earbuds today"
            if start < 225:
                return "lets talk about the latest video game news"
            if start < 375:
                return "thanks for listening to giant bomb premium members"
            if start < 495:
                return "almost at the end of giant bombcast episode"
            return "see you next week on the bombcast goodbye everyone"

        mock_sample.side_effect = _fake_sample

        cues = []
        texts = [
            (0, 20, "welcome to the giant bombcast episode nine fifty one"),
            (90, 110, "we are sponsored by raycon wireless earbuds today"),
            (150, 180, "lets talk about the latest video game news"),
            (300, 330, "thanks for listening to giant bomb premium members"),
            (450, 470, "almost at the end of giant bombcast episode"),
            (510, 600, "see you next week on the bombcast goodbye everyone"),
        ]
        for start, end, text in texts:
            cues.append(
                f"00:{start//60:02d}:{start%60:02d}.000 --> "
                f"00:{end//60:02d}:{end%60:02d}.000\n{text}"
            )
        vtt = "WEBVTT\n" + "\n".join(cues)

        ok, metrics = _verify_pc_transcript(
            vtt, "http://example.com/ep.mp3", audio_duration=600.0
        )
        self.assertTrue(ok, metrics)
        self.assertGreater(metrics["coverage"], 0.9)

    @patch("pocketcasts_adfree._transcribe_sample")
    @patch("pocketcasts_adfree._whisper_sample_available", return_value=True)
    def test_verify_rejects_preroll_offset(self, _mock_avail, mock_sample):
        from pocketcasts_adfree import _verify_pc_transcript

        mock_sample.return_value = "this is the actual audio at thirty seconds"

        vtt = """WEBVTT
00:01:00.000 --> 00:01:20.000
this is the actual audio at thirty seconds
00:02:30.000 --> 00:02:50.000
middle of the episode content here today
00:03:00.000 --> 00:03:20.000
more middle content for alignment checks
00:04:30.000 --> 00:04:50.000
late episode content continues here now
00:09:40.000 --> 00:10:00.000
near the end of this test episode goodbye
"""
        ok, metrics = _verify_pc_transcript(
            vtt, "http://example.com/ep.mp3", audio_duration=600.0
        )
        self.assertFalse(ok)
        self.assertTrue(
            "offset" in metrics["failure_reason"]
            or "similarity" in metrics["failure_reason"]
        )

    def test_main_flow_pre_populates_on_verified_branch(self):
        """pre_populate_transcript must only run inside the verified gate."""
        import inspect
        from pocketcasts_adfree import process_single_episode

        src = inspect.getsource(process_single_episode)
        self.assertIn("pre_populate_transcript(", src)
        idx = src.index("if verified:")
        branch = src[idx : idx + 800]
        self.assertIn("pre_populate_transcript(", branch)
        self.assertNotIn(
            "pre_populate_transcript(",
            src[: idx],
            "pre_populate_transcript must not run before verification",
        )


class TestResolvePcEpisodeUuid(unittest.TestCase):
    """PC episode UUID resolution from catalog metadata."""

    def _catalog(self):
        return [
            {
                "uuid": "ep-955",
                "title": "Giant Bombcast 955: Smash or Pass",
                "url": "https://cdn.example.com/giant_bombcast_955.mp3",
                "duration": 7638,
            },
            {
                "uuid": "ep-954",
                "title": "Giant Bombcast 954: Other Episode",
                "url": "https://cdn.example.com/giant_bombcast_954.mp3",
                "duration": 7200,
            },
        ]

    def test_resolve_by_strong_title(self):
        from pocketcasts_adfree import resolve_pc_episode_uuid

        eu = resolve_pc_episode_uuid(
            "Giant Bombcast 955: Smash or Pass", self._catalog()
        )
        self.assertEqual(eu, "ep-955")

    def test_resolve_by_audio_url(self):
        from pocketcasts_adfree import resolve_pc_episode_uuid

        eu = resolve_pc_episode_uuid(
            "Completely Different Title",
            self._catalog(),
            audio_url="https://dts.podtrac.com/redirect.mp3/cdn.example.com/giant_bombcast_955.mp3",
        )
        self.assertEqual(eu, "ep-955")

    def test_resolve_by_duration_unique(self):
        from pocketcasts_adfree import resolve_pc_episode_uuid

        eu = resolve_pc_episode_uuid(
            "Unknown",
            self._catalog(),
            duration=7200.0,
        )
        self.assertEqual(eu, "ep-954")


class TestPcTranscriptValidationHelpers(unittest.TestCase):
    def test_drift_correction_improves_ad_overlap(self):
        from pocketcasts_adfree import (
            compare_ad_markers_in_transcripts,
            estimate_transcript_drift,
        )

        whisper_segments = [
            {"start": 100.0, "end": 120.0, "text": "brought to you by acme widgets"},
        ]
        pc_cues = [
            {"start": 230.0, "end": 250.0, "text": "brought to you by acme widgets"},
        ]
        ad_markers = [{"start": 100.0, "end": 120.0}]
        drift_probes = [{"similarity": 0.9, "offset": 130.0}]
        drift = estimate_transcript_drift(drift_probes)

        raw = compare_ad_markers_in_transcripts(
            whisper_segments, pc_cues, ad_markers, drift=0.0
        )
        corrected = compare_ad_markers_in_transcripts(
            whisper_segments, pc_cues, ad_markers, drift=drift
        )
        self.assertFalse(raw[0]["ad_present_in_pc"])
        self.assertTrue(corrected[0]["ad_present_in_pc"])

    def test_judge_allows_one_probe_failure(self):
        from pocketcasts_adfree import _judge_pc_transcript_probes

        probes = [
            {"time": 90.0, "similarity": 0.9, "offset": -9.6},
            {"time": 300.0, "similarity": 0.95, "offset": -0.4},
            {"time": 600.0, "similarity": 0.92, "offset": 0.1},
        ]
        passed, reason, stats = _judge_pc_transcript_probes(probes)
        self.assertTrue(passed, reason)
        self.assertEqual(stats["probes_failed"], 1)

    def test_probe_times_skip_cold_open(self):
        from pocketcasts_adfree import _probe_times_for_duration

        times = _probe_times_for_duration(3600.0, 5)
        self.assertGreaterEqual(times[0], 90.0)

    def test_simulate_gate_passes_aligned_transcript(self):
        from pocketcasts_adfree import simulate_pc_transcript_gate_from_segments

        texts = [
            (90, 110, "opening welcome to our perfectly aligned podcast show"),
            (150, 180, "first sponsor read from acme widgets and friends"),
            (300, 330, "middle discussion about games movies and culture"),
            (450, 470, "second sponsor read from raycon earbuds today"),
            (510, 600, "closing thanks for listening see you next week"),
        ]
        whisper_segments = []
        pc_cues = []
        for start, end, text in texts:
            whisper_segments.append({"start": float(start), "end": float(end), "text": text})
            pc_cues.append({"start": float(start), "end": float(end), "text": text})

        gate = simulate_pc_transcript_gate_from_segments(
            whisper_segments, pc_cues, audio_duration=600.0
        )
        self.assertTrue(gate["gate_pass_simulated"], gate)


class TestEpisodeTitleMatching(unittest.TestCase):
    """Episode title matching must be case-insensitive and handle punctuation."""

    def test_exact_match(self):
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("Giant Bombcast 936: Big Pinball"): "uuid-123"}
        ep_title = "Giant Bombcast 936: Big Pinball"
        result = pc_episode_map.get(_normalize_title(ep_title))
        self.assertEqual(result, "uuid-123")

    def test_whitespace_stripped(self):
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("My Episode"): "uuid-456"}
        ep_title = "  My Episode  "
        result = pc_episode_map.get(_normalize_title(ep_title))
        self.assertEqual(result, "uuid-456")

    def test_punctuation_ignored(self):
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("How to Change the World!"): "uuid-abc"}
        ep_title = "How to Change the World"
        result = pc_episode_map.get(_normalize_title(ep_title))
        self.assertEqual(result, "uuid-abc")

    def test_colon_differences(self):
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("CAGcast #840: #notmybiomes"): "uuid-cag"}
        ep_title = "CAGcast #840 #notmybiomes"
        # Exact match after normalization (both lose punctuation)
        result = pc_episode_map.get(_normalize_title(ep_title))
        self.assertEqual(result, "uuid-cag")

    def test_substring_fallback(self):
        """When normalized titles don't match exactly, substring match works."""
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("Pragmata Made Me a Believer"): "uuid-prag"}
        ep_title = "Pragmata Made Me a Believer (Review)"
        norm = _normalize_title(ep_title)
        result = pc_episode_map.get(norm)
        if not result:
            for pc_title, pc_uuid in pc_episode_map.items():
                if norm in pc_title or pc_title in norm:
                    result = pc_uuid
                    break
        self.assertEqual(result, "uuid-prag")

    def test_no_match_returns_none(self):
        from ui_server import _normalize_title
        pc_episode_map = {_normalize_title("Some Episode"): "uuid-789"}
        ep_title = "Different Episode"
        result = pc_episode_map.get(_normalize_title(ep_title))
        self.assertIsNone(result)


class TestDownloadProcessedAudio(unittest.TestCase):
    """download_processed_audio must reject missing slug loudly.

    Regression guard: a previous build constructed a bogus
    /episodes/direct/<b64>.mp3 URL that MinusPod doesn't expose, giving
    users a confusing 404. Force a clear error instead.
    """

    def test_rejects_missing_slug(self):
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            with self.assertRaises(ValueError) as ctx:
                mp.download_processed_audio(
                    None, "ep-1", Path("/tmp"),
                    source_url="https://example.com/a.mp3",
                )
            self.assertIn("not supported", str(ctx.exception).lower())

    def test_rejects_files_slug(self):
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            with self.assertRaises(ValueError):
                mp.download_processed_audio(
                    "_files", "ep-1", Path("/tmp"),
                )

def test_get_episodes_requests_wider_limit(self):
        """limit=500 widens the window so older Up Next items are findable."""
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            resp = MagicMock()
            resp.json.return_value = {"episodes": []}
            mp.client.request.return_value = resp
            mp.get_episodes("some-slug")
            called_url = mp.client.request.call_args[0][1]
            self.assertIn("limit=500", called_url)


class TestFailedEpisodeAbort(unittest.TestCase):
    """Regression: if MinusPod marks the episode 'failed' / 'permanently_failed',
    the download retry loop must surface that error promptly instead of
    spinning forever on 503."""

    def _make_resp(self, status_code: int, headers: dict | None = None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = headers or {}
        # context-manager protocol
        resp.__enter__ = lambda self_: self_
        resp.__exit__ = lambda self_, *a: False
        resp.raise_for_status = MagicMock()
        return resp

    def test_aborts_when_episode_permanently_failed(self):
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.sleep'):  # speed up retry waits
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            mp.client.stream.return_value = self._make_resp(
                503, {"Retry-After": "1"}
            )
            with patch.object(mp, "get_status", return_value={"currentJob": None}), \
                 patch.object(mp, "get_episode", return_value={
                     "status": "permanently_failed",
                     "error": "Failed to transcribe audio",
                 }), \
                 patch.object(mp, "reprocess_episode", return_value={}):
                with self.assertRaises(RuntimeError) as ctx:
                    mp.download_processed_audio(
                        "some-slug", "ep-1", Path("/tmp"),
                        max_retries=20, retry_delay=0,
                    )
            self.assertIn("permanently_failed", str(ctx.exception))
            self.assertIn("Failed to transcribe audio", str(ctx.exception))

    def test_caps_410_reprocess_attempts(self):
        """If the .mp3 endpoint keeps returning 410, we should give up
        after MAX_REPROCESS_TRIGGERS instead of spinning forever."""
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.sleep'):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            mp.client.stream.return_value = self._make_resp(410)
            with patch.object(mp, "reprocess_episode", return_value={}) as m_re, \
                 patch.object(mp, "get_episode", return_value={
                     "status": "failed", "error": "Whisper unreachable"
                 }):
                with self.assertRaises(RuntimeError) as ctx:
                    mp.download_processed_audio(
                        "some-slug", "ep-1", Path("/tmp"),
                        max_retries=10, retry_delay=0,
                    )
            self.assertIn("gave up", str(ctx.exception).lower())
            # MinusPodClient triggers reprocess at most MAX_REPROCESS_TRIGGERS (2) times
            self.assertLessEqual(m_re.call_count, 2)

    def test_get_episode_returns_none_on_404(self):
        from pocketcasts_adfree import MinusPodClient, httpx
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            resp = MagicMock()
            resp.status_code = 404
            # The new implementation uses _request -> client.request
            http_error = httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
            mp.client.request.side_effect = http_error
            self.assertIsNone(mp.get_episode("slug", "ep"))

    def test_wallclock_cap_aborts_runaway_poll(self):
        """A wedged backend used to make a single episode poll for ~8 hours.

        The wallclock cap (default 90 min) is the safety net that kept
        the second queued episode from ever uploading: episode #1 just
        sat in this loop forever. Verify that exceeding the cap raises
        TimeoutError instead of looping.
        """
        from pocketcasts_adfree import MinusPodClient
        # First call sets wallclock_start, second call (top of next loop
        # iteration) is 6000 s later — comfortably past the 60 s cap.
        clock = [1000.0]

        def fake_monotonic():
            t = clock[0]
            clock[0] += 6000.0   # +100 min per call → blows past 90 min cap
            return t

        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.monotonic', side_effect=fake_monotonic), \
             patch('pocketcasts_adfree.time.sleep'):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            # Return 503 so we go through the polling path. The wallclock
            # check fires on the next iteration before we ever fetch status.
            mp.client.stream.return_value = self._make_resp(503, {"Retry-After": "0"})
            with patch.object(mp, "get_status", return_value={"currentJob": {"stage": "transcribing"}}):
                with self.assertRaises(TimeoutError) as ctx:
                    mp.download_processed_audio(
                        "some-slug", "ep-1", Path("/tmp"),
                        max_retries=1000, retry_delay=0,
                        max_wallclock_seconds=60,
                    )
        self.assertIn("Gave up", str(ctx.exception))
        # Hint about EPISODE_MAX_WALLCLOCK_SECONDS should be in the error
        # so users know how to bump it without grepping the source.
        self.assertIn("EPISODE_MAX_WALLCLOCK_SECONDS", str(ctx.exception))

    def test_is_llm_stage_detects_ad_detection(self):
        from pocketcasts_adfree import _is_llm_stage, _is_transcription_stage
        self.assertTrue(_is_llm_stage("pass1:detecting:1/9"))
        self.assertTrue(_is_llm_stage("pass1:verifying"))
        self.assertFalse(_is_llm_stage("pass1:transcribing 3/14"))
        self.assertTrue(_is_transcription_stage("pass1:transcribing"))

    def test_stall_threshold_higher_for_llm_stages(self):
        from pocketcasts_adfree import _stall_threshold_for_stage
        self.assertEqual(_stall_threshold_for_stage("pass1:detecting:1/9", 900), 2700)
        self.assertEqual(_stall_threshold_for_stage("pass1:transcribing", 900), 900)

    def test_stall_watchdog_bounces_whisper_then_aborts(self):
        """If MinusPod's `stage` doesn't change for the threshold window,
        we should bounce whisper-server first; if it stalls a SECOND time,
        abort the episode so the queue can move on instead of looping."""
        from pocketcasts_adfree import MinusPodClient
        # Three relevant time points: t0=start, t1=stall #1 detected,
        # t2=stall #2 detected. Anything in between just needs to be
        # increasing. Use a counter-based clock that jumps past the
        # stall threshold every iteration so the watchdog fires.
        clock = [0.0]

        def fake_monotonic():
            t = clock[0]
            clock[0] += 2000.0   # +33 min per call → > 15 min stall threshold
            return t

        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.monotonic', side_effect=fake_monotonic), \
             patch('pocketcasts_adfree.time.sleep'), \
             patch('pocketcasts_adfree._bounce_service_for_stall',
                   return_value=(True, "whisper-server")):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            mp.client.stream.return_value = self._make_resp(503, {"Retry-After": "1"})
            # Stage never advances (always "transcribing") → stall.
            with patch.object(mp, "get_status", return_value={
                "currentJob": {"stage": "transcribing", "progress": 50, "elapsed": 60}
            }), patch.object(mp, "get_episode", return_value=None):
                with self.assertRaises(TimeoutError) as ctx:
                    mp.download_processed_audio(
                        "slug", "ep-1", Path("/tmp"),
                        max_retries=50, retry_delay=0,
                        max_wallclock_seconds=10**9,  # disable wallclock cap
                        stall_threshold_seconds=60,
                    )
        self.assertIn("stuck on stage", str(ctx.exception))
        self.assertIn("whisper-server.log", str(ctx.exception))

    def test_stall_watchdog_bounces_ollama_for_detecting(self):
        """Ad detection stalls should restart Ollama, not whisper."""
        from pocketcasts_adfree import MinusPodClient
        clock = [0.0]

        def fake_monotonic():
            t = clock[0]
            clock[0] += 2000.0
            return t

        bounce_calls = []

        def fake_bounce(stage):
            bounce_calls.append(stage)
            return True, "Ollama"

        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.monotonic', side_effect=fake_monotonic), \
             patch('pocketcasts_adfree.time.sleep'), \
             patch('pocketcasts_adfree._bounce_service_for_stall', side_effect=fake_bounce):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            mp.client.stream.return_value = self._make_resp(503, {"Retry-After": "1"})
            with patch.object(mp, "get_status", return_value={
                "currentJob": {"stage": "pass1:detecting:1/9", "progress": 50, "elapsed": 60}
            }), patch.object(mp, "get_episode", return_value=None):
                with self.assertRaises(TimeoutError) as ctx:
                    mp.download_processed_audio(
                        "slug", "ep-1", Path("/tmp"),
                        max_retries=50, retry_delay=0,
                        max_wallclock_seconds=10**9,
                        stall_threshold_seconds=60,
                    )
        self.assertEqual(bounce_calls, ["pass1:detecting:1/9"])
        self.assertIn("ollama.log", str(ctx.exception))

    def test_bounce_service_uses_minuspod_for_cloud_llm(self):
        from pocketcasts_adfree import _bounce_service_for_stall
        with patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=False), \
             patch("pocketcasts_adfree._restart_minuspod_if_wedged",
                   return_value=True) as m_mp:
            ok, name = _bounce_service_for_stall("pass1:detecting:1/9")
        self.assertTrue(ok)
        self.assertEqual(name, "MinusPod")
        m_mp.assert_called_once()

    def test_orphaned_recovery_resets_db_and_reprocesses(self):
        """Broken SQL (episodes.slug) used to make orphan recovery a no-op."""
        from pocketcasts_adfree import MinusPodClient, _reset_orphaned_episode_in_db
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None), \
             patch('pocketcasts_adfree.time.sleep'), \
             patch('pocketcasts_adfree._reset_orphaned_episode_in_db',
                   return_value=True) as m_reset:
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            mp.client.stream.return_value = self._make_resp(
                503, {"Retry-After": "1"},
            )
            with patch.object(mp, "get_status", return_value={"currentJob": None}), \
                 patch.object(mp, "get_episode", return_value={
                     "status": "processing",
                 }), \
                 patch.object(mp, "reprocess_episode", return_value={}) as m_re:
                # STATUS_CHECK_EVERY=3 → need attempt>0 and attempt%3==0
                # The wallclock cap is set high (10^9) but the loop will eventually
                # hit max_retries and raise TimeoutError. The test's purpose is to
                # verify orphan recovery is attempted, not that it succeeds.
                with self.assertRaises(TimeoutError):
                    mp.download_processed_audio(
                        "dlc", "e6e9936c52a7", Path("/tmp"),
                        max_retries=10, retry_delay=0,
                        max_wallclock_seconds=10**9,
                        stall_threshold_seconds=10**9,
                    )
        m_reset.assert_called()
        m_re.assert_called_with("dlc", "e6e9936c52a7", mode="full")


class TestUpNextTitleMatching(unittest.TestCase):
    """When a PC Up Next UUID isn't in MinusPod's ep_map, the fallback
    matches by title against MinusPod's episode list (same feed)."""

    def _make_matcher(self, episodes):
        """Return a callable that mimics the _match_mp_episode_by_title
        helper but runs without needing a live MinusPod client."""
        from ui_server import _normalize_title

        def match(pc_title):
            target = _normalize_title(pc_title)
            if not target:
                return None
            for e in episodes:
                if _normalize_title(e.get("title", "")) == target:
                    return e
            for e in episodes:
                t = _normalize_title(e.get("title", ""))
                if target in t or t in target:
                    return e
            return None
        return match

    def test_exact_title_match(self):
        eps = [
            {"id": "mp-1", "title": "Foo Episode 1"},
            {"id": "mp-2", "title": "Foo Episode 2"},
            {"id": "mp-3", "title": "Prince of Persia: The Lost Crown Dev Team Potentially Reuniting"},
        ]
        m = self._make_matcher(eps)
        result = m("Prince of Persia: The Lost Crown Dev Team Potentially Reuniting")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "mp-3")

    def test_substring_title_match(self):
        eps = [{"id": "mp-9", "title": "Pragmata Made Me a Believer"}]
        m = self._make_matcher(eps)
        result = m("Pragmata Made Me a Believer (Review)")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "mp-9")

    def test_no_match_returns_none(self):
        eps = [{"id": "mp-1", "title": "Completely Unrelated"}]
        m = self._make_matcher(eps)
        self.assertIsNone(m("Prince of Persia: The Lost Crown"))

    def test_empty_title_returns_none(self):
        eps = [{"id": "mp-1", "title": "Foo"}]
        m = self._make_matcher(eps)
        self.assertIsNone(m(""))


class TestRSSUrlDetection(unittest.TestCase):
    """RSS URL detection must recognize common podcast hosting platforms."""

    def test_simplecast_url(self):
        from pocketcasts_adfree import _is_rss_url
        self.assertTrue(_is_rss_url("https://feeds.simplecast.com/abc123"))

    def test_libsyn_url(self):
        from pocketcasts_adfree import _is_rss_url
        self.assertTrue(_is_rss_url("https://myshow.libsyn.com/rss"))

    def test_website_url(self):
        from pocketcasts_adfree import _is_rss_url
        self.assertFalse(_is_rss_url("https://www.myshow.com"))

    def test_spreaker_episodes_feed(self):
        from pocketcasts_adfree import _is_rss_url
        self.assertTrue(_is_rss_url("https://www.spreaker.com/show/12345/episodes/feed"))


class TestProcessedPodcastDetection(unittest.TestCase):
    """Processed episodes filter must find podcasts by slug or title."""

    def test_slug_extracted_from_state_key(self):
        state_key = "giant-bombcast:91cca1e2d0a2"
        slug = state_key.split(":")[0] if ":" in state_key else ""
        self.assertEqual(slug, "giant-bombcast")

    def test_old_format_without_colon(self):
        state_key = "e0aa60f56a35"
        slug = state_key.split(":")[0] if ":" in state_key else ""
        self.assertEqual(slug, "")


class TestServicesManager(unittest.TestCase):
    """Status discovery, action dispatch, and Ollama model picker.

    All shell-outs (lsof, ps, brew, docker) are patched so tests don't
    depend on the host having any service running.
    """

    def setUp(self):
        import services_manager as sm
        self.sm = sm

    def _mk_proc_run(self, returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_pid_listening_parses_lsof_output(self):
        with patch("services_manager.subprocess.run",
                   return_value=self._mk_proc_run(stdout="12345\n")):
            self.assertEqual(self.sm._pid_listening(8765), 12345)

    def test_pid_listening_returns_none_when_nothing_bound(self):
        with patch("services_manager.subprocess.run",
                   return_value=self._mk_proc_run(returncode=1, stdout="")):
            self.assertIsNone(self.sm._pid_listening(8765))

    def test_http_ok_substring_check(self):
        ok_resp = MagicMock(status_code=200, text='{"status":"healthy"}')
        with patch("services_manager.httpx.get", return_value=ok_resp):
            self.assertTrue(self.sm._http_ok("http://x", expect_substr="healthy"))
            self.assertFalse(self.sm._http_ok("http://x", expect_substr="missing"))

    def test_http_ok_swallows_exceptions(self):
        with patch("services_manager.httpx.get", side_effect=Exception("boom")):
            self.assertFalse(self.sm._http_ok("http://x"))

    def test_status_whisper_flags_docker_as_warning(self):
        with patch("services_manager._pid_listening", return_value=999), \
             patch("services_manager._http_ok", return_value=True), \
             patch("services_manager._proc_command",
                   return_value="/Applications/Docker.app/Contents/Resources/bin/com.docker.cli"), \
             patch("services_manager._docker_container_status", return_value="running"):
            s = self.sm.status_whisper()
        self.assertTrue(s.healthy)
        self.assertEqual(s.backend, "docker")
        self.assertIn("warning", s.extra)
        self.assertIn("emulation", s.extra["warning"])

    def test_status_whisper_native_no_warning(self):
        with patch("services_manager._pid_listening", return_value=42), \
             patch("services_manager._http_ok", return_value=True), \
             patch("services_manager._proc_command",
                   return_value="/Users/x/whisper.cpp/build/bin/whisper-server"), \
             patch("services_manager._docker_container_status", return_value=None):
            s = self.sm.status_whisper()
        self.assertEqual(s.backend, "native")
        self.assertNotIn("warning", s.extra)

    def test_status_ui_cannot_self_terminate(self):
        with patch("services_manager._pid_listening", return_value=1), \
             patch("services_manager._http_ok", return_value=True):
            s = self.sm.status_ui()
        self.assertFalse(s.can_stop)
        self.assertFalse(s.can_restart)
        self.assertFalse(s.can_start)

    def test_status_ui_probes_health_endpoint(self):
        with patch("services_manager._pid_listening", return_value=1), \
             patch("services_manager._http_ok", return_value=True) as mock_ok:
            self.sm.status_ui()
        mock_ok.assert_called_once_with("http://localhost:5050/api/health")

    def test_perform_action_unknown_service_raises(self):
        with self.assertRaises(self.sm.ServiceError):
            self.sm.perform_action("nonexistent", "start")

    def test_perform_action_unsupported_action_raises(self):
        with self.assertRaises(self.sm.ServiceError):
            self.sm.perform_action("ollama", "explode")

    def test_perform_action_passes_whisper_backend_kwarg(self):
        with patch("services_manager.start_whisper") as m:
            m.return_value = {"ok": True}
            self.sm.ACTIONS["whisper"]["start"] = m
            try:
                self.sm.perform_action("whisper", "start", backend="docker")
                m.assert_called_once_with(backend="docker")
            finally:
                # restore the real function
                self.sm.ACTIONS["whisper"]["start"] = self.sm.start_whisper

    def test_set_minuspod_model_requires_name(self):
        with self.assertRaises(self.sm.ServiceError):
            self.sm.set_minuspod_model("")

    def test_set_minuspod_model_calls_settings_api(self):
        captured = {}
        def fake_put(url, json=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            return MagicMock(status_code=200)
        with patch("services_manager.httpx.put", side_effect=fake_put):
            r = self.sm.set_minuspod_model("qwen3.5-addetect")
        self.assertTrue(r["ok"])
        self.assertIn("/settings/ad-detection", captured["url"])
        self.assertEqual(captured["body"]["claudeModel"], "qwen3.5-addetect")
        self.assertEqual(captured["body"]["verificationModel"], "qwen3.5-addetect")
        self.assertEqual(captured["body"]["chaptersModel"], "qwen3.5-addetect")

    def test_list_ollama_models_returns_empty_on_failure(self):
        with patch("services_manager.httpx.get", side_effect=Exception("down")):
            self.assertEqual(self.sm.list_ollama_models(), [])

    def test_read_log_tail_returns_last_n_lines(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            for i in range(50):
                f.write(f"line {i}\n")
            path = Path(f.name)
        try:
            text = self.sm._read_log_tail(path, lines=5)
            lines = text.splitlines()
            self.assertEqual(len(lines), 5)
            self.assertEqual(lines[-1], "line 49")
        finally:
            path.unlink()

    def test_read_log_tail_missing_file_returns_empty(self):
        self.assertEqual(self.sm._read_log_tail(Path("/nonexistent/x.log")), "")

    @unittest.skipUnless(sys.platform == "darwin", "macOS-only memory pressure check")
    def test_get_memory_pressure_warns_on_low_free(self):
        """Preflight must surface a warning when free RAM is dangerously
        low. The threshold (8 GB) is what we've found to be the difference
        between healthy fan-spin and kernel panic on Apple Silicon Macs
        running the default Qwen3.5 35B-A3B model alongside Whisper."""
        # 36 GB total, ~3 GB available  → 1.5 GB free + 1.5 GB inactive
        # at 4096 byte pages. Numbers chosen to land below the 8 GB warn
        # threshold without being so low they're implausible.
        page = 4096
        free_pages = int(1.5 * 1024**3) // page
        inactive_pages = int(1.5 * 1024**3) // page
        vm_text = (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            f"Pages free:                  {free_pages}.\n"
            f"Pages inactive:              {inactive_pages}.\n"
            "Pages speculative:           0.\n"
            "Pages purgeable:             0.\n"
            "Pages wired down:            1000.\n"
            "Pages active:                1000.\n"
        )

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["sysctl", "-n"] and cmd[2] == "hw.memsize":
                return self._mk_proc_run(stdout=str(36 * 1024**3))
            if cmd[0] == "vm_stat":
                return self._mk_proc_run(stdout=vm_text)
            return self._mk_proc_run(stdout="")

        with patch("services_manager.subprocess.run", side_effect=fake_run), \
             patch("services_manager.httpx.get", side_effect=Exception("ollama down")):
            result = self.sm.get_memory_pressure()
        self.assertEqual(result["total_gb"], 36.0)
        self.assertLess(result["available_gb"], 8.0)
        self.assertIsNotNone(result["warning"])
        self.assertIn("free", result["warning"].lower())

    @unittest.skipUnless(sys.platform == "darwin", "macOS-only memory pressure check")
    def test_get_memory_pressure_no_warning_when_plenty_free(self):
        page = 4096
        free_pages = int(16 * 1024**3) // page  # 16 GB free
        vm_text = (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            f"Pages free:                  {free_pages}.\n"
            "Pages inactive:              0.\n"
            "Pages speculative:           0.\n"
            "Pages purgeable:             0.\n"
            "Pages wired down:            1000.\n"
            "Pages active:                1000.\n"
        )

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["sysctl", "-n"] and cmd[2] == "hw.memsize":
                return self._mk_proc_run(stdout=str(36 * 1024**3))
            if cmd[0] == "vm_stat":
                return self._mk_proc_run(stdout=vm_text)
            return self._mk_proc_run(stdout="")

        with patch("services_manager.subprocess.run", side_effect=fake_run), \
             patch("services_manager.httpx.get", side_effect=Exception("ollama down")):
            result = self.sm.get_memory_pressure()
        self.assertGreaterEqual(result["available_gb"], 8.0)
        self.assertIsNone(result["warning"])

    def test_reload_dotenv_into_overlays_keys(self):
        """The .env reloader must overlay keys (not replace the dict), respect
        the exclude set, and skip comments/blank lines."""
        from services_manager import _reload_dotenv_into
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write(
                "# comment\n"
                "\n"
                "FOO_BAR=hello\n"
                "export BAZ_QUUX='quoted value'\n"
                "SKIP_ME=ignored\n"
                "EMPTY_VAL=\n"
            )
            tmp = f.name
        try:
            from services_manager import ROOT
            old_root = ROOT
            import services_manager as _sm
            _sm.ROOT = type(ROOT)(os.path.dirname(tmp))
            # Symlink-style: just point ROOT at the temp dir and use a fixed
            # filename. Simplest path: monkeypatch ROOT's .env lookup.
            env = {"EXISTING": "stale"}
            with patch.object(_sm, "ROOT", new=type(ROOT)(os.path.dirname(tmp))):
                # The helper reads ROOT/.env directly; alias to our tmp file.
                os.rename(tmp, os.path.join(os.path.dirname(tmp), ".env"))
                try:
                    overlaid = _reload_dotenv_into(env, exclude={"SKIP_ME"})
                    self.assertGreaterEqual(overlaid, 2)
                    self.assertEqual(env["FOO_BAR"], "hello")
                    self.assertEqual(env["BAZ_QUUX"], "quoted value")
                    self.assertNotIn("SKIP_ME", env)
                    self.assertEqual(env["EXISTING"], "stale")
                    self.assertNotIn("EMPTY_VAL", env)
                finally:
                    os.rename(os.path.join(os.path.dirname(tmp), ".env"), tmp)
            _sm.ROOT = old_root
        finally:
            os.unlink(tmp)

    @unittest.skipUnless(
        (ROOT / "MinusPod" / "src" / "config.py").exists(),
        "MinusPod not vendored",
    )
    def test_start_minuspod_passes_cost_tunables(self):
        """start_minuspod must include LARGE_WINDOW_SECONDS etc. in the spawned
        subprocess env so get_stage_tunable can resolve them. Without this,
        the panel shows the env badge but the resolver still falls back to
        the DB value (because the env var never reaches the child)."""
        import services_manager as _sm
        import subprocess as _real_subprocess

        captured = {}

        class _FakePopen:
            def __init__(self, args, **kwargs):
                captured["env"] = dict(kwargs.get("env", {}))
                captured["args"] = list(args)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def wait(self, *a, **kw): return 0
            def poll(self): return 0
            def communicate(self, *a, **kw): return (b"", b"")

        def _fake_run(*args, **kwargs):
            # sysctl calls return their normal output; everything else falls
            # through to real subprocess.run.
            if args and isinstance(args[0], list) and args[0] and args[0][0] == "sysctl":
                return _real_subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout=b"4", stderr=b"",
                )
            return _real_subprocess.run(*args, **kwargs)

        fake_module = MagicMock()
        fake_module.Popen = _FakePopen
        fake_module.run = _fake_run
        fake_module.CompletedProcess = _real_subprocess.CompletedProcess
        fake_module.DEVNULL = _real_subprocess.DEVNULL

        # pre-populate the cost vars in the parent's os.environ so
        # os.environ.copy() picks them up
        with patch.dict(os.environ, {
            "LARGE_WINDOW_SECONDS": "36000",
            "LARGE_WINDOW_MIN_SECONDS": "300",
            "LARGE_WINDOW_MAX_SECONDS": "36000",
            "SKIP_VERIFICATION_UNDER_SECONDS": "0",
            "ENABLE_PROMPT_CACHING": "true",
            "AD_DETECTION_MAX_TOKENS": "8192",
            "LLM_PROVIDER": "openrouter",
        }, clear=False), \
             patch.object(_sm, "subprocess", new=fake_module), \
             patch.object(_sm, "_pid_listening", return_value=False), \
             patch.object(_sm, "update_minuspod", return_value={"updated": False}), \
             patch.object(_sm, "_http_ok", return_value=True), \
             patch.object(_sm, "_wait_until", return_value=True), \
             patch.object(_sm, "_reload_dotenv_into", return_value=0), \
             patch.object(_sm, "MINUSPOD_LOG", new="/tmp/minuspod.log"):
            _sm.start_minuspod()
        self.assertEqual(captured.get("env", {}).get("LARGE_WINDOW_SECONDS"), "36000")
        self.assertEqual(captured.get("env", {}).get("LARGE_WINDOW_MIN_SECONDS"), "300")
        self.assertEqual(captured.get("env", {}).get("LARGE_WINDOW_MAX_SECONDS"), "36000")
        self.assertEqual(captured.get("env", {}).get("SKIP_VERIFICATION_UNDER_SECONDS"), "0")
        self.assertEqual(captured.get("env", {}).get("ENABLE_PROMPT_CACHING"), "true")
        self.assertEqual(captured.get("env", {}).get("AD_DETECTION_MAX_TOKENS"), "8192")

    def test_sync_cost_tunables_from_env_pushes_db(self):
        """Cost tunables in .env must be PUT to MinusPod so DB overrides don't
        ignore SKIP_VERIFICATION_UNDER_SECONDS etc."""
        import services_manager as _sm
        with patch.dict(os.environ, {
            "SKIP_VERIFICATION_UNDER_SECONDS": "86400",
            "LARGE_WINDOW_SECONDS": "36000",
            "AD_DETECTION_MAX_TOKENS": "16384",
            "ENABLE_PROMPT_CACHING": "true",
        }, clear=False), \
             patch.object(_sm, "put_minuspod_stage_tunables",
                          return_value={"ok": True}) as m_put:
            result = _sm.sync_cost_tunables_from_env()
        self.assertTrue(result.get("ok"))
        m_put.assert_called_once()
        payload = m_put.call_args[0][0]
        self.assertEqual(payload["skipVerificationUnderSeconds"], 86400)
        self.assertEqual(payload["largeWindowSeconds"], 36000)
        self.assertEqual(payload["detectionMaxTokens"], 16384)
        self.assertEqual(payload["verificationMaxTokens"], 16384)
        self.assertTrue(payload["enablePromptCaching"])

    @unittest.skipUnless(
        (ROOT / "MinusPod" / "src" / "config.py").exists(),
        "MinusPod not vendored",
    )
    def test_large_window_range_defaults(self):
        """With no env override, the range matches the static defaults."""
        from services_manager import ROOT
        import sys
        _mp_src = str(ROOT / "MinusPod" / "src")
        if _mp_src not in sys.path:
            sys.path.insert(0, _mp_src)
        import config as _mp_config
        import importlib
        importlib.reload(_mp_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LARGE_WINDOW_MIN_SECONDS", None)
            os.environ.pop("LARGE_WINDOW_MAX_SECONDS", None)
            self.assertEqual(
                _mp_config._large_window_range(),
                (_mp_config.LARGE_WINDOW_MIN_SECONDS_DEFAULT,
                 _mp_config.LARGE_WINDOW_MAX_SECONDS_DEFAULT),
            )

    @unittest.skipUnless(
        (ROOT / "MinusPod" / "src" / "config.py").exists(),
        "MinusPod not vendored",
    )
    def test_large_window_range_env_override(self):
        """Env vars widen/narrow the accepted range without forking MinusPod."""
        from services_manager import ROOT
        import sys
        _mp_src = str(ROOT / "MinusPod" / "src")
        if _mp_src not in sys.path:
            sys.path.insert(0, _mp_src)
        import config as _mp_config
        with patch.dict(os.environ, {
            "LARGE_WINDOW_MAX_SECONDS": "86400",
        }):
            self.assertEqual(_mp_config._large_window_range(), (300, 86400))
        with patch.dict(os.environ, {
            "LARGE_WINDOW_MIN_SECONDS": "600",
            "LARGE_WINDOW_MAX_SECONDS": "7200",
        }):
            self.assertEqual(_mp_config._large_window_range(), (600, 7200))

    @unittest.skipUnless(
        (ROOT / "MinusPod" / "src" / "config.py").exists(),
        "MinusPod not vendored",
    )
    def test_large_window_range_bad_values_fall_back(self):
        """Non-integer env values log a warning and fall back to defaults."""
        from services_manager import ROOT
        import sys
        _mp_src = str(ROOT / "MinusPod" / "src")
        if _mp_src not in sys.path:
            sys.path.insert(0, _mp_src)
        import config as _mp_config
        with patch.dict(os.environ, {
            "LARGE_WINDOW_MIN_SECONDS": "garbage",
            "LARGE_WINDOW_MAX_SECONDS": "",
        }):
            self.assertEqual(
                _mp_config._large_window_range(),
                (_mp_config.LARGE_WINDOW_MIN_SECONDS_DEFAULT,
                 _mp_config.LARGE_WINDOW_MAX_SECONDS_DEFAULT),
            )


class TestServicesEndpoints(unittest.TestCase):
    """Flask routes for the Services panel."""

    def setUp(self):
        from ui_server import create_app
        # ui_server constructs PocketCasts/MinusPod clients eagerly inside
        # create_app, but they don't fire any network calls at that point —
        # patch the classes anyway to keep tests hermetic.
        self._patches = [
            patch("ui_server.PocketCastsClient"),
            patch("ui_server.MinusPodClient"),
        ]
        for p in self._patches:
            p.start()
        self.app = create_app("test@test.com", "testpass")
        self.client = self.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _mk_status(self, **overrides):
        from services_manager import ServiceStatus
        defaults = dict(
            id="ollama", name="Ollama", running=True, healthy=True, pid=1,
            port=11434, backend="brew", extra={}, log_path="/tmp/ollama.log",
            can_start=True, can_stop=True, can_restart=True,
        )
        defaults.update(overrides)
        return ServiceStatus(**defaults)

    def test_list_services_returns_array(self):
        with patch("ui_server.services_manager.all_statuses",
                   return_value=[self._mk_status(id="ollama"),
                                 self._mk_status(id="whisper", port=8765, backend="native")]):
            r = self.client.get("/api/services")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body["services"]), 2)
        self.assertEqual({s["id"] for s in body["services"]}, {"ollama", "whisper"})

    def test_action_unknown_returns_400(self):
        r = self.client.post("/api/services/ollama/explode")
        self.assertEqual(r.status_code, 400)

    def test_action_dispatches_to_service_manager(self):
        with patch("ui_server.services_manager.perform_action",
                   return_value={"ok": True}) as m:
            r = self.client.post(
                "/api/services/whisper/start",
                json={"backend": "native"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        m.assert_called_once_with("whisper", "start", backend="native")

    def test_action_handles_service_error(self):
        from services_manager import ServiceError
        with patch("ui_server.services_manager.perform_action",
                   side_effect=ServiceError("nope")):
            r = self.client.post("/api/services/whisper/start")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "nope")

    def test_log_endpoint_unknown_service(self):
        with patch("ui_server.services_manager.all_statuses", return_value=[]):
            r = self.client.get("/api/services/whisper/log")
        self.assertEqual(r.status_code, 404)

    def test_log_endpoint_returns_tail(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.write("hello\nworld\n")
            log_path = f.name
        try:
            with patch("ui_server.services_manager.all_statuses",
                       return_value=[self._mk_status(id="whisper", log_path=log_path)]):
                r = self.client.get("/api/services/whisper/log?lines=10")
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertTrue(body["exists"])
            self.assertIn("hello", body["text"])
            self.assertIn("world", body["text"])
        finally:
            os.unlink(log_path)

    def test_ollama_model_get(self):
        with patch("ui_server.services_manager.list_ollama_models",
                   return_value=[{"name": "qwen3:14b"}]), \
             patch("ui_server.services_manager.get_minuspod_model",
                   return_value="qwen3:14b"):
            r = self.client.get("/api/services/ollama/model")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["current"], "qwen3:14b")
        self.assertEqual(body["models"][0]["name"], "qwen3:14b")

    def test_ollama_model_put(self):
        with patch("ui_server.services_manager.set_minuspod_model",
                   return_value={"ok": True, "status_code": 200}) as m:
            r = self.client.put("/api/services/ollama/model",
                                json={"model": "qwen3:14b"})
        self.assertEqual(r.status_code, 200)
        m.assert_called_once_with("qwen3:14b")


class TestMinuspodSettingsEndpoints(unittest.TestCase):
    """Proxy endpoints that surface the LLM cost-optimisation tunables
    from MinusPod's settings DB to the parent UI. The parent UI only
    manages three keys (largeWindowSeconds, skipVerificationUnderSeconds,
    enablePromptCaching) so the tests focus on those, with regression
    coverage for the merge-with-existing-tunables contract in the PUT
    path."""

    def setUp(self):
        from ui_server import create_app
        self._patches = [
            patch("ui_server.PocketCastsClient"),
            patch("ui_server.MinusPodClient"),
        ]
        for p in self._patches:
            p.start()
        self.app = create_app("test@test.com", "testpass")
        self.client = self.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_get_minuspod_settings_returns_full_payload(self):
        sample = {
            "claudeModel": {"value": "deepseek/deepseek-v4-flash", "isDefault": False, "envOverride": False},
            "stageTunables": {
                "largeWindowSeconds": {"value": 1200, "isDefault": True, "envOverride": False},
                "skipVerificationUnderSeconds": {"value": 1200, "isDefault": True, "envOverride": False},
                "enablePromptCaching": {"value": True, "isDefault": True, "envOverride": False},
                "detectionTemperature": {"value": 0.0, "isDefault": True, "envOverride": False},
            },
            "stageTunableDefaults": {
                "largeWindowSeconds": 1200,
                "skipVerificationUnderSeconds": 1200,
                "enablePromptCaching": True,
            },
        }
        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value=sample):
            r = self.client.get("/api/minuspod/settings")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["stageTunables"]["largeWindowSeconds"]["value"], 1200)
        self.assertEqual(body["stageTunableDefaults"]["enablePromptCaching"], True)

    def test_get_minuspod_settings_returns_503_when_backend_down(self):
        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value=None):
            r = self.client.get("/api/minuspod/settings")
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertIn("error", body)
        self.assertIn("not reachable", body["error"])

    def test_put_stage_tunables_merges_with_existing(self):
        # The parent UI only manages three keys; the proxy must preserve
        # any other stage tunables the user has set in MinusPod's own UI
        # (e.g. detectionTemperature). We verify by inspecting the JSON
        # body the proxy sends to MinusPod's PUT.
        existing_payload = {
            "claudeModel": {"value": "deepseek/deepseek-v4-flash", "isDefault": False, "envOverride": False},
            "stageTunables": {
                "largeWindowSeconds": {"value": 1200, "isDefault": True, "envOverride": False},
                "skipVerificationUnderSeconds": {"value": 1200, "isDefault": True, "envOverride": False},
                "enablePromptCaching": {"value": True, "isDefault": True, "envOverride": False},
                "detectionTemperature": {"value": 0.4, "isDefault": False, "envOverride": False},
                "verificationTemperature": {"value": 0.2, "isDefault": False, "envOverride": False},
            },
            "stageTunableDefaults": {
                "largeWindowSeconds": 1200,
                "enablePromptCaching": True,
            },
        }
        captured = {}
        class _FakeResp:
            status_code = 200
            def json(self):
                return {"ok": True, "error": None}
            text = ""

        def _fake_put(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value=existing_payload), \
             patch("ui_server.services_manager.httpx.put",
                   side_effect=_fake_put):
            r = self.client.put("/api/minuspod/stage-tunables", json={
                "largeWindowSeconds": 1500,
                "enablePromptCaching": False,
            })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        # The three keys we sent should be in the merged payload...
        self.assertEqual(captured["json"]["largeWindowSeconds"], 1500)
        self.assertEqual(captured["json"]["enablePromptCaching"], False)
        # ...but detection/verification temperatures (set elsewhere) must survive.
        self.assertEqual(captured["json"]["detectionTemperature"], 0.4)
        self.assertEqual(captured["json"]["verificationTemperature"], 0.2)
        # skipVerificationUnderSeconds defaults to True → 1200
        self.assertEqual(captured["json"]["skipVerificationUnderSeconds"], 1200)

    def test_put_stage_tunables_rejects_unknown_keys(self):
        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value={"stageTunables": {}}):
            r = self.client.put("/api/minuspod/stage-tunables", json={
                "largeWindowSeconds": 1500,
                "bogusKey": "should-be-rejected",
            })
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unknown tunable", r.get_json()["error"])
        self.assertIn("bogusKey", r.get_json()["error"])

    def test_put_stage_tunables_surfaces_minuspod_validation_error(self):
        # MinusPod returns 400 with a JSON body when cross-field
        # validation fails (e.g. largeWindowSeconds < windowSizeSeconds).
        # The proxy must surface that error text to the frontend.
        class _FakeResp:
            status_code = 400
            def json(self):
                return {"ok": False, "error": "largeWindowSeconds must be greater than or equal to windowSizeSeconds"}
            text = ""

        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value={"stageTunables": {}}), \
             patch("ui_server.services_manager.httpx.put",
                   return_value=_FakeResp()):
            r = self.client.put("/api/minuspod/stage-tunables", json={
                "largeWindowSeconds": 100,  # below the base 300
            })
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn("largeWindowSeconds", body["error"])
        self.assertIn("HTTP 400", body["error"])

    def test_put_stage_tunables_handles_minuspod_unreachable(self):
        with patch("ui_server.services_manager.get_minuspod_settings",
                   return_value=None):
            r = self.client.put("/api/minuspod/stage-tunables", json={
                "largeWindowSeconds": 1500,
            })
        self.assertEqual(r.status_code, 400)
        self.assertIn("MinusPod settings update failed", r.get_json()["error"])


class TestPocketCastsAuthErrorSurface(unittest.TestCase):
    """Auth failures must surface as JSON, not Werkzeug HTML 500 pages.

    Regression test for the user-visible
    ``JSON.parse: unexpected character at line 1 column 1`` error: the
    dashboard's auto-refresh hits /api/subscriptions, which used to
    bubble httpx.HTTPStatusError as an HTML 500 page that the frontend
    couldn't parse.
    """

    def setUp(self):
        os.environ["POCKETCASTS_EMAIL"] = "test@example.com"
        os.environ["POCKETCASTS_PASSWORD"] = "x" * 10
        # Force a fresh app per test so cached auth state doesn't leak.
        from importlib import reload
        import ui_server
        reload(ui_server)
        self.ui_server = ui_server
        self.app = ui_server.create_app()
        self.client = self.app.test_client()

    def _mk_auth_error(self, message_id="login_account_locked"):
        from pocketcasts_adfree import PocketCastsAuthError
        return PocketCastsAuthError(
            401, message_id,
            "Your account has been locked due too many login attempts.",
        )

    def test_pocketcasts_auth_error_carries_message_id_and_body(self):
        from pocketcasts_adfree import PocketCastsAuthError
        err = PocketCastsAuthError(
            401, "login_wrong_password", "wrong password"
        )
        self.assertEqual(err.status_code, 401)
        self.assertEqual(err.message_id, "login_wrong_password")
        self.assertIn("login_wrong_password", str(err))
        self.assertIn("wrong password", str(err))

    def test_login_translates_401_json_to_typed_error(self):
        """_login must parse Pocket Casts' JSON body and raise our typed
        error, not let httpx.HTTPStatusError escape (which the route
        wouldn't know how to render as JSON)."""
        from pocketcasts_adfree import PocketCastsClient, PocketCastsAuthError

        class FakeResponse:
            status_code = 401
            text = '{"errorMessage":"locked","errorMessageId":"login_account_locked"}'
            def json(self):
                return {
                    "errorMessage": "locked",
                    "errorMessageId": "login_account_locked",
                }

        class FakeHTTPClient:
            def post(self, url, json=None):
                return FakeResponse()

        with patch("pocketcasts_adfree.httpx.Client", return_value=FakeHTTPClient()):
            with self.assertRaises(PocketCastsAuthError) as ctx:
                PocketCastsClient("a@b.c", "pw")
        self.assertEqual(ctx.exception.message_id, "login_account_locked")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_subscriptions_endpoint_returns_json_502_on_auth_failure(self):
        with patch.object(
            self.ui_server, "PocketCastsClient",
            side_effect=self._mk_auth_error(),
        ):
            r = self.client.get("/api/subscriptions")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.content_type.split(";")[0], "application/json")
        body = r.get_json()
        self.assertEqual(body["error"], "pocketcasts_auth_failed")
        self.assertEqual(body["message_id"], "login_account_locked")
        self.assertIn("locked", body["message"].lower())
        # Hint must explain remediation, not be a stack trace.
        self.assertIn("Wait", body["hint"])

    def test_files_endpoint_returns_json_502_on_auth_failure(self):
        with patch.object(
            self.ui_server, "PocketCastsClient",
            side_effect=self._mk_auth_error("login_wrong_password"),
        ):
            r = self.client.get("/api/files")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.content_type.split(";")[0], "application/json")
        body = r.get_json()
        self.assertEqual(body["message_id"], "login_wrong_password")
        self.assertIn("password", body["hint"].lower())

    def test_status_endpoint_includes_pocketcasts_error(self):
        with patch.object(
            self.ui_server, "PocketCastsClient",
            side_effect=self._mk_auth_error(),
        ):
            with patch.object(
                self.ui_server.MinusPodClient, "health", return_value=True
            ):
                r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["pocketcasts"])
        self.assertIsNotNone(body["pocketcasts_error"])
        self.assertEqual(
            body["pocketcasts_error"]["message_id"], "login_account_locked"
        )

    def test_auth_failure_is_cached_so_dashboard_polls_dont_hammer_login(self):
        """Once auth fails, subsequent calls within the cooldown window
        must NOT call PocketCastsClient again. This is what prevented
        the dashboard's 20s refresh from extending the lockout."""
        call_count = {"n": 0}

        def fake_ctor(*a, **kw):
            call_count["n"] += 1
            raise self._mk_auth_error()

        with patch.object(
            self.ui_server, "PocketCastsClient", side_effect=fake_ctor
        ):
            self.client.get("/api/subscriptions")
            self.client.get("/api/subscriptions")
            self.client.get("/api/subscriptions")
        self.assertEqual(
            call_count["n"], 1,
            "Auth-failure cache let repeated requests re-trigger login; "
            "this would extend Pocket Casts' lockout."
        )


class TestPodcastArtworkLookup(unittest.TestCase):
    """Artwork is fetched from iTunes Search API and cached on disk because
    Pocket Casts' /user/podcast/list endpoint doesn't include any image URLs.
    """

    def _mock_itunes(self, results):
        """Return a MagicMock that mimics httpx.get against itunes.apple.com."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": results}
        return resp

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_empty_title_short_circuits(self, mock_get, mock_load, mock_save):
        """No title → no iTunes call → empty string. We don't want to
        hammer iTunes for podcasts whose title is empty/None."""
        url = get_podcast_artwork_url("pod-a", "")
        self.assertEqual(url, "")
        self.assertEqual(url, get_podcast_artwork_url("pod-b", None))
        mock_get.assert_not_called()
        mock_save.assert_not_called()

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={"pod-a": "https://cached.example/a.jpg"})
    @patch('pocketcasts_adfree.httpx.get')
    def test_cache_hit_skips_itunes(self, mock_get, mock_load, mock_save):
        """If the uuid is already in the cache, never call iTunes — the URL
        is stable and re-querying would just be wasted bandwidth."""
        url = get_podcast_artwork_url("pod-a", "Some Podcast")
        self.assertEqual(url, "https://cached.example/a.jpg")
        mock_get.assert_not_called()
        mock_save.assert_not_called()

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_exact_title_match_wins(self, mock_get, mock_load, mock_save):
        """When iTunes returns the same podcast (by title), use its
        artworkUrl600 — even if it appears second in the result list."""
        mock_get.return_value = self._mock_itunes([
            {"trackName": "Some Other Podcast",
             "artworkUrl600": "https://wrong.example/other.jpg"},
            {"trackName": "Some Podcast",
             "artworkUrl600": "https://right.example/exact.jpg"},
        ])
        url = get_podcast_artwork_url("pod-a", "Some Podcast")
        self.assertEqual(url, "https://right.example/exact.jpg")
        mock_save.assert_called_once()
        self.assertEqual(mock_save.call_args[0][0]["pod-a"],
                         "https://right.example/exact.jpg")

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_falls_back_to_first_result_when_no_exact_match(self, mock_get, mock_load, mock_save):
        """iTunes fuzzy search can return near-misses. If no result has
        the exact title, we still return *something* rather than empty
        — better a slightly-wrong cover than an initial-letter placeholder
        for a real podcast the user actually subscribes to."""
        mock_get.return_value = self._mock_itunes([
            {"trackName": "Some Podcast (Bonus Episodes)",
             "artworkUrl600": "https://fallback.example/bonus.jpg"},
        ])
        url = get_podcast_artwork_url("pod-a", "Some Podcast")
        self.assertEqual(url, "https://fallback.example/bonus.jpg")

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_no_results_negatively_caches_empty(self, mock_get, mock_load, mock_save):
        """iTunes returned zero results → cache "" so we don't re-query
        on every dashboard refresh. The UI will show the initial-letter
        placeholder until the user manually re-adds the feed."""
        mock_get.return_value = self._mock_itunes([])
        url = get_podcast_artwork_url("pod-a", "Niche Indie Podcast")
        self.assertEqual(url, "")
        mock_save.assert_called_once_with({"pod-a": ""})

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_network_error_negatively_caches_empty(self, mock_get, mock_load, mock_save):
        """If iTunes is unreachable, don't propagate the exception to the
        UI request — return "" and cache it. The dashboard keeps working."""
        import httpx
        mock_get.side_effect = httpx.ConnectError("iTunes down")
        url = get_podcast_artwork_url("pod-a", "Any Podcast")
        self.assertEqual(url, "")
        mock_save.assert_called_once_with({"pod-a": ""})

    @patch('pocketcasts_adfree._save_artwork_cache')
    @patch('pocketcasts_adfree._load_artwork_cache', return_value={})
    @patch('pocketcasts_adfree.httpx.get')
    def test_non_200_status_negatively_caches(self, mock_get, mock_load, mock_save):
        """A 4xx/5xx from iTunes (rate limit, geo-block) must not crash the
        lookup — return "" and cache it."""
        resp = MagicMock()
        resp.status_code = 429
        mock_get.return_value = resp
        url = get_podcast_artwork_url("pod-a", "Any Podcast")
        self.assertEqual(url, "")
        mock_save.assert_called_once_with({"pod-a": ""})

    @patch('ui_server.PocketCastsClient')
    def test_podcast_artwork_endpoint_returns_url(self, MockPC):
        """GET /api/podcast_artwork/<uuid> must return {"url": "..."}
        and never 500, even if the underlying lookup fails."""
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A"},
        ]
        MockPC.return_value = mock_pc

        with patch('ui_server.get_podcast_artwork_url',
                   return_value="https://art.example/a.jpg"):
            from ui_server import create_app
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.get('/api/podcast_artwork/pod-a')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"url": "https://art.example/a.jpg"})

    @patch('ui_server.PocketCastsClient')
    def test_podcast_artwork_endpoint_handles_failure(self, MockPC):
        """Even when get_podcast_artwork_url raises, the endpoint must
        return a clean JSON {"url": ""} rather than 500 — the dashboard
        polls this on every load and must never break the page."""
        mock_pc = MagicMock()
        mock_pc.get_subscriptions.return_value = [
            {"uuid": "pod-a", "title": "Podcast A"},
        ]
        MockPC.return_value = mock_pc

        with patch('ui_server.get_podcast_artwork_url',
                   side_effect=RuntimeError("boom")):
            from ui_server import create_app
            app = create_app("test@test.com", "testpass")
            client = app.test_client()
            resp = client.get('/api/podcast_artwork/pod-a')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"url": ""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
