#!/usr/bin/env python3
"""
Pocket Casts Ad-Free Automation Pipeline

Downloads podcasts from Pocket Casts subscriptions, removes ads via MinusPod
(local Ollama + whisper.cpp), and uploads ad-free versions to Pocket Casts
custom files for cross-device sync.
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, APIC, CHAP, COMM, CTOC, SYLT, TALB, TCAT, TDES, TIT2, TPE1,
    TDRC, TLEN, TRCK, TXXX, USLT,
)
import xml.etree.ElementTree as ET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pocketcasts-adfree")

POCKETCASTS_API = "https://api.pocketcasts.com"
POCKETCASTS_SHOWNOTES = "https://shownotes.pocketcasts.com"
MINUSPOD_API = "http://localhost:8000"
OLLAMA_API = "http://localhost:11434"
STATE_FILE = Path(__file__).parent / "processed_episodes.json"
USER_PODCAST_UUID = "da7aba5e-f11e-f11e-f11e-da7aba5ef11e"

class _SkippedError(Exception):
    """Raised when an episode is skipped by the user."""


def _normalize_title(title: str) -> str:
    """Normalize a title for fuzzy matching between Pocket Casts and MinusPod.

    Strips punctuation, collapses whitespace, and lowercases so that
    "How to Change the World" matches "How To Change The World!" etc.
    """
    if not title: return ""
    t = title.strip().lower()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _sanitize_published_date(published: str | None) -> str:
    """Return a safe ISO-8601 published date for Pocket Casts uploads.

    Pocket Casts' web player displays "Dec 31, 1969" when it receives an
    epoch-0 date (1970-01-01T00:00:00Z) or an empty / unparseable value.
    We coerce any such value to the current UTC time so episodes always
    show a sensible date across clients.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not published or not isinstance(published, str):
        return now
    p = published.strip()
    if not p:
        return now
    # Reject epoch-0 / pre-2000 dates that display as Dec 31, 1969
    for bad in ("1970", "1969", "0001", "1899"):
        if p.startswith(bad):
            return now
    # Reject "0" or other garbage that isn't ISO-8601-ish
    if not re.match(r"^\d{4}-\d{2}-\d{2}", p):
        return now
    return p


def _normalize_artwork_to_jpeg(image_data: bytes, max_size: int = 1400) -> bytes:
    """Convert artwork to JPEG and resize if needed.

    Pocket Casts' web player reliably handles JPEG images up to ~1400x1400.
    PNG, oversized, or CMYK images can cause display glitches. This normalizes
    everything to a safe baseline.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except ImportError:
        log.warning("  Pillow not installed — artwork will be uploaded as-is")
        return image_data
    except Exception as e:
        log.warning(f"  Artwork normalization failed ({e}) — using original")
        return image_data


def unload_ollama_models():
    """Unload all Ollama models from GPU memory to free resources."""
    try:
        resp = httpx.get(f"{OLLAMA_API}/api/ps", timeout=5)
        for model in resp.json().get("models", []):
            name = model.get("name", "")
            httpx.post(
                f"{OLLAMA_API}/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=10,
            )
            log.info(f"Unloaded Ollama model: {name}")
    except Exception:
        pass


PATREON_INDICATORS = [
    "patreon.com", "patreon", "bonus feed", "premium feed",
    "subscriber feed", "ad-free feed", "member feed",
    "supporters feed", "patron",
]


def is_patreon_feed(podcast: dict) -> bool:
    """Check if a podcast is a Patreon/premium feed (already ad-free)."""
    title = (podcast.get("title") or "").lower()
    url = (podcast.get("url") or podcast.get("feed_url") or "").lower()
    author = (podcast.get("author") or "").lower()
    combined = f"{title} {url} {author}"
    return any(ind in combined for ind in PATREON_INDICATORS)


class PocketCastsClient:
    """Client for the Pocket Casts API (unofficial)."""

    def __init__(self, email: str, password: str):
        self.client = httpx.Client(timeout=120)
        self.token = self._login(email, password)

    def _login(self, email: str, password: str) -> str:
        log.info("Authenticating with Pocket Casts...")
        resp = self.client.post(
            f"{POCKETCASTS_API}/user/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"Login failed: {data}")
        log.info("Authenticated successfully")
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get_subscriptions(self) -> list[dict]:
        resp = self.client.post(
            f"{POCKETCASTS_API}/user/podcast/list",
            headers=self._headers(),
            json={"v": 1},
        )
        resp.raise_for_status()
        return resp.json().get("podcasts", [])

    def get_new_releases(self) -> list[dict]:
        resp = self.client.post(
            f"{POCKETCASTS_API}/user/new_releases",
            headers=self._headers(),
            json={"v": 1},
        )
        resp.raise_for_status()
        return resp.json().get("episodes", [])

    def get_podcast_episodes(self, podcast_uuid: str) -> list[dict]:
        resp = self.client.post(
            f"{POCKETCASTS_API}/user/podcast/episodes",
            headers=self._headers(),
            json={"uuid": podcast_uuid, "v": 1},
        )
        resp.raise_for_status()
        return resp.json().get("episodes", [])

    def get_files(self) -> dict:
        resp = self.client.get(
            f"{POCKETCASTS_API}/files",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_file(self, file_uuid: str) -> dict | None:
        """Fetch a single custom file's metadata."""
        try:
            resp = self.client.get(
                f"{POCKETCASTS_API}/files/{file_uuid}",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def delete_file(self, file_uuid: str) -> bool:
        """Delete an uploaded custom file from Pocket Casts (cloud sweep).

        Uses DELETE /files/{uuid} — discovered by probing the unofficial API.
        Returns True on 2xx, False otherwise. A 404 means the file was already
        removed and is treated as success.
        """
        try:
            resp = self.client.delete(
                f"{POCKETCASTS_API}/files/{file_uuid}",
                headers=self._headers(),
            )
            if 200 <= resp.status_code < 300 or resp.status_code == 404:
                log.info(f"  Deleted Pocket Casts file: {file_uuid[:12]}")
                return True
            log.warning(f"  Delete failed for {file_uuid[:12]}: {resp.status_code}")
            return False
        except Exception as e:
            log.warning(f"  Delete error for {file_uuid[:12]}: {e}")
            return False

    def update_file(self, file_uuid: str, **fields) -> bool:
        """Update an existing custom file's metadata (title, colour, etc.).

        Re-POSTs to /files with the full object — only the fields provided are
        changed, existing ones are preserved.
        """
        current = self.get_file(file_uuid)
        if not current:
            log.warning(f"  update_file: file not found {file_uuid[:12]}")
            return False
        payload = {
            "uuid": current["uuid"],
            "title": fields.get("title", current["title"]),
            "colour": int(fields.get("colour", current.get("colour", 3))),
            "duration": int(fields.get("duration", current["duration"])),
            "size": int(fields.get("size", current["size"])),
            "published": fields.get("published", current["published"]),
            "hasCustomImage": bool(fields.get("hasCustomImage", current.get("hasCustomImage", False))),
            "playedUpTo": int(fields.get("playedUpTo", current.get("playedUpTo", 0))),
            "playingStatus": int(fields.get("playingStatus", current.get("playingStatus", 0))),
        }
        try:
            resp = self.client.post(
                f"{POCKETCASTS_API}/files",
                headers=self._headers(),
                json={"files": [payload]},
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            log.warning(f"  update_file failed: {e}")
            return False

    def mark_file_played(self, file_uuid: str, played: bool = True) -> bool:
        """Mark an uploaded custom file as played (status 3) or unplayed (0)."""
        status = 3 if played else 0
        return self.update_file(file_uuid, playingStatus=status, playedUpTo=0)

    def upload_file(
        self, file_path: Path, title: str, colour: int = 3,
        artwork: bytes = None, published: str = None,
    ) -> str:
        """Upload an audio file to Pocket Casts custom files.

        Protocol (derived from pocket-casts-ios `UploadFileRequestTask` +
        `UploadManager.uploadImageFor`):

          1. POST /files/upload/request with `hasCustomImage=true` and
             `colour=0` when we have artwork. The server uses these two
             fields to decide whether to provision a custom-image S3 slot
             and whether to eventually promote `imageStatus` to 2. Sending
             a non-zero `colour` tells the server "use a tinted placeholder",
             which is why our earlier uploads got stuck at `imageStatus=1`
             even though the JPEG had been uploaded.
          2. PUT the MP3 to the returned presigned URL.
          3. POST /files/upload/image to get the image presigned URL.
          4. PUT the JPEG to that URL.
          5. POST /files to sync title/duration/published metadata.
        """
        audio = MP3(str(file_path))
        duration = int(audio.info.length)
        content_type = "audio/mpeg"
        file_uuid = str(uuid.uuid4())
        file_size = file_path.stat().st_size

        published = _sanitize_published_date(published)

        # Normalize artwork first so we know whether we really have a
        # usable image before telling the server about it.
        if artwork:
            try:
                artwork = _normalize_artwork_to_jpeg(artwork)
            except Exception as e:
                log.warning(f"  Artwork normalization failed: {e}")
                artwork = None

        has_custom_image = bool(artwork)
        # iOS sets `imageColor = 0` whenever a custom image is attached.
        # The upload-image step is gated by this; otherwise the server will
        # treat the file as "use a tinted placeholder" and stall imageStatus
        # at 1 forever.
        effective_colour = 0 if has_custom_image else colour

        log.info(f"Requesting upload URL for '{title}' (duration={duration}s, hasCustomImage={has_custom_image})...")
        resp = self.client.post(
            f"{POCKETCASTS_API}/files/upload/request",
            headers=self._headers(),
            json={
                "uuid": file_uuid,
                "title": title,
                "colour": effective_colour,
                "contentType": content_type,
                "duration": duration,
                "size": file_size,
                "hasCustomImage": has_custom_image,
            },
        )
        resp.raise_for_status()
        upload_url = resp.json()["url"]
        log.info(f"Uploading {file_size / 1e6:.1f} MB...")

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        self.client.put(
            upload_url,
            content=file_bytes,
            headers={"Content-Type": content_type},
            timeout=600,
        ).raise_for_status()
        log.info("File uploaded to cloud storage")

        # Upload the image immediately after the audio, before the
        # /files metadata sync. iOS fires these concurrently but we do
        # them sequentially for simpler error handling.
        if artwork:
            try:
                self.upload_image(file_uuid, artwork, content_type="image/jpeg")
                log.info(f"  Image uploaded ({len(artwork) / 1024:.0f} KB)")
            except Exception as e:
                log.warning(f"  Image upload failed: {e}")

        log.info("Syncing file metadata...")
        self.client.post(
            f"{POCKETCASTS_API}/files",
            headers=self._headers(),
            json={
                "files": [{
                    "uuid": file_uuid,
                    "title": title,
                    "colour": effective_colour,
                    "playedUpTo": 0,
                    "playingStatus": 0,
                    "duration": duration,
                    "size": file_size,
                    "published": published,
                    "hasCustomImage": has_custom_image,
                }]
            },
        ).raise_for_status()

        # Poll imageStatus. 2 means server-side processing finished and
        # the thumbnail will render in Up Next / web player / other clients.
        if artwork:
            for poll in range(24):  # up to ~2 minutes
                time.sleep(5)
                detail = self.get_file(file_uuid)
                if detail and detail.get("imageStatus") == 2:
                    log.info("  Image processed (status=2).")
                    break
            else:
                log.warning(
                    "  Image still at imageStatus=1 after 2 minutes. "
                    "The JPEG is on S3; the server-side copy job will "
                    "usually complete in the background. Use the 'Fix "
                    "stuck thumbnails' action in the UI if it doesn't."
                )

        for attempt in range(10):
            time.sleep(3)
            try:
                status_resp = self.client.get(
                    f"{POCKETCASTS_API}/files/upload/status/{file_uuid}",
                    headers=self._headers(),
                )
                if status_resp.status_code == 200 and status_resp.json().get("success"):
                    log.info(f"Upload confirmed: {file_uuid}")
                    return file_uuid
            except Exception:
                pass
            log.info(f"  Waiting for upload processing... (attempt {attempt + 1})")

        log.warning("Upload status check timed out, file may still be available")
        return file_uuid

    def upload_image(self, file_uuid: str, image_data: bytes, content_type: str = "image/jpeg"):
        """Upload a custom image for a Pocket Casts custom file."""
        resp = self.client.post(
            f"{POCKETCASTS_API}/files/upload/image",
            headers=self._headers(),
            json={"uuid": file_uuid, "contentType": content_type},
        )
        resp.raise_for_status()
        upload_url = resp.json()["url"]

        self.client.put(
            upload_url,
            content=image_data,
            headers={"Content-Type": content_type},
            timeout=60,
        ).raise_for_status()
        log.info(f"  Uploaded custom image ({len(image_data) / 1024:.0f} KB)")

    def reupload_image_from_current(self, file_uuid: str, poll: bool = True) -> dict:
        """Promote a custom file stuck at `imageStatus=1` to status 2.

        Pocket Casts promotes `imageStatus` only when the client performs
        the full upload sequence in order: PUT a JPEG through
        `/files/upload/image`, *then* `POST /files` with
        `hasCustomImage=true` and `colour=0`. The image upload alone or
        the metadata POST alone are not enough (both verified against
        live API).

        We fetch the file's existing `imageUrl` (the JPEG is already
        processed on S3 from the original upload), normalize it, and
        replay the sequence. This works even for files that were
        uploaded before the bug-fix when the initial `/files/upload/request`
        didn't declare `hasCustomImage`.

        Returns a dict with `ok`, `status_before`, `status_after`, `reason`.
        """
        detail = self.get_file(file_uuid)
        if not detail:
            return {"ok": False, "reason": "file not found"}
        status_before = detail.get("imageStatus")
        img_url = detail.get("imageUrl") or detail.get("imageURL")
        if not img_url:
            return {"ok": False, "status_before": status_before,
                    "reason": "no imageUrl on file (never had custom art)"}
        try:
            r = self.client.get(img_url, timeout=30, follow_redirects=True)
            r.raise_for_status()
            raw = r.content
        except Exception as e:
            return {"ok": False, "status_before": status_before,
                    "reason": f"fetch existing image failed: {e}"}
        try:
            jpeg = _normalize_artwork_to_jpeg(raw)
        except Exception:
            jpeg = raw

        try:
            self.upload_image(file_uuid, jpeg, content_type="image/jpeg")
        except Exception as e:
            return {"ok": False, "status_before": status_before,
                    "reason": f"image upload failed: {e}"}

        try:
            self.client.post(
                f"{POCKETCASTS_API}/files",
                headers=self._headers(),
                json={"files": [{
                    "uuid": file_uuid,
                    "title": detail.get("title") or "Untitled",
                    "colour": 0,
                    "playedUpTo": int(detail.get("playedUpTo") or 0),
                    "playingStatus": int(detail.get("playingStatus") or 0),
                    "duration": int(detail.get("duration") or 0),
                    "size": int(detail.get("size") or 0),
                    "published": detail.get("published"),
                    "hasCustomImage": True,
                }]},
            ).raise_for_status()
        except Exception as e:
            return {"ok": False, "status_before": status_before,
                    "reason": f"metadata sync failed: {e}"}

        status_after = status_before
        if poll:
            for _ in range(12):  # up to 60s
                time.sleep(5)
                d = self.get_file(file_uuid)
                status_after = d.get("imageStatus") if d else status_before
                if status_after == 2:
                    break

        return {
            "ok": status_after == 2,
            "status_before": status_before,
            "status_after": status_after,
            "reason": ("promoted to 2" if status_after == 2
                       else f"still at status {status_after}"),
        }

    def _get_up_next_server_modified(self) -> int:
        """Fetch the current serverModified timestamp from the Up Next queue.

        Using the real server timestamp prevents the sync API from interpreting
        our request as a full queue replacement, which would clear all existing
        items. Without this, setting serverModified to "now" can appear newer
        than the server's own timestamp and trigger a destructive overwrite.
        """
        try:
            resp = self.client.post(
                f"{POCKETCASTS_API}/up_next/sync",
                headers=self._headers(),
                json={
                    "deviceTime": int(time.time() * 1000),
                    "version": "2",
                    "upNext": {
                        "serverModified": 0,
                        "changes": [],
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            server_mod = data.get("serverModified") or data.get("upNext", {}).get("serverModified", 0)
            if server_mod:
                return server_mod
        except Exception as e:
            log.debug(f"Could not fetch Up Next server state: {e}")
        return 0

    def add_to_up_next(self, file_uuid: str, title: str, play_last: bool = True):
        """Add an uploaded file to the Up Next queue."""
        action = 3 if play_last else 2  # 3=PLAY_LAST, 2=PLAY_NEXT
        now_ms = int(time.time() * 1000)
        server_modified = self._get_up_next_server_modified()

        request_body = {
            "deviceTime": now_ms,
            "version": "2",
            "upNext": {
                "serverModified": server_modified,
                "changes": [{
                    "action": action,
                    "modified": now_ms,
                    "uuid": file_uuid,
                    "title": title,
                    "podcast": USER_PODCAST_UUID,
                }],
            },
        }

        resp = self.client.post(
            f"{POCKETCASTS_API}/up_next/sync",
            headers=self._headers(),
            json=request_body,
        )
        resp.raise_for_status()
        log.info(f"Added to Up Next: {title}")
        return resp.json()

    def mark_episode_played(self, episode_uuid: str, podcast_uuid: str):
        """Mark a podcast episode as played (status 3)."""
        resp = self.client.post(
            f"{POCKETCASTS_API}/sync/update_episode",
            headers=self._headers(),
            json={
                "uuid": episode_uuid,
                "podcast": podcast_uuid,
                "status": 3,
            },
        )
        resp.raise_for_status()
        log.info(f"  Marked original episode as played: {episode_uuid[:12]}")

    def get_transcript_vtt(self, podcast_uuid: str, episode_uuid: str) -> str | None:
        """Fetch Pocket Casts' generated VTT transcript for an episode."""
        url = f"{POCKETCASTS_SHOWNOTES}/generated_transcripts/{podcast_uuid}/{episode_uuid}.vtt"
        try:
            resp = self.client.get(url, timeout=30)
            if resp.status_code == 200 and resp.text.startswith("WEBVTT"):
                log.info(f"  Got Pocket Casts transcript ({len(resp.text)} chars)")
                return resp.text
        except Exception:
            pass
        return None

    def request_transcript_generation(self, episode_uuid: str):
        """Request Pocket Casts to generate a transcript for an episode."""
        try:
            # This endpoint triggers the server-side generation process
            resp = self.client.post(
                f"{POCKETCASTS_API}/sync/episode_transcript_request",
                headers=self._headers(),
                json={"uuid": episode_uuid},
                timeout=10
            )
            if resp.status_code != 200:
                log.debug(f"  Transcript request status: {resp.status_code}")
        except Exception as e:
            log.debug(f"  Transcript request failed: {e}")

    def get_transcript_vtt_from_rss(self, rss_url: str, ep_title: str) -> str | None:
        """Fetch transcript from RSS feed if available (Podcasting 2.0)."""
        if not rss_url: return None
        try:
            log.info(f"  Checking RSS for transcript: {ep_title}")
            resp = self.client.get(rss_url, timeout=30)
            resp.raise_for_status()
            
            root = ET.fromstring(resp.content)
            namespaces = {
                "podcast": "https://podcastindex.org/namespace/1.0",
                "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"
            }
            
            # Normalize title for matching
            target_title = _normalize_title(ep_title)
            
            for item in root.findall(".//item"):
                title_node = item.find("title")
                if title_node is None: continue
                
                if _normalize_title(title_node.text) == target_title:
                    # Look for podcast:transcript tags
                    transcripts = item.findall("{https://podcastindex.org/namespace/1.0}transcript")
                    # Prefer text/vtt
                    for t in transcripts:
                        if t.attrib.get("type") == "text/vtt":
                            vtt_url = t.attrib.get("url")
                            log.info(f"  Found VTT transcript in RSS: {vtt_url}")
                            vtt_resp = self.client.get(vtt_url, timeout=30)
                            if vtt_resp.status_code == 200:
                                return vtt_resp.text
                    
                    # Fallback to any transcript that we can convert or use (ignoring SRT/TXT for now to be safe)
                    for t in transcripts:
                        if t.attrib.get("type") != "text/vtt":
                             log.debug(f"  Ignore non-VTT transcript: {t.attrib.get('url')}")
            
        except Exception as e:
            log.debug(f"  Error fetching RSS transcript: {e}")
        return None

    def remove_from_up_next(self, episode_uuid: str):
        """Remove an episode from the Up Next queue."""
        now_ms = int(time.time() * 1000)
        server_modified = self._get_up_next_server_modified()
        resp = self.client.post(
            f"{POCKETCASTS_API}/up_next/sync",
            headers=self._headers(),
            json={
                "deviceTime": now_ms,
                "version": "2",
                "upNext": {
                    "serverModified": server_modified,
                    "changes": [{
                        "action": 1,
                        "modified": now_ms,
                        "uuid": episode_uuid,
                    }],
                },
            },
        )
        resp.raise_for_status()
        log.info(f"  Removed original from Up Next: {episode_uuid[:12]}")


class MinusPodClient:
    """Client for the local MinusPod API."""

    def __init__(self, base_url: str = MINUSPOD_API):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))

    def health(self) -> dict:
        resp = self.client.get(f"{self.base_url}/api/v1/health")
        resp.raise_for_status()
        return resp.json()

    def list_feeds(self) -> list[dict]:
        resp = self.client.get(f"{self.base_url}/api/v1/feeds")
        resp.raise_for_status()
        return resp.json().get("feeds", [])

    def add_feed(self, rss_url: str, slug: str = None, max_episodes: int = 5) -> dict:
        body = {"sourceUrl": rss_url, "maxEpisodes": max_episodes}
        if slug:
            body["slug"] = slug
        resp = self.client.post(
            f"{self.base_url}/api/v1/feeds",
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_feed(self, slug: str) -> bool:
        """Delete a feed from MinusPod. Used to reset failed episodes."""
        resp = self.client.delete(f"{self.base_url}/api/v1/feeds/{slug}")
        resp.raise_for_status()
        log.info(f"  Deleted MinusPod feed: {slug}")
        return True

    def get_episodes(self, slug: str, limit: int = 500) -> list[dict]:
        """Return episodes for a feed, up to MinusPod's hard cap of 500.

        Larger limits matter: a user's Up Next queue can include episodes
        older than MinusPod's default 25-item page, and title-based matching
        only works if we actually have those rows loaded.
        """
        resp = self.client.get(
            f"{self.base_url}/api/v1/feeds/{slug}/episodes?limit={limit}"
        )
        resp.raise_for_status()
        return resp.json().get("episodes", [])

    def process_episodes_bulk(self, slug: str, episode_ids: list[str]) -> dict:
        resp = self.client.post(
            f"{self.base_url}/api/v1/feeds/{slug}/episodes/bulk",
            json={"action": "process", "episodeIds": episode_ids},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_status(self) -> dict:
        resp = self.client.get(f"{self.base_url}/api/v1/status")
        resp.raise_for_status()
        return resp.json()

    def wait_for_processing(
        self, slug: str, episode_id: str, timeout: int = 1800,
        progress_callback=None,
    ) -> dict:
        start = time.time()
        last_stage = ""
        while time.time() - start < timeout:
            try:
                st = self.get_status()
                job = st.get("currentJob") or {}
                stage = job.get("stage", "")
                progress = job.get("progress", 0)
                if stage and stage != last_stage:
                    elapsed = job.get("elapsed", 0)
                    msg = f"[{stage}] {progress}% ({elapsed/60:.0f}m elapsed)"
                    log.info(f"  {msg}")
                    if progress_callback:
                        progress_callback(msg)
                    last_stage = stage
            except Exception:
                pass

            try:
                episodes = self.get_episodes(slug)
                for ep in episodes:
                    if ep.get("id") == episode_id or ep.get("episodeId") == episode_id:
                        if ep.get("status") == "completed":
                            log.info("  Episode processing complete!")
                            if progress_callback:
                                progress_callback("Processing complete!")
                            return ep
                        elif ep.get("status") in ("failed", "permanently_failed"):
                            raise RuntimeError(f"Processing failed: {ep.get('error')}")
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(10)
        raise TimeoutError(f"Processing did not complete within {timeout}s")

    def reprocess_episode(self, slug: str, episode_id: str, mode: str = "reprocess") -> dict:
        """Trigger reprocessing for an episode (useful for 410 GONE)."""
        resp = self.client.post(
            f"{self.base_url}/api/v1/feeds/{slug}/episodes/{episode_id}/reprocess",
            json={"mode": mode},
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"  Triggered reprocess for {slug}:{episode_id}")
        return resp.json()

    def get_episode(self, slug: str, episode_id: str) -> dict | None:
        """Return MinusPod's per-episode detail (status, error, ad markers, ...).

        Used by the download-retry loop to detect when MinusPod has marked
        the episode as `failed` / `permanently_failed` so we can abort
        instead of spinning forever on a job that will never complete.
        """
        try:
            resp = self.client.get(
                f"{self.base_url}/api/v1/feeds/{slug}/episodes/{episode_id}",
                timeout=15,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def download_processed_audio(
        self, slug: str, episode_id: str, output_dir: Path,
        max_retries: int = 1000, retry_delay: int = 30,
        skip_event=None, progress_callback=None,
        source_url: str = None,  # kept for backwards-compat callers; unused
    ) -> Path:
        """Download processed audio, retrying on 503 (queue busy/processing).

        The JIT endpoint returns 503 when the episode is being processed or
        the queue is busy. We retry with backoff until the audio is ready.
        If skip_event is set during a retry wait, raises _SkippedError.
        """
        if not slug or slug == '_files':
            raise ValueError(
                "Re-processing custom uploaded files is not supported: "
                "MinusPod processes episodes by feed + episode ID, and custom "
                "files live only in Pocket Casts, not in any RSS feed. "
                "Re-process the original RSS feed episode instead."
            )
        url = f"{self.base_url}/episodes/{slug}/{episode_id}.mp3"
        safe_id = re.sub(r'[^\w-]', '_', episode_id)[:80]
        output_path = output_dir / f"{safe_id}.mp3"
        last_stage = ""
        # Cap how many times we'll ask MinusPod to re-attempt a "GONE" episode.
        # MinusPod itself caps internal retries at 3; if it keeps coming back
        # GONE, the underlying problem (e.g. Whisper backend down) won't fix
        # itself by hammering the reprocess endpoint.
        MAX_REPROCESS_TRIGGERS = 2
        reprocess_count = 0
        # Sample episode-status every N retry iterations to detect a
        # "permanently_failed" verdict without spamming MinusPod.
        STATUS_CHECK_EVERY = 3

        for attempt in range(max_retries):
            if skip_event and skip_event.is_set():
                raise _SkippedError("Skipped by user")

            try:
                # Use a very long timeout (1 hour) for the stream, as Whisper
                # for a 2-hour episode can take 15-20 minutes.
                with self.client.stream("GET", url, timeout=httpx.Timeout(3600)) as resp:
                    if resp.status_code == 410:
                        if reprocess_count >= MAX_REPROCESS_TRIGGERS:
                            err = self._format_episode_failure(slug, episode_id)
                            raise RuntimeError(
                                f"MinusPod gave up on this episode after "
                                f"{reprocess_count} reprocess attempts. {err}"
                            )
                        reprocess_count += 1
                        log.warning(
                            f"  MinusPod returned 410 GONE (job likely failed/expired). "
                            f"Triggering reprocess {reprocess_count}/{MAX_REPROCESS_TRIGGERS}..."
                        )
                        if progress_callback:
                            progress_callback(
                                f"Episode previously failed; asking MinusPod to retry "
                                f"({reprocess_count}/{MAX_REPROCESS_TRIGGERS})..."
                            )
                        try:
                            self.reprocess_episode(slug, episode_id)
                        except Exception as e:
                            log.error(f"  Failed to trigger reprocess: {e}")
                        time.sleep(10)
                        continue
                    if resp.status_code == 503:
                        retry_after = int(resp.headers.get("Retry-After", 10))
                        # Poll MinusPod for detailed processing status
                        try:
                            st = self.get_status()
                            job = st.get("currentJob") or {}
                            stage = job.get("stage", "")
                            progress = job.get("progress", 0)
                            elapsed = job.get("elapsed", 0)
                            if stage and stage != last_stage:
                                msg = f"MinusPod: {stage} ({progress}%, {elapsed/60:.0f}m)"
                                log.info(f"  {msg}")
                                if progress_callback:
                                    progress_callback(msg)
                                last_stage = stage
                            elif attempt == 0:
                                log.info(f"  Episode queued for processing, waiting...")
                                if progress_callback:
                                    progress_callback("Queued for processing...")
                        except Exception:
                            if attempt == 0:
                                log.info(f"  Episode queued for processing, waiting...")

                        # Periodically check the episode-detail endpoint —
                        # `currentJob == null` + `status == failed` means
                        # MinusPod has given up and 503 will never become 200.
                        if attempt > 0 and attempt % STATUS_CHECK_EVERY == 0:
                            ep_detail = self.get_episode(slug, episode_id)
                            if ep_detail and ep_detail.get("status") in (
                                "failed", "permanently_failed",
                            ):
                                err_text = (ep_detail.get("error") or "").strip()
                                # Try a single reprocess if we haven't already
                                if reprocess_count < MAX_REPROCESS_TRIGGERS:
                                    reprocess_count += 1
                                    log.warning(
                                        f"  MinusPod episode is in '{ep_detail['status']}' state "
                                        f"({err_text or 'no error detail'}). Triggering reprocess "
                                        f"{reprocess_count}/{MAX_REPROCESS_TRIGGERS}..."
                                    )
                                    if progress_callback:
                                        progress_callback(
                                            f"MinusPod marked failed: {err_text or 'unknown'}. "
                                            f"Retrying ({reprocess_count}/{MAX_REPROCESS_TRIGGERS})..."
                                        )
                                    try:
                                        self.reprocess_episode(slug, episode_id)
                                    except Exception as e:
                                        log.error(f"  Failed to trigger reprocess: {e}")
                                    time.sleep(10)
                                    continue
                                else:
                                    raise RuntimeError(
                                        f"MinusPod marked episode as '{ep_detail['status']}': "
                                        f"{err_text or 'no error detail provided'}. "
                                        f"Check the MinusPod log (typically /tmp/minuspod.log) "
                                        f"for the underlying cause — common culprits are the "
                                        f"Whisper backend being unreachable or out-of-memory."
                                    )

                        for _ in range(retry_after):
                            if skip_event and skip_event.is_set():
                                raise _SkippedError("Skipped by user")
                            time.sleep(1)
                        continue
                    resp.raise_for_status()
                    with open(output_path, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            if skip_event and skip_event.is_set():
                                raise _SkippedError("Skipped by user")
                            f.write(chunk)
                log.info(f"  Downloaded {output_path.stat().st_size / 1e6:.1f} MB")
                return output_path
            except _SkippedError:
                output_path.unlink(missing_ok=True)
                raise
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 503:
                    time.sleep(retry_delay)
                    continue
                raise

        raise TimeoutError(f"Episode not ready after {max_retries} attempts (approx {max_retries * 10 / 60:.0f} minutes)")

    def _format_episode_failure(self, slug: str, episode_id: str) -> str:
        """Build a human-readable suffix describing why MinusPod failed."""
        ep = self.get_episode(slug, episode_id)
        if not ep:
            return ""
        err = (ep.get("error") or "").strip()
        if not err:
            return ""
        return (
            f"MinusPod reported: '{err}'. "
            f"Check /tmp/minuspod.log for details — common causes: "
            f"Whisper backend not running, audio source 404, OOM."
        )

    def set_fast_system_prompt(self):
        """Replace the system prompt with an improved version tuned for accuracy.

        Balances prompt size (~800 tokens) against detection quality. Key
        improvements over the original ~500-token version:
        - Explicit mid-roll transition pattern recognition
        - Better handling of host-read "native" ads that sound editorial
        - Dynamic Ad Insertion (DAI) markers and network bumpers
        - Evidence quoting requirement to reduce false positives
        """
        fast_prompt = (
            "You are an expert podcast ad detector. Analyze this transcript and find ALL "
            "advertisements. Return ONLY a JSON array.\n\n"
            "## What counts as an AD\n"
            "1. **Sponsor reads** — host or narrator pitching a product/service, including "
            "\"native\" reads that sound conversational (e.g. \"I've been using X and...\")\n"
            "2. **Promo codes & vanity URLs** — any mention of discount codes, special URLs, "
            "or \"use code [X] at checkout\"\n"
            "3. **Platform pre/mid/post-rolls** — inserted by Acast, Spotify, iHeart, Megaphone, "
            "Stitcher, Wondery, SiriusXM, etc. Often start with \"this episode is brought to you by\" "
            "or similar\n"
            "4. **Cross-promotions** — plugs for other podcasts on the same network, with "
            "\"check out\", \"subscribe to\", or \"new episodes every\"\n"
            "5. **Network bumpers/stingers** — short branded intros/outros like \"from Wondery\" "
            "or \"a Spotify original\"\n"
            "6. **Dynamic Ad Insertion (DAI)** — segments that feel tonally different, have "
            "different audio quality, or abruptly change topic to pitch a product\n"
            "7. **Transition phrases** — \"let's take a quick break\", \"we'll be right back\", "
            "\"and now a word from\" — include these IN the ad segment boundaries\n\n"
            "## What is NOT an ad\n"
            "- Guest discussing their own work in an interview context\n"
            "- Host mentioning their own shows/projects organically\n"
            "- Brand names in genuine editorial discussion\n"
            "- Silence or music transitions without promotional content\n\n"
            "## Detection rules\n"
            "- Use exact timestamps from the transcript [Xs] or [HH:MM:SS] markers\n"
            "- Ad boundary starts at the transition phrase, ends when show content resumes\n"
            "- Merge adjacent ads with <15s gaps into one segment\n"
            "- PRE-ROLL: first 90s commonly has platform-inserted ads — flag with high confidence\n"
            "- POST-ROLL: last 60s commonly has outro ads — flag with high confidence\n"
            "- MID-ROLL: look for topic breaks followed by promotional language\n"
            "- When uncertain, include a brief quote from the transcript as evidence\n"
            "- If no ads found, return: []\n\n"
            "## Output format\n"
            'Each ad: {"start": FLOAT, "end": FLOAT, "confidence": 0.0-1.0, '
            '"reason": "brief description", "evidence": "short quote from transcript"}\n\n'
            "Example:\n"
            '[{"start": 0.0, "end": 18.5, "confidence": 0.95, "reason": "Acast platform pre-roll", '
            '"evidence": "this episode is brought to you by..."},\n'
            ' {"start": 312.0, "end": 378.0, "confidence": 0.92, "reason": "BetterHelp sponsor read", '
            '"evidence": "go to betterhelp.com/show for 10% off"}]'
        )

        try:
            self.client.put(
                f"{self.base_url}/api/v1/settings/ad-detection",
                json={"systemPrompt": fast_prompt},
                headers={"Content-Type": "application/json"},
            )
            log.info("Set improved system prompt (~800 tokens)")
        except Exception as e:
            log.warning(f"Could not update system prompt: {e}")

    def lower_confidence_threshold(self):
        """Lower the minimum cut confidence to catch more borderline ads."""
        try:
            self.client.put(
                f"{self.base_url}/api/v1/settings/ad-detection",
                json={"minCutConfidence": 0.65},
                headers={"Content-Type": "application/json"},
            )
            log.info("Lowered min cut confidence to 0.65")
        except Exception as e:
            log.warning(f"Could not update confidence: {e}")

    def disable_auto_process(self):
        """Disable background auto-processing to prevent CPU usage when idle."""
        try:
            self.client.put(
                f"{self.base_url}/api/v1/settings/ad-detection",
                json={"autoProcessEnabled": False},
                headers={"Content-Type": "application/json"},
            )
            log.info("Disabled MinusPod auto-processing")
        except Exception as e:
            log.warning(f"Could not disable auto-process: {e}")

    def pre_populate_transcript(self, slug: str, episode_id: str, vtt_text: str) -> bool:
        """Convert a WEBVTT transcript to MinusPod's format and store it directly.

        This allows MinusPod to skip the Whisper transcription step entirely.
        """
        import sqlite3

        def _parse_vtt_ts(ts_str: str) -> float:
            """Parse VTT timestamp (MM:SS.mmm or HH:MM:SS.mmm) to seconds."""
            parts = ts_str.replace(",", ".").split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(parts[0])

        def _fmt_ts(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = seconds % 60
            return f"{h:02d}:{m:02d}:{s:06.3f}"

        lines = []
        current_start = None
        current_end = None
        current_text = []

        for line in vtt_text.strip().splitlines():
            line = line.strip()
            if line == "WEBVTT" or not line or line.isdigit():
                continue
            ts_match = re.match(
                r'([\d:.]+)\s*-->\s*([\d:.]+)',
                line,
            )
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

        if not lines:
            return False

        transcript_text = "\n".join(lines)
        db_path = Path(__file__).parent / "MinusPod" / "data" / "podcast.db"
        if not db_path.exists():
            log.warning("MinusPod database not found")
            return False

        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            # Find the episode's internal ID
            cur.execute("""
                SELECT e.id FROM episodes e
                JOIN podcasts p ON e.podcast_id = p.id
                WHERE p.slug = ? AND e.episode_id = ?
            """, (slug, episode_id))
            row = cur.fetchone()
            if not row:
                conn.close()
                return False

            ep_db_id = row[0]
            cur.execute("""
                INSERT INTO episode_details (episode_id, transcript_text, original_transcript_text)
                VALUES (?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    transcript_text = COALESCE(episode_details.transcript_text, excluded.transcript_text),
                    original_transcript_text = COALESCE(episode_details.original_transcript_text, excluded.original_transcript_text)
            """, (ep_db_id, transcript_text, transcript_text))
            conn.commit()
            conn.close()
            log.info(f"  Pre-populated transcript ({len(lines)} segments)")
            return True
        except Exception as e:
            log.warning(f"  Failed to pre-populate transcript: {e}")
            return False

    def get_episode_detail(self, slug: str, episode_id: str) -> dict:
        resp = self.client.get(
            f"{self.base_url}/api/v1/feeds/{slug}/episodes/{episode_id}",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_chapters(self, slug: str, episode_id: str) -> list[dict] | None:
        try:
            resp = self.client.get(
                f"{self.base_url}/episodes/{slug}/{episode_id}/chapters.json",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("chapters", [])
        except Exception:
            pass
        return None

    def get_artwork(self, slug: str) -> bytes | None:
        try:
            resp = self.client.get(
                f"{self.base_url}/api/v1/feeds/{slug}/artwork",
                timeout=30,
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None

    def get_feed_info(self, slug: str) -> dict | None:
        try:
            resp = self.client.get(f"{self.base_url}/api/v1/feeds")
            for f in resp.json().get("feeds", []):
                if f["slug"] == slug:
                    return f
        except Exception:
            pass
        return None


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _parse_transcript_to_sylt(transcript_text: str) -> list[tuple]:
    """Convert MinusPod's timestamped transcript to SYLT entries.

    Input format: [HH:MM:SS.mmm --> HH:MM:SS.mmm] text
    Returns: list of (text, timestamp_ms) tuples for SYLT frame.
    """
    entries = []
    for line in transcript_text.strip().splitlines():
        m = re.match(r'\[(\d+):(\d+):(\d+\.\d+)\s*-->', line)
        if m:
            h, mins, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            ms = int((h * 3600 + mins * 60 + s) * 1000)
            text = re.sub(r'^\[.*?\]\s*', '', line).strip()
            if text:
                entries.append((text, ms))
    return entries


def embed_metadata(
    mp3_path: Path,
    mp_client: "MinusPodClient",
    feed_slug: str,
    episode_id: str,
    podcast_title: str = "",
):
    """Embed artwork, description, chapters, and transcript into an MP3 file."""
    try:
        audio = MP3(str(mp3_path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
    except Exception as e:
        log.warning(f"  Could not open MP3 for tagging: {e}")
        return

    detail = {}
    try:
        detail = mp_client.get_episode_detail(feed_slug, episode_id)
    except Exception as e:
        log.warning(f"  Could not fetch episode detail: {e}")

    ep_title = detail.get("title", "")
    description = detail.get("description", "")
    published = detail.get("published", "")
    transcript = detail.get("transcript", "")
    duration_ms = int(detail.get("newDuration", detail.get("duration", 0)) * 1000)

    if not podcast_title:
        feed_info = mp_client.get_feed_info(feed_slug)
        podcast_title = (feed_info or {}).get("title", feed_slug)

    # Basic ID3 tags
    if ep_title:
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=[f"{ep_title} (Ad-Free)"]))
    if podcast_title:
        tags.delall("TALB")
        tags.add(TALB(encoding=3, text=[podcast_title]))
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=[podcast_title]))
    if published and not published.startswith("1970") and not published.startswith("1969"):
        year = published[:10]
        tags.delall("TDRC")
        tags.add(TDRC(encoding=3, text=[year]))
    tags.delall("TCAT")
    tags.add(TCAT(encoding=3, text=["Podcast"]))

    # Episode description as comment
    if description:
        clean_desc = re.sub(r'<[^>]+>', '', description).strip()
        tags.delall("COMM")
        tags.add(COMM(encoding=3, lang="eng", desc="", text=clean_desc))
        try:
            tags.delall("TDES")
            tags.add(TDES(encoding=3, text=[clean_desc]))
        except Exception:
            pass

    # Cover art — normalize to JPEG for compatibility
    artwork = mp_client.get_artwork(feed_slug)
    if artwork:
        artwork = _normalize_artwork_to_jpeg(artwork)
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # Cover (front)
            desc="Cover",
            data=artwork,
        ))
        log.info(f"  Embedded artwork ({len(artwork) / 1024:.0f} KB)")

    # Chapters (ID3 CHAP + CTOC frames)
    chapters = mp_client.get_chapters(feed_slug, episode_id)
    if chapters:
        # Clear existing chapter frames
        tags.delall("CHAP")
        tags.delall("CTOC")

        chap_ids = []
        for i, ch in enumerate(chapters):
            start_ms = int(ch["startTime"] * 1000)
            if i + 1 < len(chapters):
                end_ms = int(chapters[i + 1]["startTime"] * 1000)
            else:
                end_ms = duration_ms or (start_ms + 600_000)
            chap_id = f"chp{i}"
            chap_ids.append(chap_id)
            tags.add(CHAP(
                element_id=chap_id,
                start_time=start_ms,
                end_time=end_ms,
                start_offset=0xFFFFFFFF,
                end_offset=0xFFFFFFFF,
                sub_frames=[TIT2(encoding=3, text=[ch.get("title", f"Chapter {i+1}")])],
            ))
        tags.add(CTOC(
            element_id="toc",
            flags=3,  # top-level + ordered
            child_element_ids=chap_ids,
            sub_frames=[TIT2(encoding=3, text=["Table of Contents"])],
        ))
        log.info(f"  Embedded {len(chapters)} chapters")

    # Transcript as synchronized lyrics (SYLT) and unsynchronized (USLT)
    if transcript:
        tags.delall("USLT")
        plain_text = re.sub(r'\[.*?\]\s*', '', transcript).strip()
        tags.add(USLT(encoding=3, lang="eng", desc="Transcript", text=plain_text))

        sylt_entries = _parse_transcript_to_sylt(transcript)
        if sylt_entries:
            tags.delall("SYLT")
            tags.add(SYLT(
                encoding=3,
                lang="eng",
                format=2,  # milliseconds
                type=1,    # lyrics / transcription
                desc="Transcript",
                text=sylt_entries,
            ))
        log.info(f"  Embedded transcript ({len(transcript)} chars, {len(sylt_entries)} synced entries)")

    try:
        audio.save()
        log.info(f"  Metadata embedded successfully")
    except Exception as e:
        log.warning(f"  Failed to save metadata: {e}")


def _is_rss_url(url: str) -> bool:
    """Heuristic: does this URL look like an actual RSS/Atom feed?"""
    rss_indicators = [
        "/feed", ".rss", ".xml", "/rss",
        "feeds.", "feed.", "anchor.fm", "libsyn",
        "megaphone", "omnycontent", "podbean", "buzzsprout",
        "simplecast", "transistor",
        "podtrac", "feedburner",
    ]
    lower = url.lower()
    if "spreaker.com/" in lower and "/episodes/feed" in lower:
        return True
    return any(ind in lower for ind in rss_indicators)


def _resolve_rss_via_itunes(podcast_title: str) -> str | None:
    """Look up the RSS feed URL via Apple's iTunes Search API."""
    try:
        resp = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": podcast_title, "media": "podcast", "limit": 3},
            timeout=15,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            title_lower = podcast_title.lower().strip()
            for r in results:
                if r.get("trackName", "").lower().strip() == title_lower:
                    return r.get("feedUrl")
            if results:
                return results[0].get("feedUrl")
    except Exception:
        pass
    return None


def find_rss_url_for_podcast(podcast_uuid: str, subscription_data: dict = None, pc=None) -> str | None:
    """Find the RSS feed URL for a Pocket Casts podcast.

    Strategy:
    1. Check if the subscription `url` field is already a valid RSS feed URL.
    2. Convert known platform page URLs (Audioboom, Spreaker) to feed URLs.
    3. Fall back to the iTunes Search API (most reliable source for feed URLs).
    """
    if not subscription_data and pc and podcast_uuid:
        try:
            subs = pc.get_subscriptions()
            subscription_data = next((s for s in subs if s.get("uuid") == podcast_uuid), None)
        except Exception:
            pass

    title = (subscription_data or {}).get("title", "")
    raw_url = (subscription_data or {}).get("url", "")

    if raw_url and _is_rss_url(raw_url):
        return raw_url

    if raw_url:
        if "audioboom.com/channels" in raw_url:
            return raw_url.rstrip("/") + ".rss"

        if "spreaker.com/" in raw_url:
            try:
                resp = httpx.head(raw_url, follow_redirects=True, timeout=10)
                final = str(resp.url)
                import re
                m = re.search(r'--(\d+)', final)
                if m:
                    return f"https://www.spreaker.com/show/{m.group(1)}/episodes/feed"
            except Exception:
                pass

    if title:
        log.info(f"  Looking up RSS via iTunes for: {title}")
        itunes_url = _resolve_rss_via_itunes(title)
        if itunes_url:
            return itunes_url

    try:
        resp = httpx.get(
            f"https://podcast-api.pocketcasts.com/podcast/full/{podcast_uuid}",
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("podcast", {}).get("url")
    except Exception:
        pass
    return None


def _get_audio_summary(url: str) -> dict:
    """Fetch audio duration and metadata using ffprobe.
    
    Resolves redirects via httpx first, since many podcast CDNs
    (podtrac, audioboom, etc.) use redirect chains that ffprobe
    can't always follow.
    """
    if not url:
        return {"duration": 0, "format": ""}
    
    # Resolve redirects to get the actual audio URL
    resolved_url = url
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=15)
        resolved_url = str(resp.url)
    except Exception as e:
        log.debug(f"  Could not resolve URL redirects: {e}")
    
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:format_name",
            "-of", "json", resolved_url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return {
            "duration": float(data.get("format", {}).get("duration", 0)),
            "format": data.get("format", {}).get("format_name", "")
        }
    except Exception as e:
        log.debug(f"  ffprobe failed: {e}")
        return {"duration": 0, "format": ""}


def _transcribe_sample(url: str, start: float, duration: float = 15.0) -> str:
    """Download a small chunk and transcribe it locally for sync verification."""
    root = Path(__file__).parent
    whisper_bin = root / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    model_path = root / "whisper.cpp" / "models" / "ggml-large-v3-turbo.bin"
    
    if not whisper_bin.exists() or not model_path.exists():
        log.debug("  Whisper-cli or model not found, skipping sample transcription")
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        try:
            # 1. Download and convert to 16kHz WAV (required by whisper.cpp)
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-ss", str(max(0, start)), "-t", str(duration),
                "-i", url, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tmp.name
            ]
            subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=60)
            
            # 2. Transcribe
            whisper_cmd = [
                str(whisper_bin), "-m", str(model_path), "-f", tmp.name,
                "-nt", "-l", "en"
            ]
            result = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=60)
            text = result.stdout.strip().lower()
            # Remove timestamps [00:00:00] from whisper-cli output
            text = re.sub(r'\[.*?\]', '', text)
            return _normalize_title(text)
        except Exception as e:
            log.debug(f"  Sample sync check failed: {e}")
            return ""

def _get_vtt_duration(vtt_text: str) -> float:
    """Calculate the total duration covered by a VTT transcript."""
    # Matches both HH:MM:SS.mmm and MM:SS.mmm
    timestamps = re.findall(r'((?:\d+:)?\d+:\d+\.\d+) --> ((?:\d+:)?\d+:\d+\.\d+)', vtt_text)
    if not timestamps: return 0
    
    def to_sec(s):
        parts = s.split(':')
        if len(parts) == 3:
            h, m, sec = parts
            return int(h)*3600 + int(m)*60 + float(sec)
        elif len(parts) == 2:
            m, sec = parts
            return int(m)*60 + float(sec)
        return 0

    _, last_end = timestamps[-1]
    return to_sec(last_end)


def process_single_episode(
    pc: PocketCastsClient,
    mp: MinusPodClient,
    feed_slug: str,
    episode: dict,
    output_dir: Path,
    state: dict,
    progress_callback=None,
    skip_event=None,
    podcast_uuid: str = None,
    original_episode_uuid: str = None,
) -> str | None:
    """Process a single episode via MinusPod JIT and upload to Pocket Casts.

    Uses MinusPod's JIT (Just-In-Time) endpoint which automatically processes
    the episode on-demand when its audio is requested. This is more reliable
    than the explicit queue/bulk endpoints which have race conditions with the
    background auto-processor.

    If skip_event is set during processing, the download is aborted and None
    is returned immediately.

    When podcast_uuid and original_episode_uuid are provided, the original
    episode is marked as played and removed from Up Next after processing.
    """
    ep_id = episode["id"]
    ep_title = episode.get("title", "Unknown")
    ep_status = episode.get("status", "discovered")
    effective_slug = feed_slug if feed_slug else '_files'
    state_key = f"{effective_slug}:{ep_id}"

    # The state check should ONLY trigger if the title in the queue is already (Ad-Free).
    # This allows users to manually re-add "dirty" originals to force a re-process.
    if state_key in state.get("processed", {}) and "(Ad-Free)" in ep_title:
        log.info(f"  Skipping {ep_title} (Already in ad-free state)")
        return None

    if ep_status in ("failed", "permanently_failed"):
        log.warning(f"  Episode '{ep_title}' has status '{ep_status}' in MinusPod. Resetting feed...")
        if progress_callback:
            progress_callback(f"Resetting failed episode (was: {ep_status})...")
        try:
            # Get the feed's source URL before deleting
            feed_info = mp.get_feed_info(feed_slug)
            source_url = feed_info.get("sourceUrl") if feed_info else None
            if source_url:
                mp.delete_feed(feed_slug)
                time.sleep(2)
                result = mp.add_feed(source_url, max_episodes=10)
                new_slug = result.get("slug")
                time.sleep(5)
                # Update feed_slug and re-fetch episodes to get the fresh one
                if new_slug:
                    feed_slug = new_slug
                    new_episodes = mp.get_episodes(feed_slug)
                    # Find the same episode in the refreshed list
                    for new_ep in new_episodes:
                        if _normalize_title(new_ep.get("title", "")) == _normalize_title(ep_title):
                            episode = new_ep
                            ep_id = new_ep["id"]
                            ep_status = new_ep.get("status", "discovered")
                            state_key = f"{feed_slug}:{ep_id}"
                            log.info(f"  Feed reset successful. New status: {ep_status}")
                            break
                    else:
                        log.error(f"  Could not find episode after feed reset")
                        return None
                else:
                    log.error(f"  Feed re-add did not return a slug")
                    return None
            else:
                log.error(f"  Cannot reset: no source URL for feed {feed_slug}")
                return None
        except Exception as e:
            log.error(f"  Feed reset failed: {e}")
            return None

    log.info(f"  Downloading ad-free audio ({ep_status}): {ep_title}")
    if progress_callback:
        progress_callback(f"Downloading/processing: {ep_title}")

    # Pre-populate transcript from Pocket Casts if available (skips Whisper).
    # Requires a real Pocket Casts episode UUID — MinusPod IDs won't work.
    if podcast_uuid and ep_status != "completed":
        if not original_episode_uuid:
            log.info(f"  Could not match episode to Pocket Casts UUID (title matching failed)")
            if progress_callback:
                progress_callback("Could not match to PC episode, will use Whisper for transcript")
        else:
            try:
                # 1. Try Pocket Casts internal transcript
                vtt = pc.get_transcript_vtt(podcast_uuid, original_episode_uuid)
                
                # 2. Try RSS transcript fallback
                if not vtt and podcast_uuid and podcast_uuid != '_files':
                    rss_url = find_rss_url_for_podcast(podcast_uuid, pc=pc)
                    if rss_url:
                        vtt = pc.get_transcript_vtt_from_rss(rss_url, ep_title)
                
                if not vtt:
                    log.info(f"  Transcript not found for {original_episode_uuid[:12]}, requesting generation...")
                    if progress_callback:
                        progress_callback("Transcript not found, requesting generation...")
                    
                    # Request generation and retry
                    for attempt in range(5):
                        pc.request_transcript_generation(original_episode_uuid)
                        time.sleep(10)
                        vtt = pc.get_transcript_vtt(podcast_uuid, original_episode_uuid)
                        if vtt: break
                        log.info(f"  Retry {attempt+1}/5 for transcript...")
                
                if vtt:
                    if progress_callback: progress_callback("Verifying transcript sync...")
                    log.info("  Verifying transcript sync with audio...")
                    
                    # Resolve the actual source audio URL from RSS.
                    # MinusPod episodes don't include the source URL, so we
                    # need to look it up from the RSS feed for sync verification.
                    source_audio_url = episode.get("url", "")
                    if not source_audio_url and podcast_uuid and podcast_uuid not in ('_files', 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e'):
                        try:
                            rss_url = find_rss_url_for_podcast(podcast_uuid, pc=pc)
                            if rss_url:
                                resp = httpx.get(rss_url, timeout=15)
                                import xml.etree.ElementTree as ET
                                root = ET.fromstring(resp.text)
                                ep_norm = _normalize_title(ep_title)
                                for item in root.findall(".//item"):
                                    item_title = item.find("title")
                                    if item_title is not None and _normalize_title(item_title.text or "") == ep_norm:
                                        enclosure = item.find("enclosure")
                                        if enclosure is not None:
                                            source_audio_url = enclosure.get("url", "")
                                            log.info(f"  Resolved source audio URL from RSS")
                                        break
                        except Exception as e:
                            log.debug(f"  Could not resolve audio URL from RSS: {e}")
                    
                    audio_info = _get_audio_summary(source_audio_url)
                    vtt_dur = _get_vtt_duration(vtt)
                    actual_dur = audio_info["duration"]
                    
                    is_synced = False
                    
                    # 1. ALWAYS perform Start-Sync check (15s sample).
                    #    ffmpeg handles redirects natively so _transcribe_sample
                    #    works even when ffprobe can't reach the URL.
                    if progress_callback: progress_callback("Sync check (Start)...")
                    sample_start = _transcribe_sample(source_audio_url, start=0, duration=15)
                    vtt_norm_start = _normalize_title(vtt[:4000])  # First ~4k chars
                    
                    if sample_start:
                        if sample_start in vtt_norm_start:
                            log.info("  START SYNC PASSED: Audio matches transcript start.")
                            is_synced = True
                        else:
                            log.warning(f"  START SYNC FAILED: Heard '{sample_start[:80]}...'")
                            log.warning(f"    Expected (first 80 chars): '{vtt_norm_start[:80]}...'")
                    else:
                        log.warning("  Whisper sample returned empty — trusting transcript as fallback.")
                        is_synced = True

                    # 2. End-check only if start passed AND we have a valid duration
                    if is_synced and actual_dur > 60:
                        discrepancy = abs(actual_dur - vtt_dur)
                        if discrepancy > 15:
                            log.warning(f"  Duration mismatch: Audio {actual_dur:.0f}s vs Transcript {vtt_dur:.0f}s")
                            if progress_callback: progress_callback("Sync check (End)...")
                            sample_end = _transcribe_sample(source_audio_url, start=actual_dur - 30, duration=15)
                            vtt_norm_end = _normalize_title(vtt[-4000:])
                            
                            if sample_end and sample_end not in vtt_norm_end:
                                log.error("  END SYNC FAILED: Audio content mismatch at end.")
                                is_synced = False

                    if is_synced:
                        mp.pre_populate_transcript(feed_slug, ep_id, vtt)
                        if progress_callback:
                            progress_callback("Using Pocket Casts transcript (skipping Whisper)")
                    else:
                        vtt = None # Discard and fall back to Whisper
                        log.info("  SYNC VERIFICATION FAILED. Falling back to full Whisper transcription.")
                        if progress_callback:
                            progress_callback("Sync failed, falling back to Whisper...")

                # Final fallback check: if we have NO vtt (or sync failed), use Whisper
                if not vtt:
                    log.info("  No valid transcript found (or sync failed). Proceeding with Whisper for accurate ad detection.")
                    if progress_callback:
                        progress_callback("Using Whisper for accurate ad detection...")
                
                # Cleanup: we no longer raise error for missing transcripts, we just use Whisper.
            except Exception as e:
                log.error(f"  Transcript error: {e}")
                raise

    try:
        processed_path = mp.download_processed_audio(
            feed_slug, ep_id, output_dir, skip_event=skip_event,
            progress_callback=progress_callback,
        )
    except _SkippedError:
        log.info(f"  Skipped by user: {ep_title}")
        return None
    except Exception as e:
        log.error(f"  Processing failed: {e}")
        # Re-raise so UI server can show the actual error
        raise

    if progress_callback:
        progress_callback("Embedding metadata (artwork, chapters, transcript)...")
    embed_metadata(processed_path, mp, feed_slug, ep_id)

    artwork = mp.get_artwork(feed_slug)
    ep_published = _sanitize_published_date(
        episode.get("published") or episode.get("createdAt") or ""
    )

    upload_title = f"{ep_title} (Ad-Free)"
    if len(upload_title) > 250:
        upload_title = upload_title[:247] + "..."
    if progress_callback:
        size_mb = processed_path.stat().st_size / 1e6
        progress_callback(f"Uploading to Pocket Casts ({size_mb:.1f} MB)...")
    
    # Attempt upload
    file_uuid = pc.upload_file(
        processed_path, upload_title,
        artwork=artwork, published=ep_published,
    )

    if not file_uuid:
        log.error(f"  UPLOAD FAILED: Pocket Casts did not return a UUID for {ep_title}")
        if progress_callback: progress_callback("Upload failed: No UUID returned")
        return None

    pc.add_to_up_next(file_uuid, upload_title, play_last=True)

    # Mark the original episode as played and remove from Up Next
    if podcast_uuid and original_episode_uuid:
        try:
            pc.mark_episode_played(original_episode_uuid, podcast_uuid)
            pc.remove_from_up_next(original_episode_uuid)
            if progress_callback:
                progress_callback("Marked original episode as played")
        except Exception as e:
            log.warning(f"  Could not mark original as played: {e}")

    history_meta: dict = {}
    try:
        detail = mp.get_episode_detail(feed_slug, ep_id) or {}
        original_secs = detail.get("originalDuration") or detail.get("duration")
        new_secs = detail.get("newDuration")
        history_meta = {
            "ads_removed": detail.get("adsRemoved") or detail.get("ad_count"),
            "time_saved_secs": detail.get("timeSaved"),
            "original_duration_secs": original_secs,
            "new_duration_secs": new_secs,
            "original_size": detail.get("originalSize"),
            "new_size": detail.get("fileSize") or processed_path.stat().st_size,
            "podcast_title": (detail.get("podcast") or {}).get("name") if isinstance(detail.get("podcast"), dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  Could not fetch history metadata: {exc}")

    state["processed"][state_key] = {
        "title": ep_title,
        "file_uuid": file_uuid,
        "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **{k: v for k, v in history_meta.items() if v is not None},
    }
    save_state(state)

    processed_path.unlink(missing_ok=True)
    log.info(f"  Done: {ep_title} -> Up Next")
    return file_uuid


def test_single_episode(pc_email, pc_password, rss_url):
    pc = PocketCastsClient(pc_email, pc_password)
    mp = MinusPodClient()
    mp.disable_auto_process()
    mp.set_fast_system_prompt()
    mp.lower_confidence_threshold()

    output_dir = Path(__file__).parent / "processed_audio"
    output_dir.mkdir(exist_ok=True)

    existing_feeds = mp.list_feeds()
    feed_slug = None
    for f in existing_feeds:
        if f.get("sourceUrl") == rss_url:
            feed_slug = f["slug"]
            break

    if not feed_slug:
        result = mp.add_feed(rss_url, max_episodes=3)
        feed_slug = result.get("slug")
        time.sleep(5)

    episodes = mp.get_episodes(feed_slug)
    if not episodes:
        log.error("No episodes found")
        return

    state = load_state()
    target = episodes[0]
    file_uuid = process_single_episode(pc, mp, feed_slug, target, output_dir, state)

    if file_uuid:
        log.info(f"\nPIPELINE COMPLETE - uploaded and queued in Up Next")


def run_automation(pc_email, pc_password, rss_urls=None, podcast_filter=None):
    pc = PocketCastsClient(pc_email, pc_password)
    mp = MinusPodClient()
    mp.disable_auto_process()
    mp.set_fast_system_prompt()
    mp.lower_confidence_threshold()
    state = load_state()
    output_dir = Path(__file__).parent / "processed_audio"
    output_dir.mkdir(exist_ok=True)

    if rss_urls:
        for rss_url in rss_urls:
            existing = mp.list_feeds()
            if not any(f.get("sourceUrl") == rss_url for f in existing):
                mp.add_feed(rss_url, max_episodes=10)
                time.sleep(3)

    feeds = mp.list_feeds()
    if podcast_filter:
        fl = [f.lower() for f in podcast_filter]
        feeds = [f for f in feeds if any(x in f.get("title", "").lower() for x in fl)]

    for feed in feeds:
        slug = feed["slug"]
        log.info(f"\nProcessing feed: {feed.get('title', slug)}")
        for ep in mp.get_episodes(slug):
            process_single_episode(pc, mp, slug, ep, output_dir, state)

    log.info("\nAutomation run complete!")


def main():
    parser = argparse.ArgumentParser(description="Pocket Casts Ad-Free Pipeline")
    parser.add_argument("command", choices=["test", "auto", "ui"])
    parser.add_argument("--email", default=os.environ.get("POCKETCASTS_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("POCKETCASTS_PASSWORD"))
    parser.add_argument("--rss-url", action="append", dest="rss_urls")
    parser.add_argument("--filter", action="append", dest="filters")
    parser.add_argument("--port", type=int, default=5050)

    args = parser.parse_args()

    if args.command == "ui":
        from ui_server import create_app
        app = create_app(args.email, args.password)
        app.run(host="0.0.0.0", port=args.port, debug=False)
        return

    if not args.email or not args.password:
        print("Set POCKETCASTS_EMAIL and POCKETCASTS_PASSWORD, or use --email/--password")
        sys.exit(1)

    if args.command == "test":
        if not args.rss_urls:
            print("--rss-url required for test mode")
            sys.exit(1)
        test_single_episode(args.email, args.password, args.rss_urls[0])
    elif args.command == "auto":
        run_automation(args.email, args.password, args.rss_urls, args.filters)


if __name__ == "__main__":
    main()
