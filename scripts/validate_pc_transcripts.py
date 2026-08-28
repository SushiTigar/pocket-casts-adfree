#!/usr/bin/env python3
"""Read-only harness: compare Pocket Casts transcripts against local Whisper data.

Loads episodes from MinusPod's SQLite DB (Whisper transcripts + ad markers),
fetches matching Pocket Casts generated VTTs, and reports:
  - coverage and proportional duration delta
  - raw and drift-corrected ad-marker overlap
  - fuzzy drift at probe points (Whisper segment text vs PC cues)
  - simulated production gate pass rate

Writes a JSON report to data/pc_transcript_validation.json by default.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from pocketcasts_adfree import (  # noqa: E402
    PocketCastsAuthError,
    PocketCastsClient,
    _align_sample,
    _get_vtt_duration,
    _normalize_title,
    _normalize_title_strong,
    _parse_minuspod_transcript,
    _parse_vtt_cues,
    _probe_times_for_duration,
    compare_ad_markers_in_transcripts,
    estimate_transcript_drift,
    normalize_feed_url,
    pc_transcript_coverage_metrics,
    pc_transcript_probe_count,
    resolve_pc_episode_uuid,
    simulate_pc_transcript_gate_from_segments,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("validate-pc-transcripts")

MINUSPOD_DB = ROOT / "MinusPod" / "data" / "podcast.db"
DEFAULT_OUTPUT = ROOT / "data" / "pc_transcript_validation.json"


def _load_episodes(
    db_path: Path,
    *,
    with_ads_only: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    query = """
        SELECT
            p.slug,
            p.title AS podcast_title,
            p.source_url AS rss_url,
            e.episode_id,
            e.title,
            e.original_url,
            e.original_duration,
            ed.original_transcript_text,
            ed.ad_markers_json
        FROM episodes e
        JOIN podcasts p ON e.podcast_id = p.id
        JOIN episode_details ed ON ed.episode_id = e.id
        WHERE ed.original_transcript_text IS NOT NULL
          AND length(ed.original_transcript_text) > 0
    """
    if with_ads_only:
        query += """
          AND ed.ad_markers_json IS NOT NULL
          AND ed.ad_markers_json != '[]'
        """
    query += " ORDER BY e.id DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


def _resolve_podcast_uuid(
    slug: str, rss_url: str, subscriptions: list[dict]
) -> str | None:
    slug_norm = _normalize_title(slug.replace("-", " "))
    rss_norm = normalize_feed_url(rss_url or "")
    for sub in subscriptions:
        sub_uuid = sub.get("uuid", "")
        sub_title = _normalize_title(sub.get("title", ""))
        sub_rss = normalize_feed_url(sub.get("url") or "")
        if sub_uuid and (slug_norm in sub_title or sub_title in slug_norm):
            return sub_uuid
        if rss_norm and sub_rss and rss_norm == sub_rss:
            return sub_uuid
    return None


def _get_catalog(
    pc: PocketCastsClient,
    podcast_uuid: str,
    cache: dict[str, list[dict]],
) -> list[dict]:
    if podcast_uuid not in cache:
        log.info("Fetching PC episode catalog for %s...", podcast_uuid[:12])
        cache[podcast_uuid] = pc.get_podcast_episodes_catalog(podcast_uuid)
    return cache[podcast_uuid]


def _measure_drift(
    whisper_segments: list[dict],
    pc_cues: list[dict],
    duration: float,
    n_probes: int,
) -> list[dict[str, Any]]:
    probes = []
    for t in _probe_times_for_duration(duration, n_probes):
        from pocketcasts_adfree import _text_for_time_range

        sample = _text_for_time_range(whisper_segments, t, t + 20.0)
        if not sample.strip():
            continue
        ratio, matched = _align_sample(sample, pc_cues, t)
        probes.append(
            {
                "time": round(t, 1),
                "similarity": round(ratio, 3),
                "offset": round(matched - t, 2),
            }
        )
    return probes


def validate_episode(
    row: dict[str, Any],
    pc: PocketCastsClient | None,
    subscriptions: list[dict],
    catalog_cache: dict[str, list[dict]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "slug": row["slug"],
        "episode_id": row["episode_id"],
        "title": row["title"],
        "podcast_title": row["podcast_title"],
        "status": "pending",
    }

    whisper_segments = _parse_minuspod_transcript(
        row["original_transcript_text"] or ""
    )
    duration = float(row["original_duration"] or 0)
    if not duration and whisper_segments:
        duration = whisper_segments[-1]["end"]
    result["audio_duration"] = duration
    result["whisper_segments"] = len(whisper_segments)

    ad_markers = []
    if row.get("ad_markers_json"):
        try:
            ad_markers = json.loads(row["ad_markers_json"])
        except json.JSONDecodeError:
            ad_markers = []
    result["ad_count"] = len(ad_markers)

    if not pc:
        result["status"] = "skipped_no_pc_client"
        return result

    podcast_uuid = _resolve_podcast_uuid(
        row["slug"], row.get("rss_url") or "", subscriptions
    )
    if not podcast_uuid:
        result["status"] = "skipped_no_podcast_match"
        return result

    result["podcast_uuid"] = podcast_uuid
    catalog = _get_catalog(pc, podcast_uuid, catalog_cache)
    if not catalog:
        result["status"] = "skipped_no_catalog"
        return result

    episode_uuid = resolve_pc_episode_uuid(
        row["title"],
        catalog,
        audio_url=row.get("original_url") or "",
        duration=duration,
    )
    if not episode_uuid:
        result["status"] = "skipped_no_episode_match"
        result["catalog_size"] = len(catalog)
        result["title_norm_strong"] = _normalize_title_strong(row["title"])
        return result

    result["episode_uuid"] = episode_uuid

    vtt = pc.get_transcript_vtt(podcast_uuid, episode_uuid)
    if not vtt:
        result["status"] = "no_pc_vtt"
        return result

    pc_cues = _parse_vtt_cues(vtt)
    vtt_duration = _get_vtt_duration(vtt)
    cov = pc_transcript_coverage_metrics(vtt_duration, duration)

    drift_probes = _measure_drift(
        whisper_segments,
        pc_cues,
        duration,
        pc_transcript_probe_count(),
    )
    estimated_drift = estimate_transcript_drift(drift_probes)
    gate = simulate_pc_transcript_gate_from_segments(
        whisper_segments, pc_cues, duration
    )

    ad_comparisons = compare_ad_markers_in_transcripts(
        whisper_segments, pc_cues, ad_markers, drift=0.0
    )
    ad_comparisons_drift = compare_ad_markers_in_transcripts(
        whisper_segments, pc_cues, ad_markers, drift=estimated_drift
    )

    result.update(
        {
            "status": "ok",
            "pc_cue_count": len(pc_cues),
            "vtt_duration": vtt_duration,
            "coverage": round(cov["coverage"], 4),
            "duration_delta": round(cov["duration_delta"], 2),
            "max_duration_delta": round(cov["max_duration_delta"], 2),
            "coverage_pass": cov["coverage_pass"],
            "duration_pass": cov["duration_pass"],
            "ad_comparisons": ad_comparisons,
            "ad_comparisons_drift_corrected": ad_comparisons_drift,
            "estimated_drift": round(estimated_drift, 2),
            "drift_probes": drift_probes,
            "gate_simulation": gate,
            "gate_pass_simulated": gate.get("gate_pass_simulated", False),
        }
    )

    ads_with_pc = [
        a for a in ad_comparisons if a.get("ad_present_in_pc") is True
    ]
    ads_with_pc_drift = [
        a for a in ad_comparisons_drift if a.get("ad_present_in_pc") is True
    ]
    result["ads_present_in_pc"] = len(ads_with_pc)
    result["ads_present_in_pc_drift_corrected"] = len(ads_with_pc_drift)
    result["ads_missing_from_pc"] = len(ad_markers) - len(ads_with_pc)
    result["ads_missing_from_pc_drift_corrected"] = (
        len(ad_markers) - len(ads_with_pc_drift)
    )

    if drift_probes:
        result["max_drift_offset"] = max(
            abs(p["offset"]) for p in drift_probes
        )
        result["min_drift_similarity"] = min(
            p["similarity"] for p in drift_probes
        )
    return result


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {
        status: sum(1 for r in results if r.get("status") == status)
        for status in sorted({r.get("status", "?") for r in results})
    }
    ok = [r for r in results if r.get("status") == "ok"]
    summary: dict[str, Any] = {
        "episodes_total": len(results),
        "episodes_ok": len(ok),
        "status_counts": status_counts,
    }
    if not ok:
        return summary

    coverages = [r["coverage"] for r in ok]
    deltas = [r["duration_delta"] for r in ok]
    max_drifts = [r.get("max_drift_offset", 0) for r in ok if r.get("drift_probes")]
    min_sims = [
        r.get("min_drift_similarity", 0) for r in ok if r.get("drift_probes")
    ]

    total_ads = sum(r.get("ad_count", 0) for r in ok)
    ads_in_pc = sum(r.get("ads_present_in_pc", 0) for r in ok)
    ads_in_pc_drift = sum(r.get("ads_present_in_pc_drift_corrected", 0) for r in ok)
    gate_pass = sum(1 for r in ok if r.get("gate_pass_simulated"))

    summary.update(
        {
            "coverage_pass_rate": sum(1 for r in ok if r["coverage_pass"]) / len(ok),
            "duration_pass_rate": sum(1 for r in ok if r["duration_pass"]) / len(ok),
            "gate_pass_rate_simulated": gate_pass / len(ok),
            "coverage_min": min(coverages),
            "coverage_median": sorted(coverages)[len(coverages) // 2],
            "coverage_max": max(coverages),
            "duration_delta_max": max(deltas),
            "max_drift_offset_p95": (
                sorted(max_drifts)[int(len(max_drifts) * 0.95)]
                if max_drifts
                else 0
            ),
            "min_drift_similarity_median": (
                sorted(min_sims)[len(min_sims) // 2] if min_sims else 0
            ),
            "ads_present_in_pc_rate": (ads_in_pc / total_ads) if total_ads else None,
            "ads_present_in_pc_rate_drift_corrected": (
                (ads_in_pc_drift / total_ads) if total_ads else None
            ),
        }
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=MINUSPOD_DB,
        help="Path to MinusPod podcast.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON report path",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max episodes to validate",
    )
    parser.add_argument(
        "--with-ads-only",
        action="store_true",
        help="Only episodes that have ad_markers_json",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip Pocket Casts API (DB stats only)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        log.error("Database not found: %s", args.db)
        return 1

    episodes = _load_episodes(
        args.db, with_ads_only=args.with_ads_only, limit=args.limit
    )
    log.info("Loaded %d episode(s) from DB", len(episodes))

    pc = None
    subscriptions: list[dict] = []
    catalog_cache: dict[str, list[dict]] = {}

    if not args.offline:
        email = os.environ.get("POCKETCASTS_EMAIL", "")
        password = os.environ.get("POCKETCASTS_PASSWORD", "")
        if not email or not password:
            log.warning(
                "POCKETCASTS_EMAIL/PASSWORD not set — use --offline or set credentials"
            )
            args.offline = True
        else:
            try:
                pc = PocketCastsClient(email, password)
                subscriptions = pc.get_subscriptions()
                log.info("Loaded %d Pocket Casts subscription(s)", len(subscriptions))
            except PocketCastsAuthError as exc:
                log.error("Pocket Casts auth failed: %s", exc)
                return 1

    results = [
        validate_episode(row, pc, subscriptions, catalog_cache)
        for row in episodes
    ]
    summary = _summarize(results)

    report = {"summary": summary, "episodes": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    log.info("Wrote report to %s", args.output)

    print("\n=== PC Transcript Validation Summary ===")
    for key, val in summary.items():
        print(f"  {key}: {val}")

    ok_eps = [r for r in results if r.get("status") == "ok"]
    if ok_eps:
        print("\n=== Per-episode (ok) ===")
        for r in ok_eps[:20]:
            gate = "PASS" if r.get("gate_pass_simulated") else "fail"
            print(
                f"  {r['slug']}/{r['episode_id'][:8]} "
                f"cov={r['coverage']:.1%} "
                f"Δdur={r['duration_delta']:.0f}s "
                f"ads={r.get('ads_present_in_pc_drift_corrected', 0)}/"
                f"{r.get('ad_count', 0)} "
                f"drift={r.get('estimated_drift', 0):+.0f}s "
                f"gate={gate}"
            )
        if len(ok_eps) > 20:
            print(f"  ... and {len(ok_eps) - 20} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
