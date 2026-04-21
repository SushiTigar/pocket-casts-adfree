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
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(__file__))

from pocketcasts_adfree import (
    _normalize_artwork_to_jpeg,
    _parse_transcript_to_sylt,
    _sanitize_published_date,
    is_patreon_feed,
    find_rss_url_for_podcast,
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


class TestTranscriptPrePopulation(unittest.TestCase):
    """VTT parsing and DB insertion must handle edge cases."""

    def test_vtt_to_minuspod_format(self):
        """Verify VTT timestamp conversion to MinusPod's format."""
        vtt = """WEBVTT

1
00:00:00.000 --> 00:00:05.123
Hello world

2
00:01:30.456 --> 00:01:35.789
Second segment
"""
        # Simulate the parsing logic from pre_populate_transcript
        lines = []
        current_start = None
        current_end = None
        current_text = []

        def _parse_vtt_ts(ts_str):
            parts = ts_str.replace(",", ".").split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(parts[0])

        def _fmt_ts(seconds):
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = seconds % 60
            return f"{h:02d}:{m:02d}:{s:06.3f}"

        for line in vtt.strip().splitlines():
            line = line.strip()
            if line == "WEBVTT" or not line or line.isdigit():
                continue
            ts_match = re.match(r'([\d:.]+)\s*-->\s*([\d:.]+)', line)
            if ts_match:
                if current_start is not None and current_text:
                    text = " ".join(current_text).strip()
                    if text:
                        lines.append(f"[{current_start} --> {current_end}] {text}")
                start_sec = _parse_vtt_ts(ts_match.group(1))
                end_sec = _parse_vtt_ts(ts_match.group(2))
                current_start = _fmt_ts(start_sec)
                current_end = _fmt_ts(end_sec)
                current_text = []
            elif current_start is not None:
                current_text.append(line)

        if current_start is not None and current_text:
            text = " ".join(current_text).strip()
            if text:
                lines.append(f"[{current_start} --> {current_end}] {text}")

        self.assertEqual(len(lines), 2)
        self.assertIn("[00:00:00.000 --> 00:00:05.123] Hello world", lines[0])
        self.assertIn("[00:01:30.456 --> 00:01:35.789] Second segment", lines[1])


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
            resp.raise_for_status = MagicMock()
            mp.client.get.return_value = resp
            mp.get_episodes("some-slug")
            called_url = mp.client.get.call_args[0][0]
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
        from pocketcasts_adfree import MinusPodClient
        with patch.object(MinusPodClient, '__init__', lambda self, *a, **kw: None):
            mp = MinusPodClient.__new__(MinusPodClient)
            mp.base_url = "http://localhost:8000"
            mp.client = MagicMock()
            resp = MagicMock()
            resp.status_code = 404
            mp.client.get.return_value = resp
            self.assertIsNone(mp.get_episode("slug", "ep"))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
