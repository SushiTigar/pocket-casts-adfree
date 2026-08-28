#!/usr/bin/env python3
"""
Pocket Casts Ad-Free Automation Pipeline

Downloads podcasts from Pocket Casts subscriptions, removes ads via MinusPod
(local Ollama + whisper.cpp), and uploads ad-free versions to Pocket Casts
custom files for cross-device sync.
"""

import argparse
import difflib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
}


def normalize_feed_url(url: str) -> str:
    """Normalize a feed URL for equality comparisons.

    Strips trailing slashes, lowercases the host, drops common tracking
    query params, and normalizes the scheme (http -> https). Used to
    match Pocket Casts subscription RSS URLs against URLs already
    stored in MinusPod, which often differ only in trivial formatting.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url.strip())
        scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parts.path.rstrip("/") or "/"
        query_pairs = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_QUERY_KEYS
        ]
        query_pairs.sort()
        query = urlencode(query_pairs)
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url.strip().rstrip("/")


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


def _normalize_title_strong(title: str) -> str:
    """Stronger title normalization for cross-source matching (e.g. RSS vs PC).

    Used by the post-upload title-match sweep where one source is the
    MinusPod RSS feed (often includes smart quotes, em-dashes, season tags,
    HTML entities) and the other is the Pocket Casts queue (often uses
    straight ASCII). Beyond :func:`_normalize_title`, this:

    * Decodes HTML entities (``&`` -> ``&``).
    * Unicode-normalizes (NFKD) and ASCII-folds so ``café``, ``café``
      and ``cafe`` all collapse to ``cafe``.
    * Strips common parenthetical season/episode tags: ``(S2 E5)``,
      ``(Season 2 Episode 5)``, ``(S02E05)``, ``(Pt. 1)``.
    * Folds ``Pt.`` -> ``Part``, ``Ep.`` -> ``Episode``, ``#`` -> ``No.``
      so partial-numbering differences match.

    Falls back to :func:`_normalize_title` if the underlying libraries
    aren't available or input is invalid.
    """
    if not title:
        return ""
    try:
        import html as _html
        import unicodedata as _ud
        s = _html.unescape(str(title))
        s = _ud.normalize("NFKD", s)
        s = s.encode("ascii", "ignore").decode("ascii")
    except Exception:
        s = str(title)
    s = re.sub(r"\((?:[^)]*?(?:s\s*\d+\s*e\s*\d+|season|episode|ep\.?|pt\.?|part|no\.?|\bno\b)[^)]*?)\)", " ", s, flags=re.I)
    s = re.sub(r"\[(?:\d+|s\s*\d+\s*e\s*\d+|season[^]]*|episode[^]]*|part[^]]*)\]", " ", s, flags=re.I)
    s = re.sub(r"\bpt\.?\b", "part", s, flags=re.I)
    s = re.sub(r"\bep\.?\b", "episode", s, flags=re.I)
    s = re.sub(r"\bno\.?\s*(\d)", r"number \1", s, flags=re.I)
    s = re.sub(r"#(\d)", r"number \1", s)
    return _normalize_title(s)


def _normalize_episode_url(url: str) -> str:
    """Normalize an episode audio URL for cross-source matching."""
    if not url:
        return ""
    try:
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(url.strip()).path)
        return path.rsplit("/", 1)[-1].lower()
    except Exception:
        return url.strip().lower()


def resolve_pc_episode_uuid(
    title: str,
    episodes: list[dict],
    *,
    audio_url: str = "",
    duration: float = 0,
    duration_tolerance: float = 5.0,
) -> str | None:
    """Resolve a Pocket Casts episode UUID from catalog metadata.

    Tries, in order: strong title match, audio URL basename, basic title
    match/substring, then duration disambiguation.
    """
    if not episodes:
        return None

    title_strong = _normalize_title_strong(title)
    title_basic = _normalize_title(title)
    url_key = _normalize_episode_url(audio_url)

    for ep in episodes:
        ep_title = ep.get("title") or ""
        eu = ep.get("uuid")
        if ep_title and eu and _normalize_title_strong(ep_title) == title_strong:
            return eu

    if url_key:
        for ep in episodes:
            ep_url = ep.get("url") or ""
            eu = ep.get("uuid")
            if not eu or not ep_url:
                continue
            if _normalize_episode_url(ep_url) == url_key or url_key in ep_url.lower():
                return eu

    for ep in episodes:
        ep_title = ep.get("title") or ""
        eu = ep.get("uuid")
        if ep_title and eu and _normalize_title(ep_title) == title_basic:
            return eu

    if title_basic:
        for ep in episodes:
            ep_title = ep.get("title") or ""
            eu = ep.get("uuid")
            if not ep_title or not eu:
                continue
            ep_norm = _normalize_title(ep_title)
            if title_basic in ep_norm or ep_norm in title_basic:
                return eu

    if duration > 0:
        candidates = []
        for ep in episodes:
            eu = ep.get("uuid")
            if not eu:
                continue
            try:
                ep_dur = float(ep.get("duration") or 0)
            except (TypeError, ValueError):
                continue
            if ep_dur > 0 and abs(ep_dur - duration) <= duration_tolerance:
                candidates.append(ep)
        if len(candidates) == 1:
            return candidates[0].get("uuid")
        if candidates and title_strong:
            for ep in candidates:
                ep_title = ep.get("title") or ""
                eu = ep.get("uuid")
                if not ep_title or not eu:
                    continue
                ep_norm = _normalize_title_strong(ep_title)
                if title_strong in ep_norm or ep_norm in title_strong:
                    return eu

    return None


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


def _is_transcription_failure(err_text: str) -> bool:
    """Heuristic: does this MinusPod error suggest the Whisper backend is sick?

    The Metal backend on macOS occasionally crashes with
    `kIOGPUCommandBufferCallbackErrorInnocentVictim` when another GPU
    workload (Safari, Xcode, another whisper instance) preempts it. Once
    that happens the whisper-server keeps accepting requests but every
    inference returns 500. MinusPod surfaces this as
    "Failed to transcribe audio" or similar.
    """
    if not err_text:
        return False
    needle = err_text.lower()
    return any(s in needle for s in (
        "failed to transcribe",
        "transcription failed",
        "transcribe audio",
        "whisper",
        "metal",
        "gpu",
    ))


def _restart_whisper_if_wedged() -> bool:
    """Restart whisper-server in-place. Returns True on success.

    Lazy-imports `services_manager` to avoid a circular dependency between
    pipeline code and the Flask UI module. Falls back to no-op if the
    service helpers aren't available (e.g. running in CLI-only mode where
    services_manager hasn't been initialised — restarting whisper there
    is the user's job anyway).
    """
    try:
        import services_manager
    except Exception:
        return False
    try:
        result = services_manager.restart_whisper(backend="native")
        return bool(result.get("ok"))
    except Exception as exc:
        log.warning(f"  Whisper restart failed: {exc}")
        return False


def _restart_ollama_if_wedged() -> bool:
    """Restart Ollama when ad-detection LLM calls are hung. Returns True on success."""
    try:
        import services_manager
    except Exception:
        return False
    try:
        result = services_manager.restart_ollama()
        return bool(result.get("ok"))
    except Exception as exc:
        log.warning(f"  Ollama restart failed: {exc}")
        return False


def _restart_minuspod_if_wedged() -> bool:
    """Restart MinusPod when cloud-LLM ad detection is hung. Returns True on success."""
    try:
        import services_manager
    except Exception:
        return False
    try:
        result = services_manager.restart_minuspod()
        return bool(result.get("ok"))
    except Exception as exc:
        log.warning(f"  MinusPod restart failed: {exc}")
        return False


def _llm_provider_uses_ollama() -> bool:
    return os.environ.get("LLM_PROVIDER", "ollama") == "ollama"


def _reset_orphaned_episode_in_db(slug: str, episode_id: str) -> bool:
    """Clear an episode stuck in ``processing`` with no active MinusPod worker."""
    db_path = Path(__file__).parent / "MinusPod" / "data" / "podcast.db"
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            """UPDATE episodes SET status='discovered'
               WHERE episode_id=? AND status='processing'
               AND podcast_id IN (SELECT id FROM podcasts WHERE slug=?)""",
            (episode_id, slug),
        )
        conn.commit()
        updated = cur.rowcount > 0
        conn.close()
        return updated
    except Exception as exc:
        log.error(f"  Failed to reset orphaned episode in DB: {exc}")
        return False


def _reset_stuck_episode_in_db(slug: str, episode_id: str) -> tuple[bool, str]:
    """Reset any stuck MinusPod episode (``processing``/``failed``/``permanently_failed``)
    back to ``discovered`` so ``reprocess_episode`` can re-queue it.

    Unlike ``_reset_orphaned_episode_in_db`` (which only matches the orphaned
    ``processing`` state surfaced during the in-loop recovery), this covers
    the user-facing reset button that has to clear episodes MinusPod has
    permanently given up on.

    Returns ``(ok, previous_status)``. ``previous_status`` is one of
    ``"processing" | "failed" | "permanently_failed" | "not_stuck" | "db_missing" | "db_error"``.
    """
    db_path = Path(__file__).parent / "MinusPod" / "data" / "podcast.db"
    if not db_path.exists():
        return False, "db_missing"
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            """SELECT status FROM episodes
               WHERE episode_id=? AND status IN ('processing','failed','permanently_failed')
               AND podcast_id IN (SELECT id FROM podcasts WHERE slug=?)""",
            (episode_id, slug),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return False, "not_stuck"
        previous_status = row[0]
        conn.execute(
            """UPDATE episodes SET status='discovered'
               WHERE episode_id=?
               AND podcast_id IN (SELECT id FROM podcasts WHERE slug=?)""",
            (episode_id, slug),
        )
        conn.commit()
        conn.close()
        return True, previous_status
    except Exception as exc:
        log.error(f"  Failed to reset stuck episode in DB: {exc}")
        return False, "db_error"


def _is_llm_stage(stage: str) -> bool:
    if not stage:
        return False
    s = stage.lower()
    return "detecting" in s or "llm" in s or "pass2" in s or "verifying" in s


def _is_transcription_stage(stage: str) -> bool:
    if not stage:
        return False
    s = stage.lower()
    return "transcribing" in s or "whisper" in s


def _stall_threshold_for_stage(stage: str, base_threshold: int) -> int:
    """LLM ad detection stages get a 3x threshold multiplier compared to Whisper."""
    if _is_llm_stage(stage):
        return base_threshold * 3
    return base_threshold


def _bounce_service_for_stall(stage: str) -> tuple[bool, str]:
    """Restart the backend most likely wedged for this MinusPod stage."""
    if _is_transcription_stage(stage):
        return _restart_whisper_if_wedged(), "whisper-server"
    if _llm_provider_uses_ollama():
        return _restart_ollama_if_wedged(), "Ollama"
    # Cloud LLM (OpenRouter, etc.) — Ollama is not involved; bounce MinusPod.
    return _restart_minuspod_if_wedged(), "MinusPod"


def _list_up_next_episodes(pc) -> list[dict]:
    """Fetch the current Up Next list using a pull-only sync (no changes).

    Helper used by `process_single_episode` for the post-upload title-match
    sweep. Mirrors the dashboard's `_get_up_next_episodes` so behaviour is
    consistent between the background pipeline and the on-demand reconciler.
    """
    try:
        resp = pc.client.post(
            f"{POCKETCASTS_API}/up_next/sync",
            headers=pc._headers(),
            json={
                "deviceTime": int(time.time() * 1000),
                "version": "2",
                "upNext": {"serverModified": 0, "changes": []},
            },
        )
        resp.raise_for_status()
        return resp.json().get("episodes", []) or []
    except Exception:
        return []


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


class PocketCastsAuthError(RuntimeError):
    """Raised when /user/login fails. Carries the upstream message id and
    body so callers (Flask routes, CLI) can surface a useful explanation
    instead of a generic 500 / opaque HTTPStatusError.

    Attributes
    ----------
    status_code:
        HTTP status from Pocket Casts (typically 401).
    message_id:
        Pocket Casts' machine-readable code, e.g. ``login_account_locked``,
        ``login_wrong_password``, ``login_email_unknown``. Empty string if
        the response wasn't JSON.
    upstream_message:
        Human-readable text from Pocket Casts' JSON body, e.g. "Your
        account has been locked due to too many login attempts...".
    """

    def __init__(self, status_code: int, message_id: str, upstream_message: str):
        self.status_code = status_code
        self.message_id = message_id
        self.upstream_message = upstream_message
        suffix = f" [{message_id}]" if message_id else ""
        super().__init__(
            f"Pocket Casts login failed (HTTP {status_code}){suffix}: "
            f"{upstream_message or 'no detail provided'}"
        )


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
        if resp.status_code >= 400:
            # Pocket Casts returns JSON like
            #   {"errorMessage":"...","errorMessageId":"login_account_locked"}
            # Surface those instead of the opaque httpx.HTTPStatusError so
            # the dashboard can show "account locked, try again later"
            # rather than "JSON.parse: unexpected character" (the frontend
            # was choking on Flask's HTML 500 page).
            msg_id = ""
            msg = ""
            try:
                body = resp.json()
                msg_id = (body.get("errorMessageId") or "").strip()
                msg = (body.get("errorMessage") or "").strip()
            except Exception:
                msg = (resp.text or "").strip()[:200]
            raise PocketCastsAuthError(resp.status_code, msg_id, msg)
        data = resp.json()
        token = data.get("token")
        if not token:
            raise PocketCastsAuthError(
                resp.status_code, "", f"login response missing token: {data}"
            )
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

    def get_podcast_episodes_catalog(self, podcast_uuid: str) -> list[dict]:
        """Full episode metadata (title, url, uuid) from Pocket Casts CDN."""
        try:
            resp, _ = _httpx_request_public(
                "GET",
                f"https://podcast-api.pocketcasts.com/podcast/full/{podcast_uuid}",
                timeout=30,
            )
            if resp.status_code == 200:
                episodes = resp.json().get("podcast", {}).get("episodes") or []
                log.debug(
                    f"  Loaded {len(episodes)} episodes from PC catalog "
                    f"for {podcast_uuid[:12]}"
                )
                return episodes
        except Exception as e:
            log.debug(f"  Episode catalog fetch failed: {e}")
        return []

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

    def add_to_up_next(
        self, file_uuid: str, title: str, play_last: bool = True,
        published: str | None = None,
    ):
        """Add an uploaded file to the Up Next queue.

        `published` is forwarded so Pocket Casts clients display the real
        episode date instead of "Dec 31, 1969". The /up_next/sync endpoint
        treats omitted/empty `published` as epoch 0, which the official
        Pocket Casts apps render as 1969-12-31. The /files endpoint gets
        the same value during upload, but Up Next is a separate cache —
        without this field here, the Up Next entry shows the epoch date
        even though the file's own metadata is correct.
        """
        action = 3 if play_last else 2  # 3=PLAY_LAST, 2=PLAY_NEXT
        now_ms = int(time.time() * 1000)
        server_modified = self._get_up_next_server_modified()

        change: dict = {
            "action": action,
            "modified": now_ms,
            "uuid": file_uuid,
            "title": title,
            "podcast": USER_PODCAST_UUID,
        }
        sanitized = _sanitize_published_date(published) if published else None
        if sanitized:
            change["published"] = sanitized

        request_body = {
            "deviceTime": now_ms,
            "version": "2",
            "upNext": {
                "serverModified": server_modified,
                "changes": [change],
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
                        "action": 4,
                        "modified": now_ms,
                        "uuid": episode_uuid,
                    }],
                },
            },
        )
        resp.raise_for_status()
        log.info(f"  Removed original from Up Next: {episode_uuid[:12]}")

    def replace_in_up_next(
        self,
        file_uuid: str,
        upload_title: str,
        original_uuid: str | None,
        published: str | None = None,
    ):
        """Atomically add an uploaded file to Up Next and remove the original.

        Pocket Casts' ``/up_next/sync`` endpoint processes a single diff
        against ``serverModified``. Sending the add + remove in two
        separate sync requests introduces a race window where the
        server has applied the add but not yet persisted the new
        ``serverModified``, causing the second ``_get_up_next_server_modified``
        probe to return stale data and the remove to be silently rejected.

        Batching the changes in one request avoids that race and uses a
        single network round-trip.

        If ``original_uuid`` is falsy, only the add is sent (caller will
        fall back to the title-match sweep).
        """
        action = 3  # PLAY_LAST
        now_ms = int(time.time() * 1000)
        server_modified = self._get_up_next_server_modified()

        changes: list[dict] = []
        add_change: dict = {
            "action": action,
            "modified": now_ms,
            "uuid": file_uuid,
            "title": upload_title,
            "podcast": USER_PODCAST_UUID,
        }
        sanitized = _sanitize_published_date(published) if published else None
        if sanitized:
            add_change["published"] = sanitized
        changes.append(add_change)

        if original_uuid:
            changes.append({
                "action": 4,  # REMOVE
                "modified": now_ms,
                "uuid": original_uuid,
            })

        resp = self.client.post(
            f"{POCKETCASTS_API}/up_next/sync",
            headers=self._headers(),
            json={
                "deviceTime": now_ms,
                "version": "2",
                "upNext": {
                    "serverModified": server_modified,
                    "changes": changes,
                },
            },
        )
        resp.raise_for_status()
        log.info(
            f"  Replace in Up Next: +{upload_title[:40]} "
            f"({file_uuid[:12]})"
            + (f" -orig({original_uuid[:12]})" if original_uuid else "")
        )
        return resp.json()


def _retry_up_next(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Retry helper for transient /up_next/sync failures.

    Pocket Casts occasionally returns 5xx or drops the connection when
    the sync endpoint is under load (e.g. immediately after a file
    upload, the file-upload worker and the up-next worker can race).
    Without a retry, a single transient failure leaves the user with
    an inconsistent queue — Ad-Free file in Up Next but original also
    still queued. We retry with exponential backoff before giving up.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            delay = base_delay * (2 ** i)
            log.debug(
                f"  Up Next op failed (attempt {i + 1}/{attempts}): {exc}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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
        if resp.status_code == 409:
            log.info(f"  Feed already exists (409): {rss_url}")
            error_msg = ""
            try:
                body = resp.json()
                error_msg = body.get("error", "")
                if body.get("slug"):
                    return body
            except Exception:
                pass
            slug_match = re.search(r'slug "([^"]+)"', error_msg)
            if slug_match:
                existing_slug = slug_match.group(1)
                existing = self.list_feeds()
                for f in existing:
                    if f.get("slug") == existing_slug:
                        return f
            existing = self.list_feeds()
            target = normalize_feed_url(rss_url)
            for f in existing:
                if normalize_feed_url(f.get("sourceUrl", "")) == target:
                    return f
            return {"slug": None, "sourceUrl": rss_url, "already_exists": True}
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
        """Trigger reprocessing for an episode (useful for 410 GONE).

        Returns a dict with an ``already_processing`` key set to ``True`` when
        MinusPod responds 409 (the episode is still being worked on internally).
        In that case the caller should keep polling rather than counting a retry.
        """
        resp = self.client.post(
            f"{self.base_url}/api/v1/feeds/{slug}/episodes/{episode_id}/reprocess",
            json={"mode": mode},
            timeout=30,
        )
        if resp.status_code == 409:
            # 409 Conflict — MinusPod is still actively processing this episode.
            # This is NOT a failed reprocess; the episode is healthy, just busy.
            log.info(
                f"  Reprocess 409 for {slug}:{episode_id} — "
                "MinusPod is still processing, keeping poll loop alive."
            )
            return {"already_processing": True}
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
        max_wallclock_seconds: int | None = None,
        stall_threshold_seconds: int | None = None,
        pause_event=None,
    ) -> Path:
        """Download processed audio, retrying on 503 (queue busy/processing).

        The JIT endpoint returns 503 when the episode is being processed or
        the queue is busy. We retry with backoff until the audio is ready.
        If skip_event is set during a retry wait, raises _SkippedError.

        Bounded by two safety nets so a wedged backend can't make a single
        episode hold the whole queue hostage:
        - `max_wallclock_seconds` (env: `EPISODE_MAX_WALLCLOCK_SECONDS`, default
          5400 = 90 min): hard cap on total time spent waiting. The previous
          implementation polled for `max_retries × retry_after` ≈ 8 hours,
          which is what made "second episode never uploaded" possible.
        - `stall_threshold_seconds` (env: `EPISODE_STALL_THRESHOLD_SECONDS`,
          default 900 = 15 min): if MinusPod's reported `stage` doesn't change
          for this long we treat the backend as wedged and either bounce
          whisper-server or abort. This is what catches the silent
          "transcription happening but nothing visible" hang.
        """
        if not slug or slug == '_files':
            raise ValueError(
                "Re-processing custom uploaded files is not supported: "
                "MinusPod processes episodes by feed + episode ID, and custom "
                "files live only in Pocket Casts, not in any RSS feed. "
                "Re-process the original RSS feed episode instead."
            )

        # Resolve safety caps from kwargs → env → defaults.
        if max_wallclock_seconds is None:
            try:
                max_wallclock_seconds = int(os.environ.get(
                    "EPISODE_MAX_WALLCLOCK_SECONDS", "5400"
                ))
            except ValueError:
                max_wallclock_seconds = 5400
        if stall_threshold_seconds is None:
            try:
                stall_threshold_seconds = int(os.environ.get(
                    "EPISODE_STALL_THRESHOLD_SECONDS", "900"
                ))
            except ValueError:
                stall_threshold_seconds = 900

        url = f"{self.base_url}/episodes/{slug}/{episode_id}.mp3"
        safe_id = re.sub(r'[^\w-]', '_', episode_id)[:80]
        output_path = output_dir / f"{safe_id}.mp3"
        last_stage = ""
        last_progress_at = time.monotonic()
        wallclock_start = time.monotonic()
        service_bounced_for_stall = False
        bounced_service_name = ""
        # Cap how many times we'll ask MinusPod to re-attempt a "GONE" episode.
        # MinusPod itself caps internal retries at 3; if it keeps coming back
        # GONE, the underlying problem (e.g. Whisper backend down) won't fix
        # itself by hammering the reprocess endpoint.
        MAX_REPROCESS_TRIGGERS = 2
        reprocess_count = 0
        MAX_ORPHAN_RECOVERY = 3
        orphan_recovery_count = 0
        # Sample episode-status every N retry iterations to detect a
        # "permanently_failed" verdict without spamming MinusPod.
        STATUS_CHECK_EVERY = 3

        for attempt in range(max_retries):
            if pause_event and pause_event.is_set():
                log.info("  Pause requested mid-episode. Pausing poll loop (services kept running)...")
                if progress_callback:
                    progress_callback("Pausing active processing and freeing resources...")
                # Do NOT stop Whisper or Ollama here.
                # MinusPod is a separate process that continues running and may have
                # transcription or LLM inference in-flight. Stopping either service
                # mid-chunk causes MinusPod to mark the episode 'failed', which burns
                # the reprocess budget. We simply pause our polling loop and let
                # MinusPod finish whatever chunk it's working on.
                # Resources (Ollama KEEP_ALIVE, Whisper) are freed between episodes
                # by the caller in ui_server.py, safely after the episode completes.
                while pause_event.is_set():
                    if skip_event and skip_event.is_set():
                        raise _SkippedError("Skipped by user")
                    time.sleep(1)
                log.info("  Resumed (services were kept running, no restart needed).")
                if progress_callback:
                    progress_callback("Resuming services...")
                # Reset reprocess budget — any 'failed' MinusPod state that appeared
                # while paused was caused by transient issues, not a genuine error.
                reprocess_count = 0
                last_progress_at = time.monotonic()
                service_bounced_for_stall = False
                log.info("  Reprocess budget reset after pause/resume.")

            # Hard wallclock cap — this is the real safety net.
            elapsed = time.monotonic() - wallclock_start
            if elapsed > max_wallclock_seconds:
                raise TimeoutError(
                    f"Gave up waiting for {slug}/{episode_id} after "
                    f"{elapsed/60:.0f} min (cap: {max_wallclock_seconds/60:.0f} min, "
                    f"last stage: {last_stage or 'unknown'}). The episode is "
                    f"either too long for the configured budget, or the "
                    f"MinusPod / Whisper backend is wedged. Bumping the cap: "
                    f"export EPISODE_MAX_WALLCLOCK_SECONDS=10800."
                )
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
                                last_progress_at = time.monotonic()
                            elif attempt == 0:
                                log.info(f"  Episode queued for processing, waiting...")
                                if progress_callback:
                                    progress_callback("Queued for processing...")
                        except Exception:
                            if attempt == 0:
                                log.info(f"  Episode queued for processing, waiting...")

                        # Stall watchdog: if `stage` hasn't advanced for the
                        # configured threshold, bounce the relevant backend
                        # (Ollama for ad detection, whisper for transcription).
                        stall_for = time.monotonic() - last_progress_at
                        stage_stall_cap = _stall_threshold_for_stage(
                            last_stage, stall_threshold_seconds,
                        )
                        if stall_for > stage_stall_cap:
                            if not service_bounced_for_stall:
                                service_bounced_for_stall = True
                                bounced_ok, bounced_service_name = (
                                    _bounce_service_for_stall(last_stage)
                                )
                                log.warning(
                                    f"  No MinusPod stage change for "
                                    f"{stall_for/60:.0f} min (stuck on "
                                    f"'{last_stage or 'unknown'}', cap "
                                    f"{stage_stall_cap/60:.0f} min). Bouncing "
                                    f"{bounced_service_name} in case it's wedged."
                                )
                                if progress_callback:
                                    progress_callback(
                                        f"Stalled on '{last_stage or 'unknown'}' "
                                        f"for {stall_for/60:.0f}m — restarting "
                                        f"{bounced_service_name}..."
                                    )
                                if not bounced_ok:
                                    log.warning(
                                        f"  {bounced_service_name} restart "
                                        f"failed or skipped."
                                    )
                                last_progress_at = time.monotonic()
                            else:
                                if bounced_service_name == "Ollama":
                                    log_hint = "~/Library/Logs/Homebrew/ollama/ollama.log"
                                elif bounced_service_name == "MinusPod":
                                    log_hint = "/tmp/minuspod.log"
                                else:
                                    log_hint = "/tmp/whisper-server.log"
                                llm_hint = ""
                                if _is_llm_stage(last_stage) or not last_stage:
                                    if _llm_provider_uses_ollama():
                                        llm_hint = (
                                            " Ad detection uses Ollama — if windows "
                                            "keep timing out, set OPENAI_MODEL=qwen3:14b "
                                            "in .env (faster on ≤36GB Macs) or raise "
                                            "LLM_TIMEOUT_LOCAL in MinusPod's environment."
                                        )
                                    else:
                                        provider = os.environ.get(
                                            "LLM_PROVIDER", "openrouter",
                                        )
                                        llm_hint = (
                                            f" Ad detection uses {provider} "
                                            f"(LLM_PROVIDER={provider}) — verify "
                                            "OPENAI_MODEL is valid for that API and "
                                            "check /tmp/minuspod.log for LLM errors."
                                        )
                                raise TimeoutError(
                                    f"MinusPod stuck on stage "
                                    f"'{last_stage or 'unknown'}' for "
                                    f"{stall_for/60:.0f} min even after "
                                    f"restarting {bounced_service_name or 'backend'}. "
                                    f"Aborting so other queued episodes can run. "
                                    f"Inspect /tmp/minuspod.log and {log_hint}."
                                    f"{llm_hint}"
                                )

                        # Periodically check the episode-detail endpoint —
                        # 1. `currentJob == null` + `status == failed` means
                        #    MinusPod has given up and 503 will never become 200.
                        # 2. `currentJob == null` + `status == processing` means
                        #    the episode is orphaned (stuck from a previous pause/crash).
                        if attempt > 0 and attempt % STATUS_CHECK_EVERY == 0:
                            ep_detail = self.get_episode(slug, episode_id)
                            if ep_detail:
                                status = ep_detail.get("status")
                                current_job = st.get("currentJob")
                                
                                # Auto-recovery for orphaned 'processing' state
                                if status == "processing" and not current_job:
                                    orphan_recovery_count += 1
                                    if orphan_recovery_count > MAX_ORPHAN_RECOVERY:
                                        raise RuntimeError(
                                            f"Episode {slug}/{episode_id} stuck in "
                                            "orphaned 'processing' state after "
                                            f"{MAX_ORPHAN_RECOVERY} recovery attempts. "
                                            "MinusPod has no active worker for this "
                                            "episode — check /tmp/minuspod.log."
                                        )
                                    log.warning(
                                        f"  Detected orphaned processing status for "
                                        f"{slug}/{episode_id} with no active job "
                                        f"({orphan_recovery_count}/{MAX_ORPHAN_RECOVERY}). "
                                        "Forcing database reset..."
                                    )
                                    if progress_callback:
                                        progress_callback(
                                            "Orphaned job detected; resetting status "
                                            "to discovered..."
                                        )

                                    if _reset_orphaned_episode_in_db(slug, episode_id):
                                        log.info(
                                            "  Reset orphaned episode status to "
                                            "discovered in DB."
                                        )
                                    else:
                                        log.warning(
                                            "  Orphaned episode DB reset matched no "
                                            "rows (already cleared or wrong id?)."
                                        )

                                    try:
                                        self.reprocess_episode(
                                            slug, episode_id, mode="full",
                                        )
                                    except Exception as e:
                                        log.error(
                                            f"  Failed to trigger reprocess for "
                                            f"orphaned episode: {e}"
                                        )
                                    last_progress_at = time.monotonic()
                                    service_bounced_for_stall = False
                                    time.sleep(10)
                                    continue

                                err_text = ""
                                if status in ("failed", "permanently_failed"):
                                    err_text = (ep_detail.get("error") or "").strip()
                                    # Only reprocess for genuine failures, not transient
                                    # states (processing, discovering, done, etc.).
                                    # MinusPod sometimes marks an episode 'failed' while
                                    # still internally retrying a window; calling reprocess
                                    # then gets a 409 (still processing) which should not
                                    # burn a retry slot.
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
                                        if _is_transcription_failure(err_text):
                                            if progress_callback:
                                                progress_callback(
                                                    "Whisper appears wedged; restarting it before retry..."
                                                )
                                            if _restart_whisper_if_wedged():
                                                log.info("  Whisper restarted; retrying transcription.")
                                            else:
                                                log.warning(
                                                    "  Whisper restart attempt failed or skipped; "
                                                    "reprocess may still loop."
                                                )
                                        try:
                                            result = self.reprocess_episode(slug, episode_id)
                                            if result.get("already_processing"):
                                                # MinusPod is still working — don't count this
                                                # as a retry; just keep polling.
                                                reprocess_count -= 1
                                                log.info(
                                                    "  MinusPod still processing (409); "
                                                    "reprocess count not incremented."
                                                )
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

    def sync_model_from_env(self):
        """Apply OPENAI_MODEL from the environment to MinusPod's settings DB.

        Required when LLM_PROVIDER is not ollama — MinusPod otherwise keeps a
        stale Ollama model name (e.g. qwen3:14b) that cloud APIs reject.
        """
        provider = os.environ.get("LLM_PROVIDER", "ollama")
        if provider == "ollama":
            return
        model = os.environ.get("OPENAI_MODEL", "").strip()
        if not model:
            log.warning("LLM_PROVIDER=%s but OPENAI_MODEL is not set", provider)
            return
        try:
            resp = self.client.get(f"{self.base_url}/api/v1/settings", timeout=5)
            if resp.status_code == 200:
                cm = resp.json().get("claudeModel")
                current = cm.get("value") if isinstance(cm, dict) else cm
                if current == model:
                    return
            self.client.put(
                f"{self.base_url}/api/v1/settings/ad-detection",
                json={
                    "claudeModel": model,
                    "verificationModel": model,
                    "chaptersModel": model,
                },
                timeout=10,
            )
            log.info(f"Synced MinusPod ad-detection model from .env: {model}")
        except Exception as e:
            log.warning(f"Could not sync MinusPod model from .env: {e}")

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

        cues = _parse_vtt_cues(vtt_text)
        if not cues:
            return False

        transcript_text = _vtt_cues_to_minuspod_text(cues)
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
            log.info(f"  Pre-populated transcript ({len(cues)} segments)")
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


ARTWORK_CACHE_FILE = Path(__file__).parent / "podcast_artwork_cache.json"


def _load_artwork_cache() -> dict:
    try:
        if ARTWORK_CACHE_FILE.exists():
            return json.loads(ARTWORK_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_artwork_cache(cache: dict) -> None:
    try:
        ARTWORK_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.debug(f"Could not write artwork cache: {e}")


def get_podcast_artwork_url(podcast_uuid: str, podcast_title: str) -> str:
    """Return a high-res artwork URL for a podcast, or "" if unknown.

    Source priority:
      1. Local cache (podcast_artwork_cache.json, keyed by uuid).
      2. iTunes Search API — `artworkUrl600` is a reliable public CDN URL
         that's already used by the official iOS/Android/web clients.
         We do an exact title match (falling back to the first result)
         to avoid grabbing the wrong podcast's art.
      3. Empty string — the UI falls back to an initial-letter placeholder.

    Result is cached permanently: iTunes artwork URLs are stable, and a
    bad match won't change unless the user unsubscribes and resubscribes.
    """
    if not podcast_title:
        return ""
    cache = _load_artwork_cache()
    if podcast_uuid in cache:
        return cache[podcast_uuid]
    try:
        resp = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": podcast_title, "media": "podcast", "limit": 3},
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            title_lower = podcast_title.lower().strip()
            url = ""
            for r in results:
                if r.get("trackName", "").lower().strip() == title_lower:
                    url = r.get("artworkUrl600", "")
                    if url:
                        break
            if not url and results:
                url = results[0].get("artworkUrl600", "")
            cache[podcast_uuid] = url
            _save_artwork_cache(cache)
            return url
    except Exception as e:
        log.debug(f"Artwork lookup failed for {podcast_title}: {e}")
    # Negative-cache so we don't hammer iTunes for unmatchable titles.
    cache[podcast_uuid] = ""
    _save_artwork_cache(cache)
    return ""


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _ip_is_non_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_is_non_public(ip.ipv4_mapped)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    return any(ip in net for net in _PRIVATE_NETWORKS)


def _assert_public_url(url: str) -> None:
    """Raise ValueError if url targets a private or link-local address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked non-http(s) URL: {url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"Blocked URL with no host: {url}")
    try:
        ip = ipaddress.ip_address(host)
        if _ip_is_non_public(ip):
            raise ValueError(f"Blocked private/internal URL: {url}")
        return
    except ValueError as exc:
        if str(exc).startswith("Blocked"):
            raise
    try:
        for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(info[4][0])
            if _ip_is_non_public(ip):
                raise ValueError(f"Blocked private/internal URL: {url}")
    except ValueError:
        raise
    except OSError as exc:
        log.warning("DNS resolution failed for %s: %s; allowing URL", host, exc)


def _httpx_request_public(
    method: str,
    url: str,
    max_redirects: int = 10,
    **kwargs,
) -> tuple[httpx.Response, str]:
    """HTTP request with redirect-aware private-IP blocking.

    Returns (response, final_url) after following redirects safely.
    """
    current = url
    timeout = kwargs.pop("timeout", 15)
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            _assert_public_url(current)
            resp = client.request(method, current, **kwargs)
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("Location")
                if not location:
                    return resp, current
                current = urljoin(current, location)
                continue
            return resp, current
    raise ValueError(f"Too many redirects for URL: {url}")


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
                _, final = _httpx_request_public("HEAD", raw_url, timeout=10)
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
        resp, _ = _httpx_request_public(
            "GET",
            f"https://podcast-api.pocketcasts.com/podcast/full/{podcast_uuid}",
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("podcast", {}).get("url")
    except Exception:
        pass
    return None


# --- Pocket Casts transcript reuse (gated injection) -------------------------

_VTT_INLINE_TIME_RE = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")
_VTT_VOICE_TAG_RE = re.compile(r"</?v[^>]*>", re.I)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def pc_transcript_reuse_enabled() -> bool:
    """True when the PC transcript fetch/verify/inject path should run."""
    return _env_bool("PC_TRANSCRIPT_REUSE", True) and not _env_bool(
        "DISABLE_TRANSCRIPT_SYNC", False
    )


def pc_transcript_min_coverage() -> float:
    return _env_float("PC_TRANSCRIPT_MIN_COVERAGE", 0.97)


def pc_transcript_max_duration_delta() -> float:
    return _env_float("PC_TRANSCRIPT_MAX_DURATION_DELTA", 10.0)


def pc_transcript_effective_max_duration_delta(audio_duration: float) -> float:
    """Max |audio_duration - vtt_duration| allowed for a given episode length.

    Uses the greater of the configured floor (``PC_TRANSCRIPT_MAX_DURATION_DELTA``)
    and the gap implied by ``PC_TRANSCRIPT_MIN_COVERAGE``. A flat 10s cap
    contradicts a 97% coverage floor on long episodes (3% of 2h ≈ 216s).
    """
    floor = pc_transcript_max_duration_delta()
    if audio_duration <= 0:
        return floor
    proportional = audio_duration * (1.0 - pc_transcript_min_coverage())
    return max(floor, proportional)


def pc_transcript_coverage_metrics(
    vtt_duration: float, audio_duration: float
) -> dict:
    """Coverage / duration metrics for a PC VTT vs audio length."""
    coverage = (vtt_duration / audio_duration) if audio_duration > 0 else 0.0
    duration_delta = abs(audio_duration - vtt_duration) if audio_duration > 0 else 0.0
    max_delta = pc_transcript_effective_max_duration_delta(audio_duration)
    return {
        "coverage": coverage,
        "duration_delta": duration_delta,
        "max_duration_delta": max_delta,
        "coverage_pass": coverage >= pc_transcript_min_coverage(),
        "duration_pass": duration_delta <= max_delta,
    }


def estimate_transcript_drift(drift_probes: list[dict]) -> float:
    """Median timestamp offset from multi-point Whisper-vs-PC alignment.

    Filters out low-confidence matches that indicate a false alignment.
    """
    usable = [
        p
        for p in drift_probes
        if p.get("similarity", 0) >= 0.4 and abs(p.get("offset", 0)) < 600
    ]
    if not usable:
        usable = [p for p in drift_probes if p.get("similarity", 0) >= 0.25]
    if not usable:
        return 0.0
    offsets = sorted(float(p["offset"]) for p in usable)
    return offsets[len(offsets) // 2]


def compare_ad_markers_in_transcripts(
    whisper_segments: list[dict],
    pc_cues: list[dict],
    ad_markers: list[dict],
    *,
    drift: float = 0.0,
    min_similarity: float = 0.4,
) -> list[dict]:
    """Compare ad-marker windows between Whisper and PC transcript text."""
    results = []
    for ad in ad_markers:
        start = float(ad.get("start", 0))
        end = float(ad.get("end", start))
        w_text = _normalize_for_align(
            _text_for_time_range(whisper_segments, start, end)
        )
        pc_start = max(0.0, start + drift)
        pc_end = max(pc_start, end + drift)
        p_text = _normalize_for_align(
            _text_for_time_range(pc_cues, pc_start, pc_end)
        )
        similarity = (
            difflib.SequenceMatcher(None, w_text, p_text).ratio()
            if w_text and p_text
            else 0.0
        )
        results.append(
            {
                "start": start,
                "end": end,
                "drift_applied": round(drift, 2),
                "whisper_chars": len(w_text),
                "pc_chars": len(p_text),
                "similarity": round(similarity, 3),
                "ad_present_in_pc": similarity >= min_similarity if w_text else None,
            }
        )
    return results


def simulate_pc_transcript_gate_from_segments(
    whisper_segments: list[dict],
    pc_cues: list[dict],
    audio_duration: float,
) -> dict:
    """Offline gate simulation using Whisper DB segments instead of live probes.

    Mirrors ``_verify_pc_transcript`` probe logic for validation harnesses.
    """
    cov = pc_transcript_coverage_metrics(
        pc_cues[-1]["end"] if pc_cues else 0.0, audio_duration
    )
    result = {
        **cov,
        "probes": [],
        "max_offset": 0.0,
        "min_similarity": 1.0,
        "gate_pass_simulated": False,
        "gate_failure_reason": "",
    }
    if not pc_cues:
        result["gate_failure_reason"] = "no cues"
        return result
    if not cov["coverage_pass"]:
        result["gate_failure_reason"] = "coverage"
        return result
    if not cov["duration_pass"]:
        result["gate_failure_reason"] = "duration delta"
        return result

    min_sim = pc_transcript_min_similarity()
    max_offset = pc_transcript_max_offset()
    probe_times = _probe_times_for_duration(
        audio_duration, pc_transcript_probe_count()
    )
    probes: list[dict] = []

    for probe_time in probe_times:
        sample = _text_for_time_range(whisper_segments, probe_time, probe_time + 20.0)
        if not sample.strip():
            result["gate_failure_reason"] = f"empty probe at {probe_time:.0f}s"
            return result
        ratio, matched_time = _align_sample(sample, pc_cues, probe_time)
        offset = matched_time - probe_time
        probes.append(
            {
                "time": probe_time,
                "similarity": round(ratio, 3),
                "offset": round(offset, 2),
                "matched_time": round(matched_time, 2),
            }
        )

    result["probes"] = probes
    passed, reason, stats = _judge_pc_transcript_probes(probes)
    result.update(stats)
    if passed:
        result["gate_pass_simulated"] = True
    else:
        result["gate_failure_reason"] = reason
    return result


def pc_transcript_probe_count() -> int:
    return _env_int("PC_TRANSCRIPT_PROBES", 5)


def pc_transcript_min_similarity() -> float:
    return _env_float("PC_TRANSCRIPT_MIN_SIMILARITY", 0.55)


def pc_transcript_max_offset() -> float:
    return _env_float("PC_TRANSCRIPT_MAX_OFFSET", 3.0)


def pc_transcript_probe_warmup_seconds() -> float:
    """Skip the first N seconds when placing the opening probe.

    Cold opens (theme music, sparse speech) routinely misalign across ASR
    engines even when the rest of the episode is in sync.
    """
    return _env_float("PC_TRANSCRIPT_PROBE_WARMUP_SECONDS", 90.0)


def pc_transcript_probe_max_failures() -> int:
    """How many probe checks may fail before rejecting the transcript."""
    return _env_int("PC_TRANSCRIPT_PROBE_MAX_FAILURES", 1)


def pc_transcript_allow_rss() -> bool:
    return _env_bool("PC_TRANSCRIPT_ALLOW_RSS", False)


def _parse_vtt_ts(ts_str: str) -> float:
    """Parse VTT timestamp (MM:SS.mmm or HH:MM:SS.mmm) to seconds."""
    parts = ts_str.replace(",", ".").strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _fmt_vtt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _clean_vtt_cue_text(text: str) -> str:
    text = _VTT_VOICE_TAG_RE.sub("", text)
    text = _VTT_INLINE_TIME_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_vtt_cues(vtt_text: str) -> list[dict]:
    """Parse WEBVTT into cue dicts with start, end, text (seconds)."""
    cues: list[dict] = []
    current_start = None
    current_end = None
    current_text: list[str] = []

    for raw_line in vtt_text.strip().splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE") or line.isdigit():
            continue
        ts_match = re.match(r"([\d:.]+)\s*-->\s*([\d:.]+)", line)
        if ts_match:
            if current_start is not None and current_text:
                text = _clean_vtt_cue_text(" ".join(current_text))
                if text:
                    cues.append(
                        {"start": current_start, "end": current_end, "text": text}
                    )
            current_start = _parse_vtt_ts(ts_match.group(1))
            end_token = ts_match.group(2).split()[0]
            current_end = _parse_vtt_ts(end_token)
            current_text = []
        elif current_start is not None:
            if line.lower().startswith(("align:", "position:", "size:", "line:")):
                continue
            current_text.append(line)

    if current_start is not None and current_text:
        text = _clean_vtt_cue_text(" ".join(current_text))
        if text:
            cues.append({"start": current_start, "end": current_end, "text": text})
    return cues


def _vtt_cues_to_minuspod_text(cues: list[dict]) -> str:
    lines = [
        f"[{_fmt_vtt_ts(c['start'])} --> {_fmt_vtt_ts(c['end'])}] {c['text']}"
        for c in cues
    ]
    return "\n".join(lines)


def _parse_minuspod_transcript(transcript_text: str) -> list[dict]:
    """Parse MinusPod timestamped transcript lines into segment dicts."""
    segments: list[dict] = []
    for line in transcript_text.split("\n"):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            time_part, text_part = line.split("] ", 1)
            time_range = time_part.strip("[")
            start_str, end_str = time_range.split(" --> ")
            segments.append(
                {
                    "start": _parse_vtt_ts(start_str),
                    "end": _parse_vtt_ts(end_str),
                    "text": text_part,
                }
            )
        except (ValueError, TypeError):
            continue
    return segments


def _normalize_for_align(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _cues_to_word_timeline(cues: list[dict]) -> list[dict]:
    timeline: list[dict] = []
    for cue in cues:
        words = cue["text"].split()
        if not words:
            continue
        start = cue["start"]
        end = cue["end"]
        duration = max(end - start, 0.001)
        if len(words) == 1:
            timeline.append({"word": words[0], "time": start})
            continue
        step = duration / len(words)
        for i, word in enumerate(words):
            timeline.append({"word": word, "time": start + i * step})
    return timeline


def _align_sample(
    sample_text: str,
    cues: list[dict],
    expected_time: float,
    band: float = 180.0,
) -> tuple[float, float]:
    """Fuzzy-align sample text against VTT cues near expected_time.

    Returns (similarity_ratio, matched_timestamp).
    """
    sample_norm = _normalize_for_align(sample_text)
    sample_words = sample_norm.split()
    if not sample_words:
        return 0.0, expected_time

    timeline = _cues_to_word_timeline(cues)
    if not timeline:
        return 0.0, expected_time

    in_band = [
        w for w in timeline
        if expected_time - band <= w["time"] <= expected_time + band
    ]
    if len(in_band) < len(sample_words):
        in_band = timeline

    words = [w["word"] for w in in_band]
    times = [w["time"] for w in in_band]
    n = len(sample_words)
    best_ratio = 0.0
    best_time = expected_time

    if len(words) < n:
        window_norm = _normalize_for_align(" ".join(words))
        best_ratio = difflib.SequenceMatcher(None, sample_norm, window_norm).ratio()
        if words:
            best_time = times[0]
        return best_ratio, best_time

    for start in range(len(words) - n + 1):
        window_norm = _normalize_for_align(" ".join(words[start : start + n]))
        ratio = difflib.SequenceMatcher(None, sample_norm, window_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_time = times[start]
    return best_ratio, best_time


def _probe_times_for_duration(duration: float, n_probes: int) -> list[float]:
    if duration <= 0 or n_probes <= 0:
        return []
    warmup = pc_transcript_probe_warmup_seconds()
    first_probe = min(warmup, max(0.0, duration - 20.0))
    candidates = [
        first_probe,
        duration * 0.25,
        duration * 0.5,
        duration * 0.75,
        max(0.0, duration - 90.0),
    ]
    times: list[float] = []
    for t in candidates:
        t = max(0.0, min(t, max(0.0, duration - 20.0)))
        if t not in times:
            times.append(t)
        if len(times) >= n_probes:
            break
    return times[:n_probes]


def _judge_pc_transcript_probes(probes: list[dict]) -> tuple[bool, str, dict]:
    """Evaluate collected probe results. Returns (passed, reason, stats)."""
    stats: dict = {
        "probes_passed": 0,
        "probes_failed": 0,
        "max_offset": 0.0,
        "min_similarity": 1.0,
        "median_abs_offset": 0.0,
    }
    if not probes:
        return False, "no probes", stats

    min_sim = pc_transcript_min_similarity()
    max_offset = pc_transcript_max_offset()
    max_failures = pc_transcript_probe_max_failures()

    passed_probes: list[dict] = []
    failures: list[str] = []
    for probe in probes:
        ratio = float(probe.get("similarity", 0))
        offset = float(probe.get("offset", 0))
        stats["max_offset"] = max(stats["max_offset"], abs(offset))
        stats["min_similarity"] = min(stats["min_similarity"], ratio)
        probe_ok = ratio >= min_sim and abs(offset) <= max_offset
        if probe_ok:
            passed_probes.append(probe)
            stats["probes_passed"] += 1
        else:
            stats["probes_failed"] += 1
            reason_parts = []
            if ratio < min_sim:
                reason_parts.append(f"similarity {ratio:.2f} < {min_sim}")
            if abs(offset) > max_offset:
                reason_parts.append(f"offset {offset:+.1f}s > {max_offset}s")
            failures.append(
                f"{probe.get('time', '?')}s ({', '.join(reason_parts)})"
            )

    if passed_probes:
        abs_offsets = sorted(abs(float(p["offset"])) for p in passed_probes)
        stats["median_abs_offset"] = abs_offsets[len(abs_offsets) // 2]

    if stats["probes_failed"] > max_failures:
        return (
            False,
            f"{stats['probes_failed']}/{len(probes)} probes failed "
            f"(max {max_failures}): {failures[0]}",
            stats,
        )

    if not passed_probes:
        return False, failures[0] if failures else "no probes passed", stats

    return True, "", stats


def _whisper_sample_available() -> bool:
    root = Path(__file__).parent
    whisper_bin = root / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    model_path = root / "whisper.cpp" / "models" / "ggml-large-v3-turbo.bin"
    return whisper_bin.exists() and model_path.exists()


def _text_for_time_range(segments: list[dict], start: float, end: float) -> str:
    parts = []
    for seg in segments:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        parts.append(seg["text"])
    return " ".join(parts)


def _verify_pc_transcript(
    vtt: str,
    audio_url: str,
    audio_duration: float,
    progress_callback=None,
) -> tuple[bool, dict]:
    """Gate PC transcript before injection. Returns (passed, metrics)."""
    metrics: dict = {
        "cue_count": 0,
        "vtt_duration": 0.0,
        "audio_duration": audio_duration,
        "coverage": 0.0,
        "duration_delta": 0.0,
        "max_duration_delta": 0.0,
        "probes": [],
        "max_offset": 0.0,
        "min_similarity": 1.0,
        "failure_reason": "",
    }

    cues = _parse_vtt_cues(vtt)
    metrics["cue_count"] = len(cues)
    if not cues:
        metrics["failure_reason"] = "no cues parsed"
        return False, metrics

    vtt_duration = cues[-1]["end"]
    metrics["vtt_duration"] = vtt_duration
    if audio_duration <= 0:
        metrics["failure_reason"] = "unknown audio duration"
        return False, metrics

    cov = pc_transcript_coverage_metrics(vtt_duration, audio_duration)
    metrics.update(cov)
    if not cov["coverage_pass"]:
        metrics["failure_reason"] = (
            f"coverage {cov['coverage']:.1%} < {pc_transcript_min_coverage():.0%}"
        )
        return False, metrics

    if not cov["duration_pass"]:
        metrics["failure_reason"] = (
            f"duration delta {cov['duration_delta']:.1f}s > "
            f"{cov['max_duration_delta']:.1f}s"
        )
        return False, metrics

    if not _whisper_sample_available():
        metrics["failure_reason"] = "whisper-cli unavailable for probe verification"
        return False, metrics

    if not audio_url:
        metrics["failure_reason"] = "no audio URL for probe verification"
        return False, metrics

    probe_times = _probe_times_for_duration(
        audio_duration, pc_transcript_probe_count()
    )
    probes: list[dict] = []

    for probe_time in probe_times:
        if progress_callback:
            progress_callback(f"Sync probe at {probe_time:.0f}s...")
        sample = _transcribe_sample(audio_url, start=probe_time, duration=20.0)
        if not sample:
            metrics["failure_reason"] = f"empty probe at {probe_time:.0f}s"
            return False, metrics
        ratio, matched_time = _align_sample(sample, cues, probe_time)
        offset = matched_time - probe_time
        probes.append(
            {
                "time": probe_time,
                "similarity": round(ratio, 3),
                "offset": round(offset, 2),
                "matched_time": round(matched_time, 2),
            }
        )

    metrics["probes"] = probes
    passed, reason, stats = _judge_pc_transcript_probes(probes)
    metrics.update(stats)
    if not passed:
        metrics["failure_reason"] = reason
        return False, metrics
    return True, metrics


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
        _, resolved_url = _httpx_request_public("HEAD", url, timeout=15)
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
    cues = _parse_vtt_cues(vtt_text)
    return cues[-1]["end"] if cues else 0.0


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
    pause_event=None,
    rss_url: str = None,
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
        log.warning(
            f"  Episode '{ep_title}' has status '{ep_status}' in MinusPod. "
            "Triggering reprocess..."
        )
        # `full` clears cached transcript/ads and re-runs AI from scratch —
        # needed when MinusPod has given up (`permanently_failed`).
        reprocess_mode = "full" if ep_status == "permanently_failed" else "reprocess"
        if progress_callback:
            progress_callback(
                f"Episode failed in MinusPod ({ep_status}), "
                f"requesting reprocess (mode={reprocess_mode})..."
            )
        try:
            result = mp.reprocess_episode(feed_slug, ep_id, mode=reprocess_mode)
            if result.get("already_processing"):
                log.info("  MinusPod already reprocessing this episode.")
                if progress_callback:
                    progress_callback("MinusPod is already reprocessing this episode...")
            else:
                log.info(f"  Reprocess queued (mode={reprocess_mode}).")
                if progress_callback:
                    progress_callback(f"Reprocess queued (mode={reprocess_mode})")
            time.sleep(3)
            refreshed = mp.get_episode(feed_slug, ep_id)
            if refreshed:
                episode = refreshed
                ep_id = refreshed["id"]
                ep_status = refreshed.get("status", ep_status)
                state_key = f"{feed_slug}:{ep_id}"
                log.info(f"  Episode status after reprocess request: {ep_status}")
        except Exception as e:
            # Don't abort — download_processed_audio has its own reprocess
            # retry budget when it sees failed / permanently_failed states.
            log.warning(f"  Reprocess request failed: {e} — download loop will retry")
            if progress_callback:
                progress_callback(
                    f"Reprocess request failed ({e}); will retry during download..."
                )

    log.info(f"  Downloading ad-free audio ({ep_status}): {ep_title}")
    if progress_callback:
        progress_callback(f"Downloading/processing: {ep_title}")

    # Pocket Casts generated transcripts can replace Whisper when verified.
    # PC runs ASR over the CDN audio (ads included). RSS publisher transcripts
    # are injectable only when PC_TRANSCRIPT_ALLOW_RSS=true.
    if pc_transcript_reuse_enabled() and podcast_uuid and ep_status != "completed":
        if not original_episode_uuid:
            log.info("  Could not match episode to Pocket Casts UUID (title matching failed)")
            if progress_callback:
                progress_callback("Could not match to PC episode, will use Whisper for transcript")
        else:
            try:
                vtt = None
                vtt_source = None  # 'pc' or 'rss'

                def _resolve_source_audio_url() -> str:
                    source = episode.get("url", "")
                    if source:
                        return source
                    if podcast_uuid in ("_files", USER_PODCAST_UUID):
                        return ""
                    try:
                        resolved_rss = rss_url or find_rss_url_for_podcast(
                            podcast_uuid, pc=pc
                        )
                        if not resolved_rss:
                            return ""
                        resp = httpx.get(resolved_rss, timeout=15)
                        root = ET.fromstring(resp.text)
                        ep_norm = _normalize_title(ep_title)
                        for item in root.findall(".//item"):
                            item_title = item.find("title")
                            if item_title is None:
                                continue
                            if _normalize_title(item_title.text or "") != ep_norm:
                                continue
                            enclosure = item.find("enclosure")
                            if enclosure is not None:
                                log.info("  Resolved source audio URL from RSS")
                                return enclosure.get("url", "")
                    except Exception as e:
                        log.debug(f"  Could not resolve audio URL from RSS: {e}")
                    return ""

                # 1. Pocket Casts generated transcript
                vtt = pc.get_transcript_vtt(podcast_uuid, original_episode_uuid)
                if vtt:
                    vtt_source = "pc"

                # 2. RSS transcript fallback (verify-only unless allow_rss)
                if not vtt and podcast_uuid != "_files":
                    resolved_rss = rss_url or find_rss_url_for_podcast(
                        podcast_uuid, pc=pc
                    )
                    if resolved_rss:
                        vtt = pc.get_transcript_vtt_from_rss(resolved_rss, ep_title)
                        if vtt:
                            vtt_source = "rss"

                source_audio_url = _resolve_source_audio_url()
                audio_info = _get_audio_summary(source_audio_url)
                actual_dur = audio_info["duration"]

                def _coverage_for_vtt(vtt_text: str) -> float:
                    if actual_dur <= 0:
                        return 0.0
                    return _get_vtt_duration(vtt_text) / actual_dur

                if not vtt:
                    log.info(
                        f"  Transcript not found for {original_episode_uuid[:12]}, "
                        "requesting generation..."
                    )
                    if progress_callback:
                        progress_callback("Transcript not found, requesting generation...")
                    for attempt in range(5):
                        pc.request_transcript_generation(original_episode_uuid)
                        time.sleep(10)
                        vtt = pc.get_transcript_vtt(
                            podcast_uuid, original_episode_uuid
                        )
                        if vtt:
                            vtt_source = "pc"
                            break
                        log.info(f"  Retry {attempt+1}/5 for transcript...")

                # Re-request when PC returned a partial transcript
                if vtt and vtt_source == "pc" and actual_dur > 0:
                    coverage = _coverage_for_vtt(vtt)
                    if coverage < pc_transcript_min_coverage():
                        log.info(
                            f"  PC transcript coverage {coverage:.1%} — "
                            "requesting regeneration..."
                        )
                        for attempt in range(3):
                            pc.request_transcript_generation(original_episode_uuid)
                            time.sleep(10)
                            refreshed = pc.get_transcript_vtt(
                                podcast_uuid, original_episode_uuid
                            )
                            if refreshed and _coverage_for_vtt(refreshed) > coverage:
                                vtt = refreshed
                                coverage = _coverage_for_vtt(vtt)
                            if coverage >= pc_transcript_min_coverage():
                                break
                            log.info(
                                f"  Regeneration retry {attempt+1}/3 "
                                f"(coverage {coverage:.1%})..."
                            )

                injectable = bool(
                    vtt
                    and (vtt_source == "pc" or pc_transcript_allow_rss())
                )

                if vtt and not injectable:
                    log.info(
                        "  RSS transcript found but injection disabled "
                        "(PC_TRANSCRIPT_ALLOW_RSS=false) — Whisper will transcribe"
                    )

                if vtt and injectable:
                    if progress_callback:
                        progress_callback("Verifying PC transcript alignment...")
                    log.info("  Verifying PC transcript alignment with audio...")

                    if actual_dur <= 0:
                        log.warning(
                            "  Could not determine audio duration — "
                            "skipping PC transcript injection"
                        )
                    else:
                        verified, metrics = _verify_pc_transcript(
                            vtt,
                            source_audio_url,
                            actual_dur,
                            progress_callback=progress_callback,
                        )
                        if verified:
                            if mp.pre_populate_transcript(feed_slug, ep_id, vtt):
                                log.info(
                                    "  Injected PC transcript "
                                    f"({metrics['cue_count']} cues, "
                                    f"coverage {metrics['coverage']:.1%}, "
                                    f"max offset {metrics['max_offset']:.1f}s) "
                                    "— skipping Whisper"
                                )
                                if progress_callback:
                                    progress_callback(
                                        "PC transcript verified — skipping Whisper"
                                    )
                            else:
                                log.warning(
                                    "  pre_populate_transcript failed — "
                                    "falling back to Whisper"
                                )
                        else:
                            reason = metrics.get("failure_reason", "unknown")
                            log.info(
                                f"  PC transcript verification failed ({reason}) "
                                "— falling back to Whisper"
                            )
                            if progress_callback:
                                progress_callback(
                                    f"PC transcript rejected ({reason}) — Whisper"
                                )
                            if (
                                vtt_source == "pc"
                                and metrics.get("coverage", 0)
                                < pc_transcript_min_coverage()
                            ):
                                pc.request_transcript_generation(
                                    original_episode_uuid
                                )
                elif not vtt:
                    log.info(
                        "  No PC transcript available — Whisper will transcribe"
                    )
                    if progress_callback:
                        progress_callback("Using Whisper for transcription...")
            except Exception as e:
                log.error(f"  Transcript error: {e}")
                raise

    try:
        processed_path = mp.download_processed_audio(
            feed_slug, ep_id, output_dir, skip_event=skip_event,
            progress_callback=progress_callback,
            pause_event=pause_event,
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

    # Mark the original episode as played and replace it in Up Next with
    # the ad-free file in a single atomic sync. Doing this in one POST
    # eliminates the race where two sequential /up_next/sync requests can
    # see inconsistent serverModified timestamps and the remove is lost.
    if podcast_uuid and original_episode_uuid:
        try:
            try:
                pc.mark_episode_played(original_episode_uuid, podcast_uuid)
            except Exception as e:
                log.warning(f"  Could not mark original as played: {e}")
            try:
                _retry_up_next(
                    pc.replace_in_up_next,
                    file_uuid, upload_title, original_episode_uuid,
                    published=ep_published,
                )
                if progress_callback:
                    progress_callback("Removed original episode from Up Next")
            except Exception as e:
                log.warning(f"  Could not remove original from Up Next: {e}")
        except Exception as e:
            log.warning(f"  Up Next replace failed: {e}")
    else:
        # Fallback: title-match sweep. The episode-uuid lookup can miss when
        # MinusPod's RSS title and Pocket Casts' title differ in trivial
        # ways (smart quotes, trailing season tags, etc.) that break exact
        # equality. Re-scan Up Next using the same normalized-title trick
        # the dashboard reconciler uses, but only for the title we just
        # uploaded so we don't sweep unrelated rows. Uses the stronger
        # _normalize_title_strong so '(S2 E5)' matches '(Season 2 Episode 5)',
        # 'Pt. 1' matches 'Part 1', and smart-quote/em-dash differences
        # collapse to the same ASCII form.
        try:
            target_norm = _normalize_title_strong(ep_title)
            for queue_ep in _list_up_next_episodes(pc):
                qu_uuid = queue_ep.get("uuid") or ""
                qu_title = (queue_ep.get("title") or "").strip()
                qu_pod = queue_ep.get("podcast") or ""
                if not qu_uuid or not qu_title:
                    continue
                if qu_pod == USER_PODCAST_UUID:
                    continue  # custom-file row, not the original
                if "(Ad-Free)" in qu_title:
                    continue
                if _normalize_title_strong(qu_title) != target_norm:
                    continue
                try:
                    if qu_pod:
                        try:
                            pc.mark_episode_played(qu_uuid, qu_pod)
                        except Exception as exc:
                            log.warning(f"  Title-match mark played failed for {qu_uuid[:12]}: {exc}")
                    try:
                        _retry_up_next(pc.remove_from_up_next, qu_uuid)
                        if progress_callback:
                            progress_callback("Removed original from Up Next (title match)")
                    except Exception as exc:
                        log.warning(f"  Title-match remove from Up Next failed for {qu_uuid[:12]}: {exc}")
                    break
                except Exception as exc:
                    log.warning(f"  Title-match sweep failed for {qu_uuid[:12]}: {exc}")
        except Exception as exc:
            log.debug(f"  Up Next title-sweep skipped: {exc}")

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
    mp.sync_model_from_env()
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
    file_uuid = process_single_episode(
        pc, mp, feed_slug, target, output_dir, state, rss_url=rss_url,
    )

    if file_uuid:
        log.info(f"\nPIPELINE COMPLETE - uploaded and queued in Up Next")


def run_automation(pc_email, pc_password, rss_urls=None, podcast_filter=None):
    pc = PocketCastsClient(pc_email, pc_password)
    mp = MinusPodClient()
    mp.disable_auto_process()
    mp.sync_model_from_env()
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
            process_single_episode(
                pc, mp, slug, ep, output_dir, state,
                rss_url=feed.get("sourceUrl"),
            )

    log.info("\nAutomation run complete!")


def _load_dotenv_file() -> None:
    """Load ``.env`` into ``os.environ`` when the UI/CLI starts.

    Shell ``source .env`` is still supported; this makes
    ``python3 pocketcasts_adfree.py ui`` work without it.
    """
    try:
        from services_manager import _reload_dotenv_into

        count = _reload_dotenv_into(os.environ)
        if count:
            log.debug("Loaded %d keys from .env", count)
    except Exception as exc:
        log.warning("Could not load .env: %s", exc)


def main():
    _load_dotenv_file()
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
