
"""
Web UI for Pocket Casts Ad-Free Pipeline

Flask app that provides a dashboard for managing podcast subscriptions,
processing episodes, and monitoring the ad removal pipeline.
"""

import json
import logging
import os
import re
import threading
import time
import uuid as uuid_mod
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from pocketcasts_adfree import (
    MinusPodClient,
    PocketCastsClient,
    is_patreon_feed,
    find_rss_url_for_podcast,
    load_state,
    save_state,
    process_single_episode,
    unload_ollama_models,
    _normalize_title,
)
import services_manager

log = logging.getLogger("ui-server")



processing_jobs: dict = {}
job_queue: deque = deque()
queue_lock = threading.Lock()
active_job_id: str | None = None





def create_app(email=None, password=None):
    email = email or os.environ.get("POCKETCASTS_EMAIL")
    password = password or os.environ.get("POCKETCASTS_PASSWORD")

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24).hex()

    pc_client = None
    mp_client = MinusPodClient()
    output_dir = Path(__file__).parent / "processed_audio"
    output_dir.mkdir(exist_ok=True)

    # Disable MinusPod auto-processing at startup to prevent background
    # CPU/GPU usage when the user isn't actively processing episodes.
    try:
        mp_client.disable_auto_process()
        unload_ollama_models()
    except Exception:
        pass

    def get_pc():
        nonlocal pc_client
        if pc_client is None:
            if not email or not password:
                raise RuntimeError("Pocket Casts credentials not set")
            pc_client = PocketCastsClient(email, password)
        return pc_client

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/readme")
    def readme():
        readme_path = Path(__file__).parent / "README.md"
        if not readme_path.exists():
            return ("README.md not found", 404)
        text = readme_path.read_text(encoding="utf-8", errors="replace")
        try:
            import markdown  # type: ignore
            html = markdown.markdown(
                text, extensions=["fenced_code", "tables", "toc"]
            )
        except Exception:
            html = "<pre>" + text.replace("<", "&lt;") + "</pre>"
        return render_template("readme.html", content=html)

    @app.route("/api/status")
    def api_status():
        mp_ok = False
        pc_ok = False
        try:
            mp_client.health()
            mp_ok = True
        except Exception:
            pass
        try:
            get_pc()
            pc_ok = True
        except Exception:
            pass
        return jsonify({"minuspod": mp_ok, "pocketcasts": pc_ok})

    @app.route("/api/subscriptions")
    def api_subscriptions():
        pc = get_pc()
        subs = pc.get_subscriptions()
        state = load_state()
        processed = state.get("processed", {})

        # Auto-reconcile: silently sweep any original (non-Ad-Free) episode
        # whose Ad-Free counterpart already exists as a Pocket Casts custom
        # file. This fixes queues where processing completed but the original
        # never got removed (e.g. stale state or interrupted runs). Safe to
        # run on every request — it's a few small HTTP calls.
        swept_titles: set[str] = set()
        try:
            swept_titles = _reconcile_up_next_originals(pc, processed)
        except Exception as e:  # noqa: BLE001
            log.debug(f"Up Next reconcile failed: {e}")

        # Fetch Up Next queue — return actual episodes, not podcast UUIDs.
        # Fetch AFTER reconcile so swept originals aren't shown as stale.
        #
        # Pocket Casts' /up_next/sync endpoint only returns
        # {title, url, podcast, uuid, published} per episode. We want the
        # same rich metadata (playing status, progress, duration) that
        # custom-file rows already expose in the UI, so we batch-fetch
        # per-podcast episode status via get_podcast_episodes() and splice
        # it in. This keeps the Up Next section a single source of truth
        # with no async "Loading metadata..." placeholder.
        up_next_episodes_list = []
        try:
            up_next_raw = _get_up_next_episodes(pc)

            regular_podcasts = {
                ep.get("podcast") for ep in up_next_raw
                if ep.get("podcast")
                and ep.get("podcast") != "da7aba5e-f11e-f11e-f11e-da7aba5ef11e"
                and ep.get("title", "") not in swept_titles
            }
            status_by_ep: dict[str, dict] = {}
            for puuid in regular_podcasts:
                try:
                    pc_eps = pc.get_podcast_episodes(puuid)
                    for pce in pc_eps:
                        eid = pce.get("uuid")
                        if eid:
                            status_by_ep[eid] = pce
                except Exception as exc:  # noqa: BLE001
                    log.debug(f"Could not fetch episodes for {puuid}: {exc}")

            for ep in up_next_raw:
                title = ep.get("title", "")
                if title in swept_titles:
                    continue
                ep_uuid = ep.get("uuid", "")
                status = status_by_ep.get(ep_uuid, {})
                up_next_episodes_list.append({
                    "uuid": ep_uuid,
                    "title": title,
                    "podcast_uuid": ep.get("podcast", ""),
                    "url": ep.get("url", ""),
                    "published": ep.get("published", ""),
                    "duration": int(status.get("duration") or ep.get("duration") or 0),
                    "playing_status": int(status.get("playingStatus") or 0),
                    "played_up_to": int(status.get("playedUpTo") or 0),
                    "is_archived": bool(status.get("isDeleted")),
                    "starred": bool(status.get("starred")),
                })
        except Exception as e:
            log.debug(f"Could not fetch Up Next: {e}")

        # Fetch episode play status per podcast from new releases
        episode_status = {}
        new_releases = []
        try:
            new_releases = pc.get_new_releases()
            for ep in new_releases:
                puuid = ep.get("podcastUuid", "")
                title = ep.get("title", "").strip()
                status = ep.get("playingStatus", 0)
                if puuid and title:
                    if puuid not in episode_status:
                        episode_status[puuid] = {}
                    episode_status[puuid][title] = status
        except Exception:
            pass

        # Determine which podcasts have processed episodes
        # Build a reverse map: feed_slug -> podcast_uuid using RSS URL matching
        # AND title matching as fallback
        processed_podcast_uuids = set()
        mp = MinusPodClient()
        try:
            feeds = mp.list_feeds()
            slug_to_uuid = {}
            # Match by RSS URL
            for p in subs:
                rss_url = (p.get("url") or "")
                ptitle = (p.get("title") or "").lower().strip()
                for f in feeds:
                    if rss_url and f.get("sourceUrl") == rss_url:
                        slug_to_uuid[f["slug"]] = p.get("uuid")
                        break
                    ftitle = (f.get("title") or "").lower().strip()
                    if ptitle and ftitle and ptitle == ftitle:
                        slug_to_uuid[f["slug"]] = p.get("uuid")
                        break

            for state_key in processed:
                slug = state_key.split(":")[0] if ":" in state_key else ""
                if slug in slug_to_uuid:
                    processed_podcast_uuids.add(slug_to_uuid[slug])
        except Exception:
            pass

        result = []
        patreon_count = 0
        for p in subs:
            is_pat = is_patreon_feed(p)
            if is_pat:
                patreon_count += 1
            result.append({
                "uuid": p.get("uuid"),
                "title": p.get("title", ""),
                "author": p.get("author", ""),
                "is_patreon": is_pat,
            })

        result.sort(key=lambda x: (x["is_patreon"], x["title"].lower()))
        eligible = len(result) - patreon_count

        return jsonify({
            "podcasts": result,
            "total": len(result),
            "eligible": eligible,
            "patreon": patreon_count,
            "processed_count": len(processed),
            "up_next_episodes": up_next_episodes_list,
            "episode_status": episode_status,
            "processed_podcast_uuids": list(processed_podcast_uuids),
        })

    def _get_up_next_episodes(pc):
        """Fetch the current Up Next episode list from Pocket Casts."""
        try:
            resp = pc.client.post(
                f"https://api.pocketcasts.com/up_next/sync",
                headers=pc._headers(),
                json={
                    "deviceTime": int(time.time() * 1000),
                    "version": "2",
                    "upNext": {"serverModified": 0, "changes": []},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("episodes", [])
        except Exception:
            return []

    def _reconcile_up_next_originals(pc, processed: dict) -> set[str]:
        """Remove original (non-Ad-Free) episodes from Up Next when an
        Ad-Free counterpart already exists — either uploaded to Pocket Casts
        or recorded in local processed state.

        Returns the set of titles that were swept so callers can filter
        stale Up Next responses returned by Pocket Casts on the same tick.
        """
        swept: set[str] = set()
        try:
            up_next = _get_up_next_episodes(pc)
        except Exception:
            return swept
        if not up_next:
            return swept

        # Build set of ad-free titles we already have (minus the (Ad-Free) tag).
        adfree_titles: set[str] = set()
        try:
            files_resp = pc.get_files() or {}
            for f in files_resp.get("files", []) if isinstance(files_resp, dict) else []:
                t = (f.get("title") or "").strip()
                if t.endswith(" (Ad-Free)"):
                    adfree_titles.add(_normalize_title(t[: -len(" (Ad-Free)")]))
        except Exception:
            pass
        for meta in processed.values():
            t = (meta.get("title") or "").strip()
            if t:
                adfree_titles.add(_normalize_title(t))

        if not adfree_titles:
            return swept

        for ep in up_next:
            ep_uuid = ep.get("uuid") or ""
            title = (ep.get("title") or "").strip()
            podcast_uuid = ep.get("podcast") or ""
            if not ep_uuid or not title:
                continue
            if "(Ad-Free)" in title:
                continue  # already processed upload — keep it
            if podcast_uuid == "da7aba5e-f11e-f11e-f11e-da7aba5ef11e":
                continue  # custom-files virtual podcast — not an original
            norm = _normalize_title(title)
            if norm not in adfree_titles:
                continue
            try:
                pc.remove_from_up_next(ep_uuid)
                if podcast_uuid:
                    try:
                        pc.mark_episode_played(ep_uuid, podcast_uuid)
                    except Exception:
                        pass
                swept.add(title)
                log.info(f"Reconciled: removed stale original from Up Next — {title[:60]}")
            except Exception as exc:  # noqa: BLE001
                log.debug(f"Could not sweep {title[:40]}: {exc}")
        return swept

    @app.route("/api/episodes/<podcast_uuid>")
    def api_episodes(podcast_uuid):
        """Get episodes for a podcast via MinusPod (resolves RSS, adds feed if needed)."""
        pc = get_pc()
        subs = pc.get_subscriptions()
        pod = next((s for s in subs if s.get("uuid") == podcast_uuid), {})
        title = pod.get("title", podcast_uuid)

        rss_url = find_rss_url_for_podcast(podcast_uuid, subscription_data=pod)
        if not rss_url:
            return jsonify({"episodes": [], "error": f"Could not find RSS for {title}"})

        mp = MinusPodClient()
        existing_feeds = mp.list_feeds()
        feed_slug = None
        for f in existing_feeds:
            if f.get("sourceUrl") == rss_url:
                feed_slug = f["slug"]
                break

        if not feed_slug:
            try:
                result = mp.add_feed(rss_url, max_episodes=10)
                feed_slug = result.get("slug")
                time.sleep(3)
            except Exception as e:
                return jsonify({"episodes": [], "error": str(e)})

        try:
            episodes = mp.get_episodes(feed_slug)
        except Exception as e:
            return jsonify({"episodes": [], "error": str(e)})

        # Fetch Pocket Casts' own episode metadata so we can surface play
        # status + expose the PC episode UUID needed for Queue/Un-queue
        # actions. Two endpoints are combined because neither is sufficient
        # on its own:
        #   • The public podcast feed API (unauthenticated) returns TITLES
        #     but no per-user status.
        #   • The authenticated /user/podcast/episodes call returns status
        #     (playingStatus, isDeleted, playedUpTo) keyed by episode UUID
        #     but WITHOUT titles.
        # Merging them on UUID gives us both.
        pc_titles_to_uuid: dict[str, str] = {}
        try:
            import httpx
            with httpx.Client(timeout=30, follow_redirects=True) as _c:
                feed = _c.get(
                    f"https://podcast-api.pocketcasts.com/podcast/full/{podcast_uuid}/0/3/500"
                )
                if feed.status_code == 200:
                    for pce in (feed.json().get("podcast") or {}).get("episodes", []) or []:
                        t_norm = _normalize_title(pce.get("title") or "")
                        u = pce.get("uuid") or ""
                        if t_norm and u and t_norm not in pc_titles_to_uuid:
                            pc_titles_to_uuid[t_norm] = u
        except Exception as e:  # noqa: BLE001
            log.debug(f"Could not fetch public podcast feed for {podcast_uuid}: {e}")

        pc_status_by_uuid: dict[str, dict] = {}
        try:
            for pce in (pc.get_podcast_episodes(podcast_uuid) or []):
                u = pce.get("uuid") or ""
                if u:
                    pc_status_by_uuid[u] = pce
        except Exception as e:  # noqa: BLE001
            log.debug(f"Could not fetch PC status for {podcast_uuid}: {e}")

        # Build set of Up Next episode UUIDs so we can render an accurate
        # Queue/Un-queue toggle per episode.
        up_next_uuids: set[str] = set()
        try:
            for ep in _get_up_next_episodes(pc):
                u = ep.get("uuid")
                if u:
                    up_next_uuids.add(u)
        except Exception:
            pass

        state = load_state()
        ep_list = []
        for ep in episodes:
            ep_id = ep.get("id") or ep.get("episodeId")
            state_key = f"{feed_slug}:{ep_id}"
            title = ep.get("title", "?")
            pc_ep_uuid = pc_titles_to_uuid.get(_normalize_title(title), "")
            pce = pc_status_by_uuid.get(pc_ep_uuid, {}) if pc_ep_uuid else {}
            ep_list.append({
                "id": ep_id,
                "title": title,
                "duration": ep.get("duration"),
                "published": ep.get("published") or ep.get("createdAt"),
                "status": ep.get("status", "discovered"),
                "already_processed": state_key in state.get("processed", {}),
                "feed_slug": feed_slug,
                "pc_episode_uuid": pc_ep_uuid,
                "pc_playing_status": pce.get("playingStatus"),
                "pc_archived": bool(pce.get("isDeleted")),
                "in_up_next": bool(pc_ep_uuid) and pc_ep_uuid in up_next_uuids,
            })

        return jsonify({"episodes": ep_list, "feed_slug": feed_slug, "rss_url": rss_url, "pc_podcast_uuid": podcast_uuid})

    @app.route("/api/process", methods=["POST"])
    def api_process():
        data = request.get_json()
        selections = data.get("selections", {})
        if not selections:
            return jsonify({"error": "No episodes selected"}), 400

        job_id = str(uuid_mod.uuid4())
        skip_event = threading.Event()
        stop_event = threading.Event()

        processing_jobs[job_id] = {
            "status": "queued",
            "logs": [],
            "processed": 0,
            "uploaded": 0,
            "total_episodes": sum(len(v) for v in selections.values()),
            "current_episode": "",
            "log_cursor": 0,
            "skip_event": skip_event,
            "stop_event": stop_event,
            "selections": selections,
        }

        with queue_lock:
            job_queue.append(job_id)

        _maybe_start_next_job()

        return jsonify({"job_id": job_id})

    def _maybe_start_next_job():
        """Start the next job from the queue if nothing is actively running."""
        global active_job_id
        with queue_lock:
            if active_job_id and processing_jobs.get(active_job_id, {}).get("status") == "running":
                return
            while job_queue:
                next_id = job_queue.popleft()
                job = processing_jobs.get(next_id)
                if not job or job["status"] != "queued":
                    continue
                active_job_id = next_id
                job["status"] = "running"
                thread = threading.Thread(
                    target=_process_job,
                    args=(next_id, job["selections"]),
                    daemon=True,
                )
                thread.start()
                return

    @app.route("/api/queue/status")
    def api_queue_status():
        """Return info about the active job and queue depth.

        Aggregates episode counts across all jobs (active + queued) so the
        UI can show a single coherent progress indicator.
        """
        active = None
        queued_episode_count = 0

        with queue_lock:
            for qid in job_queue:
                qjob = processing_jobs.get(qid)
                if qjob:
                    queued_episode_count += qjob.get("total_episodes", 0)

        if active_job_id:
            job = processing_jobs.get(active_job_id)
            if job and job["status"] == "running":
                active = {
                    "job_id": active_job_id,
                    "processed": job["processed"],
                    "uploaded": job["uploaded"],
                    "total_episodes": job["total_episodes"],
                    "current_episode": job.get("current_episode", ""),
                }

        return jsonify({
            "active_job": active,
            "queued_episodes": queued_episode_count,
        })

    @app.route("/api/files", methods=["GET"])
    def api_files_list():
        """List all uploaded custom files on Pocket Casts.

        Returns sorted by modifiedAt (newest first). Each file is augmented
        with `ad_free: bool` for easy client filtering.
        """
        pc = get_pc()
        try:
            data = pc.get_files()
        except Exception as e:
            return jsonify({"error": str(e), "files": []}), 500
        files = data.get("files", [])
        out = []
        for f in files:
            title = f.get("title", "")
            out.append({
                "uuid": f.get("uuid"),
                "title": title,
                "size": int(f.get("size", 0) or 0),
                "duration": int(f.get("duration", 0) or 0),
                "published": f.get("published", ""),
                "modified_at": f.get("modifiedAt", ""),
                "played_up_to": int(f.get("playedUpTo", 0) or 0),
                "playing_status": int(f.get("playingStatus", 0) or 0),
                "has_custom_image": bool(f.get("hasCustomImage")),
                "image_status": f.get("imageStatus"),
                "image_url": f.get("imageUrl", ""),
                "ad_free": "(Ad-Free)" in title,
            })
        out.sort(key=lambda x: x["modified_at"], reverse=True)
        return jsonify({"files": out})

    @app.route("/api/files/<file_uuid>", methods=["DELETE"])
    def api_files_delete(file_uuid):
        """Delete a single uploaded custom file from Pocket Casts cloud."""
        pc = get_pc()
        ok = pc.delete_file(file_uuid)
        if ok:
            _prune_state_for_file(file_uuid)
        return jsonify({"ok": ok})

    @app.route("/api/files/<file_uuid>", methods=["PATCH"])
    def api_files_patch(file_uuid):
        """Update a custom file's metadata (title, played status, etc.).

        Body fields (all optional):
          - title:           new episode title
          - playing_status:  0=unplayed, 2=in_progress, 3=played
          - played_up_to:    position in seconds
        """
        pc = get_pc()
        data = request.get_json() or {}
        kwargs = {}
        if "title" in data:
            kwargs["title"] = data["title"]
        if "playing_status" in data:
            kwargs["playingStatus"] = int(data["playing_status"])
        if "played_up_to" in data:
            kwargs["playedUpTo"] = int(data["played_up_to"])
        ok = pc.update_file(file_uuid, **kwargs)
        return jsonify({"ok": ok})

    @app.route("/api/files/<file_uuid>/up_next", methods=["DELETE"])
    def api_files_remove_up_next(file_uuid):
        """Remove an uploaded file from the Up Next queue."""
        pc = get_pc()
        try:
            pc.remove_from_up_next(file_uuid)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/pc_episode/<episode_uuid>/up_next", methods=["DELETE"])
    def api_pc_episode_remove_up_next(episode_uuid):
        """Remove a regular (non-custom-file) podcast episode from Up Next."""
        pc = get_pc()
        try:
            pc.remove_from_up_next(episode_uuid)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/pc_episode/<episode_uuid>/up_next", methods=["POST"])
    def api_pc_episode_add_up_next(episode_uuid):
        """Queue a regular podcast episode (by Pocket Casts episode UUID).

        Body: {podcast_uuid: str, title: str, play_last?: bool}
        """
        pc = get_pc()
        data = request.get_json() or {}
        podcast_uuid = data.get("podcast_uuid") or ""
        title = data.get("title") or ""
        play_last = bool(data.get("play_last", True))
        if not podcast_uuid:
            return jsonify({"ok": False, "error": "podcast_uuid required"}), 400
        try:
            now_ms = int(time.time() * 1000)
            server_modified = pc._get_up_next_server_modified()
            resp = pc.client.post(
                "https://api.pocketcasts.com/up_next/sync",
                headers=pc._headers(),
                json={
                    "deviceTime": now_ms,
                    "version": "2",
                    "upNext": {
                        "serverModified": server_modified,
                        "changes": [{
                            "action": 3 if play_last else 2,
                            "modified": now_ms,
                            "uuid": episode_uuid,
                            "title": title,
                            "podcast": podcast_uuid,
                        }],
                    },
                },
            )
            resp.raise_for_status()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/pc_episode/<episode_uuid>/played", methods=["POST"])
    def api_pc_episode_set_played(episode_uuid):
        """Set played status on a regular podcast episode.

        Body: {podcast_uuid: str, played: bool}
        """
        pc = get_pc()
        data = request.get_json() or {}
        podcast_uuid = data.get("podcast_uuid") or ""
        played = bool(data.get("played", True))
        if not podcast_uuid:
            return jsonify({"ok": False, "error": "podcast_uuid required"}), 400
        try:
            resp = pc.client.post(
                "https://api.pocketcasts.com/sync/update_episode",
                headers=pc._headers(),
                json={
                    "uuid": episode_uuid,
                    "podcast": podcast_uuid,
                    "status": 3 if played else 0,
                },
            )
            resp.raise_for_status()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/files/cleanup_played", methods=["POST"])
    def api_files_cleanup_played():
        """Delete all uploaded Ad-Free files that are marked played.

        Body (optional):
          - include_in_progress: bool — also delete files that are in-progress
                                 with >90% played.
        Returns `{deleted: [<uuid>], kept: [<uuid>]}`.
        """
        pc = get_pc()
        data = request.get_json() or {}
        include_in_progress = bool(data.get("include_in_progress", False))
        try:
            files = pc.get_files().get("files", [])
        except Exception as e:
            return jsonify({"error": str(e), "deleted": [], "kept": []}), 500
        deleted, kept = [], []
        for f in files:
            title = f.get("title", "")
            if "(Ad-Free)" not in title:
                kept.append(f.get("uuid"))
                continue
            status = int(f.get("playingStatus", 0) or 0)
            played_up_to = int(f.get("playedUpTo", 0) or 0)
            duration = int(f.get("duration", 0) or 0)
            is_played = status == 3
            near_end = (
                include_in_progress
                and status == 2
                and duration > 0
                and (played_up_to / duration) >= 0.9
            )
            if is_played or near_end:
                if pc.delete_file(f.get("uuid")):
                    deleted.append(f.get("uuid"))
                    _prune_state_for_file(f.get("uuid"))
                else:
                    kept.append(f.get("uuid"))
            else:
                kept.append(f.get("uuid"))
        return jsonify({"deleted": deleted, "kept": kept})

    def _prune_state_for_file(file_uuid: str):
        """Drop a `processed` state entry if its file_uuid matches — allows the
        user to re-process the same source episode after deletion."""
        try:
            state = load_state()
            processed = state.get("processed", {})
            to_remove = [k for k, v in processed.items() if v.get("file_uuid") == file_uuid]
            for k in to_remove:
                processed.pop(k, None)
            if to_remove:
                save_state(state)
                log.info(f"Pruned {len(to_remove)} processed state entr(ies) for file {file_uuid[:12]}")
        except Exception as e:
            log.debug(f"State prune failed: {e}")

    @app.route("/api/processed", methods=["GET"])
    def api_processed_list():
        """Return all processed-episode state entries so the UI can offer
        a per-entry 'Reset' action."""
        state = load_state()
        processed = state.get("processed", {})
        items = []
        for key, meta in processed.items():
            slug, _, ep_id = key.partition(":")
            items.append({
                "key": key,
                "slug": slug,
                "episode_id": ep_id,
                "title": meta.get("title", ""),
                "file_uuid": meta.get("file_uuid", ""),
                "processed_at": meta.get("processed_at", ""),
            })
        items.sort(key=lambda x: x["processed_at"], reverse=True)
        return jsonify({"processed": items, "count": len(items)})

    @app.route("/api/processed", methods=["DELETE"])
    def api_processed_clear():
        """Clear processed markers. Body: {keys: [str]} to delete specific
        entries, or {all: true} to clear everything."""
        data = request.get_json() or {}
        state = load_state()
        processed = state.get("processed", {})
        if data.get("all"):
            removed = len(processed)
            state["processed"] = {}
            save_state(state)
            return jsonify({"removed": removed})
        keys = data.get("keys") or []
        removed = 0
        for k in keys:
            if processed.pop(k, None) is not None:
                removed += 1
        if removed:
            save_state(state)
        return jsonify({"removed": removed})

    @app.route("/api/processed/podcast/<podcast_uuid>", methods=["DELETE"])
    def api_processed_clear_podcast(podcast_uuid):
        """Clear processed markers for a single podcast (by Pocket Casts UUID)."""
        try:
            pc = get_pc()
            subs = pc.get_subscriptions()
            if isinstance(subs, dict):
                subs = subs.get("podcasts", [])
            podcast = next((p for p in (subs or []) if p.get("uuid") == podcast_uuid), None)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        if podcast is None:
            return jsonify({"error": "podcast not found"}), 404

        state = load_state()
        processed = state.get("processed", {})

        title_lc = (podcast.get("title") or "").lower()
        from re import sub as _sub
        slug_from_title = _sub(r"[^a-z0-9]+", "-", title_lc).strip("-")
        slug_candidates = {c for c in {title_lc, slug_from_title} if c}

        cleared = 0
        for key in list(processed.keys()):
            entry = processed[key]
            slug = key.split(":", 1)[0].lower()
            entry_title = (entry.get("title") or "").lower()
            entry_uuid = entry.get("podcast_uuid")
            if (
                entry_uuid == podcast_uuid
                or slug in slug_candidates
                or any(c and c in slug for c in slug_candidates)
                or any(c and c in entry_title for c in slug_candidates)
            ):
                processed.pop(key, None)
                cleared += 1
        if cleared:
            save_state(state)
        return jsonify({"cleared": cleared, "uuid": podcast_uuid})

    @app.route("/api/history", methods=["GET"])
    def api_history():
        """Return processing history derived from local state and live custom files.

        Each entry includes processed_at, title, podcast_title, ads_removed,
        time_saved_secs, original_size, new_size, pocket_casts_uuid.
        """
        state = load_state()
        processed = state.get("processed", {})
        try:
            pc = get_pc()
            files_resp = pc.get_files() or {}
            files = files_resp.get("files", []) if isinstance(files_resp, dict) else (files_resp or [])
        except Exception:  # noqa: BLE001
            files = []
        files_by_uuid = {(f.get("uuid") or ""): f for f in files}

        entries = []
        for key, meta in processed.items():
            slug, _, ep_id = key.partition(":")
            file_uuid = meta.get("file_uuid") or ""
            f = files_by_uuid.get(file_uuid) or {}
            entries.append({
                "key": key,
                "slug": slug,
                "episode_id": ep_id,
                "title": meta.get("title", ""),
                "podcast_title": meta.get("podcast_title") or slug.replace("-", " ").title(),
                "processed_at": meta.get("processed_at", ""),
                "ads_removed": meta.get("ads_removed"),
                "time_saved_secs": meta.get("time_saved_secs"),
                "original_size": meta.get("original_size") or f.get("original_size"),
                "new_size": meta.get("new_size") or f.get("size"),
                "pocket_casts_uuid": file_uuid or None,
                "deleted": bool(file_uuid) and file_uuid not in files_by_uuid,
            })
        entries.sort(key=lambda e: e.get("processed_at") or "", reverse=True)
        return jsonify({"entries": entries, "count": len(entries)})

    # ─── Services panel ────────────────────────────────────────────────────
    @app.route("/api/services", methods=["GET"])
    def api_services_list():
        """Return live status for ollama, whisper, minuspod, ui."""
        return jsonify({
            "services": [s.as_dict() for s in services_manager.all_statuses()]
        })

    @app.route("/api/services/<service_id>/<action>", methods=["POST"])
    def api_services_action(service_id, action):
        """Start, stop, or restart a service.

        Body (optional, JSON):
          - backend: "native" | "docker"   (whisper start/restart only)
        """
        if action not in ("start", "stop", "restart"):
            return jsonify({"error": f"unknown action: {action}"}), 400
        body = request.get_json(silent=True) or {}
        try:
            result = services_manager.perform_action(
                service_id, action, **body,
            )
            return jsonify(result)
        except services_manager.ServiceError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            log.exception("Service action failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/services/<service_id>/log", methods=["GET"])
    def api_services_log(service_id):
        """Return the tail of the service's log file. ?lines=N (default 200)."""
        statuses = {s.id: s for s in services_manager.all_statuses()}
        svc = statuses.get(service_id)
        if not svc:
            return jsonify({"error": "unknown service"}), 404
        lines = max(1, min(int(request.args.get("lines", 200)), 2000))
        from pathlib import Path as _Path
        path = _Path(svc.log_path) if svc.log_path else None
        text = services_manager._read_log_tail(path, lines=lines) if path else ""
        return jsonify({
            "service": service_id,
            "log_path": str(path) if path else "",
            "exists": bool(path and path.exists()),
            "text": text,
        })

    @app.route("/api/services/ollama/model", methods=["GET", "PUT"])
    def api_services_ollama_model():
        """GET: list installed models + currently active MinusPod model.
        PUT { model: "<name>" }: tell MinusPod to use that model."""
        if request.method == "GET":
            return jsonify({
                "models": services_manager.list_ollama_models(),
                "current": services_manager.get_minuspod_model(),
            })
        body = request.get_json() or {}
        try:
            return jsonify(services_manager.set_minuspod_model(body.get("model", "")))
        except services_manager.ServiceError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/job/<job_id>")
    def api_job(job_id):
        job = processing_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        cursor = int(request.args.get("cursor", 0))
        new_logs = job["logs"][cursor:]

        return jsonify({
            "status": job["status"],
            "processed": job["processed"],
            "uploaded": job["uploaded"],
            "new_logs": new_logs,
        })

    @app.route("/api/job/<job_id>/skip", methods=["POST"])
    def api_skip(job_id):
        job = processing_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        job["skip_event"].set()
        # Unload models now — MinusPod will keep the LLM loaded until
        # its JIT job finishes, so unloading Ollama immediately frees GPU.
        threading.Thread(target=_cleanup_after_stop, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/job/<job_id>/stop", methods=["POST"])
    def api_stop(job_id):
        job = processing_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        job["stop_event"].set()
        job["skip_event"].set()
        # Immediately unload models so fans quiet down — don't wait
        # for the job thread to finish its current retry loop.
        threading.Thread(target=_cleanup_after_stop, daemon=True).start()
        return jsonify({"ok": True})

    def _cleanup_after_stop():
        """Unload models and disable auto-processing after a stop request."""
        try:
            unload_ollama_models()
            MinusPodClient().disable_auto_process()
        except Exception:
            pass

    def _job_log(job_id, level, msg):
        job = processing_jobs.get(job_id)
        if job:
            job["logs"].append({"level": level, "msg": msg})
            log.info(f"[job:{job_id[:8]}] {msg}")

    def _match_mp_episode_by_title(
        mp, feed_slug: str, pc_title: str,
        rss_url: str | None = None, job_log=None,
    ) -> dict | None:
        """Find the MinusPod episode in `feed_slug` whose title matches
        `pc_title`. Uses the same normalization as the rest of the pipeline
        (exact-normalized, then substring-normalized).

        If no match is found AND `rss_url` is provided, re-add the feed with
        a larger window (maxEpisodes=500) so older Up Next items become
        matchable, then retry once.
        """
        def _find(episodes: list[dict]) -> dict | None:
            target = _normalize_title(pc_title)
            if not target:
                return None
            for e in episodes:
                if _normalize_title(e.get("title", "")) == target:
                    return e
            # substring fallback
            for e in episodes:
                t = _normalize_title(e.get("title", ""))
                if target in t or t in target:
                    return e
            return None

        try:
            episodes = mp.get_episodes(feed_slug)
        except Exception as e:
            if job_log:
                job_log("warn", f"  MinusPod get_episodes failed: {e}")
            return None

        match = _find(episodes)
        if match:
            return match

        # Not found — ask MinusPod to ingest more history. add_feed with the
        # same sourceUrl is idempotent server-side (returns 409) but also
        # accepts a larger maxEpisodes; if it 409s, try a refresh instead.
        if not rss_url:
            return None
        if job_log:
            job_log(
                "info",
                f"  Title not found in MinusPod feed; expanding window to 500 and retrying...",
            )
        try:
            mp.add_feed(rss_url, max_episodes=500)
        except Exception:
            # Likely 409 already-exists — fall through to refresh.
            try:
                mp.client.post(
                    f"{mp.base_url}/api/v1/feeds/{feed_slug}/refresh", timeout=30
                )
            except Exception as e:
                if job_log:
                    job_log("warn", f"  Feed refresh failed: {e}")
                return None
        time.sleep(5)
        try:
            episodes = mp.get_episodes(feed_slug)
        except Exception as e:
            if job_log:
                job_log("warn", f"  MinusPod get_episodes (retry) failed: {e}")
            return None
        return _find(episodes)

    def _process_job(job_id, selections):
        """Process selected episodes.

        selections: dict mapping podcast_uuid -> list of episode_ids
        """
        global active_job_id
        job = processing_jobs[job_id]
        skip_event = job["skip_event"]
        stop_event = job["stop_event"]

        try:
            pc = get_pc()
            mp = MinusPodClient()
            mp.disable_auto_process()
            mp.set_fast_system_prompt()
            mp.lower_confidence_threshold()
            state = load_state()

            subs = pc.get_subscriptions()
            sub_map = {s["uuid"]: s for s in subs}

            for puuid, episode_ids in selections.items():
                if stop_event.is_set():
                    _job_log(job_id, "warn", "Job stopped by user.")
                    break

                pod = sub_map.get(puuid, {})
                title = pod.get("title", puuid)
                ep_count = len(episode_ids)
                _job_log(job_id, "info", f"Starting: {title} ({ep_count} episode{'s' if ep_count != 1 else ''})")
                is_custom_file = (puuid == '_files' or puuid == 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e')
                rss_url = None
                feed_slug = None

                if is_custom_file:
                    _job_log(job_id, "info", "Processing custom files directly...")
                else:
                    if is_patreon_feed(pod):
                        _job_log(job_id, "warn", f"Skipping Patreon feed: {title}")
                        continue

                    rss_url = find_rss_url_for_podcast(puuid, subscription_data=pod)
                    if not rss_url:
                        _job_log(job_id, "warn", f"Could not find RSS for: {title}")
                        continue

                    _job_log(job_id, "info", f"RSS: {rss_url}")

                    existing_feeds = mp.list_feeds()
                    feed_slug = None
                    for f in existing_feeds:
                        if f.get("sourceUrl") == rss_url:
                            feed_slug = f["slug"]
                            break

                    if not feed_slug:
                        try:
                            result = mp.add_feed(rss_url, max_episodes=10)
                            feed_slug = result.get("slug")
                            _job_log(job_id, "info", f"Added feed to MinusPod: {feed_slug}")
                            time.sleep(5)
                        except Exception as e:
                            _job_log(job_id, "error", f"Failed to add feed: {e}")
                            continue

                ep_map = {}
                if is_custom_file:
                    # Custom files aren't in MinusPod feeds yet, but they are in the selection
                    # We can use the Up Next data to populate the map
                    up_next_raw = _get_up_next_episodes(pc)
                    for ep in up_next_raw:
                        p_uuid = ep.get("podcast")
                        if p_uuid == 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e' or not p_uuid or p_uuid == "_files":
                            eid = ep.get("uuid")
                            ep_map[eid] = {
                                "id": eid,
                                "title": ep.get("title", ""),
                                "url": ep.get("url", ""),
                                "published": ep.get("published", ""),
                                "duration": int(ep.get("duration", 0) if ep.get("duration") else 0),
                            }
                else:
                    try:
                        all_episodes = mp.get_episodes(feed_slug)
                    except Exception as e:
                        _job_log(job_id, "error", f"Failed to get episodes: {e}")
                        continue

                    for ep in all_episodes:
                        eid = ep.get("id") or ep.get("episodeId")
                        ep_map[eid] = ep

                pc_episode_map = {}
                try:
                    pc_eps = pc.get_podcast_episodes(puuid)
                    for pce in pc_eps:
                        pc_episode_map[_normalize_title(pce.get("title", ""))] = pce["uuid"]
                except Exception:
                    pass

                try:
                    pc_eps = pc.get_new_releases()
                    for pce in pc_eps:
                        if pce.get("podcastUuid") == puuid:
                            pc_episode_map[_normalize_title(pce.get("title", ""))] = pce["uuid"]
                except Exception:
                    pass

                try:
                    up_next_eps = _get_up_next_episodes(pc)
                    for pce in up_next_eps:
                        p_uuid = pce.get("podcast")
                        if p_uuid == puuid or (is_custom_file and (p_uuid == 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e' or not p_uuid)):
                            pc_episode_map[_normalize_title(pce.get("title", ""))] = pce.get("uuid")
                except Exception:
                    pass

                for ep_id in episode_ids:
                    if stop_event.is_set():
                        _job_log(job_id, "warn", "Job stopped by user.")
                        break

                    # Only clear skip_event if stop wasn't requested.
                    # stop sets both stop_event AND skip_event; clearing
                    # skip_event here would let the next episode proceed.
                    if not stop_event.is_set():
                        skip_event.clear()

                    ep = ep_map.get(ep_id)
                    if not ep and not is_custom_file:
                        # The selected `ep_id` is a Pocket Casts episode UUID,
                        # which won't match MinusPod's internal episode IDs.
                        # Strategy: find the episode's TITLE from Pocket Casts
                        # (Up Next / new releases / feed listing), then match
                        # that title against the MinusPod episode list for the
                        # same feed. If MinusPod doesn't have it yet, refresh
                        # with a larger window and retry once.
                        pc_title = None
                        pc_url = ""
                        pc_published = ""
                        pc_duration = 0
                        try:
                            for pce in _get_up_next_episodes(pc):
                                if pce.get("uuid") == ep_id:
                                    pc_title = pce.get("title", "")
                                    pc_url = pce.get("url", "")
                                    pc_published = pce.get("published", "")
                                    pc_duration = int(pce.get("duration", 0) or 0)
                                    break
                        except Exception:
                            pass
                        if not pc_title:
                            try:
                                for pce in pc.get_podcast_episodes(puuid):
                                    if pce.get("uuid") == ep_id:
                                        pc_title = pce.get("title", "")
                                        pc_url = pce.get("url", "")
                                        pc_published = pce.get("published", "")
                                        pc_duration = int(pce.get("duration", 0) or 0)
                                        break
                            except Exception:
                                pass

                        if pc_title and feed_slug:
                            mp_ep = _match_mp_episode_by_title(
                                mp, feed_slug, pc_title,
                                rss_url=rss_url if not is_custom_file else None,
                                job_log=lambda lvl, msg: _job_log(job_id, lvl, msg),
                            )
                            if mp_ep:
                                ep = dict(mp_ep)
                                ep_map[ep_id] = ep
                                # Remember the PC UUID mapping so downstream
                                # transcript / mark-as-played uses the correct
                                # PC episode.
                                pc_episode_map[_normalize_title(pc_title)] = ep_id
                                _job_log(
                                    job_id, "info",
                                    f"  Matched Up Next → MinusPod by title: "
                                    f"{pc_title[:60]} → {ep.get('id','')[:12]}"
                                )
                    if not ep:
                        _job_log(
                            job_id, "warn",
                            f"  Episode {ep_id} not found in MinusPod feed "
                            f"(no title-match either). Skipping."
                        )
                        continue

                    ep_title = ep.get("title", "?")
                    effective_slug = feed_slug if feed_slug else '_files'
                    state_key = f"{effective_slug}:{ep_id}"

                    # Only skip if it's in the state AND the title already reflects it's ad-free.
                    # This allows users to manually re-add "dirty" episodes to the queue to force re-processing.
                    if state_key in state.get("processed", {}) and "(Ad-Free)" in ep_title:
                        _job_log(job_id, "info", f"  Already processed: {ep_title}")
                        job["processed"] += 1
                        continue

                    job["current_episode"] = ep_title
                    _job_log(job_id, "info", f"  Processing: {ep_title}")

                    original_ep_uuid = pc_episode_map.get(_normalize_title(ep_title))
                    if not original_ep_uuid:
                        # Fallback: substring match for titles that differ
                        # between MinusPod (RSS) and Pocket Casts
                        norm = _normalize_title(ep_title)
                        for pc_title, pc_uuid in pc_episode_map.items():
                            if norm in pc_title or pc_title in norm:
                                original_ep_uuid = pc_uuid
                                break

                    try:
                        cb = lambda msg, _jid=job_id: _job_log(_jid, "info", f"    {msg}")
                        file_uuid = process_single_episode(
                            pc, mp, feed_slug, ep, output_dir, state,
                            progress_callback=cb,
                            skip_event=skip_event,
                            podcast_uuid=puuid,
                            original_episode_uuid=original_ep_uuid,
                        )
                        job["processed"] += 1
                        if skip_event.is_set() and not file_uuid:
                            _job_log(job_id, "warn", f"  Skipped: {ep_title}")
                            continue
                        if file_uuid:
                            job["uploaded"] += 1
                            _job_log(job_id, "success", f"  Uploaded & queued: {ep_title}")
                        else:
                            # file_uuid is None usually means it was already processed according to state check
                            if skip_event.is_set():
                                _job_log(job_id, "warn", f"  Skipped by user: {ep_title}")
                            else:
                                _job_log(job_id, "info", f"  Already processed: {ep_title}")
                    except Exception as e:
                        job["processed"] += 1
                        _job_log(job_id, "error", f"  Failed: {ep_title}: {e}")

            if stop_event.is_set():
                job["status"] = "stopped"
            else:
                job["status"] = "completed"
                total = job["uploaded"]
                _job_log(job_id, "success", f"All done! {total} episode(s) uploaded to Pocket Casts.")

            _job_log(job_id, "info", "Unloading models to free memory...")
            unload_ollama_models()
            _job_log(job_id, "info", "Models unloaded. Fans should quiet down shortly.")
        except Exception as e:
            job["status"] = "failed"
            _job_log(job_id, "error", f"Job failed: {e}")
            unload_ollama_models()
        finally:
            with queue_lock:
                if active_job_id == job_id:
                    active_job_id = None
            _maybe_start_next_job()

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5050, debug=True)
