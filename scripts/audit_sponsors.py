#!/usr/bin/env python3
"""Sponsor audit script.

Reads episode_details.ad_markers_json from MinusPod DB and MinusPod log rejections
for informational reporting. With --apply, syncs only the curated entries in
data/house_sponsors.json to known_sponsors via /api/v1/sponsors.
"""
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
MINUSPOD_DB = ROOT / "MinusPod" / "data" / "podcast.db"
MINUSPOD_LOG_FILE = Path("/tmp/minuspod.log")
HOUSE_SPONSORS_FILE = ROOT / "data" / "house_sponsors.json"
MINUSPOD_API_BASE = os.environ.get("MINUSPOD_API_BASE", "http://localhost:8000/api/v1")

# Brand-specific patterns only — no generic words like "premium" or "membership".
HOUSE_BRAND_PATTERNS = [
    (r'giant bomb\s+premium', 'Giant Bomb Premium'),
    (r'giant\s*bomb', 'Giant Bomb'),
    (r'video game town', 'Video Game Town'),
    (r'dlc\s+podcast', 'DLC Podcast'),
    (r'blight club', 'Blight Club'),
    (r'radiotopia', 'Radiotopia'),
    (r'patreon', 'Patreon'),
]


def load_house_sponsors() -> List[Dict]:
    """Load curated house sponsors from JSON file."""
    if not HOUSE_SPONSORS_FILE.exists():
        logger.warning("File not found: %s", HOUSE_SPONSORS_FILE)
        return []
    with open(HOUSE_SPONSORS_FILE) as f:
        data = json.load(f)
    return data.get("house_sponsors", [])


def load_ad_markers_from_db() -> List[Tuple[str, float, str]]:
    """Read ad_markers_json from episode_details joined to episodes/podcasts."""
    if not MINUSPOD_DB.exists():
        logger.warning("MinusPod DB not found: %s", MINUSPOD_DB)
        return []

    candidates: List[Tuple[str, float, str]] = []
    conn = sqlite3.connect(MINUSPOD_DB)
    try:
        rows = conn.execute(
            """
            SELECT p.slug, e.episode_id, ed.ad_markers_json
            FROM episode_details ed
            JOIN episodes e ON e.id = ed.episode_id
            JOIN podcasts p ON p.id = e.podcast_id
            WHERE ed.ad_markers_json IS NOT NULL
              AND ed.ad_markers_json != '[]'
            """
        ).fetchall()
    finally:
        conn.close()

    for slug, episode_id, markers_json in rows:
        try:
            markers = json.loads(markers_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(markers, list):
            continue
        episode_key = f"{slug}:{episode_id}"
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            reason = marker.get("reason") or marker.get("sponsor") or ""
            start = float(marker.get("start", 0))
            end = float(marker.get("end", 0))
            duration = end - start
            if reason and duration > 0:
                candidates.append((episode_key, duration, reason))
    return candidates


def extract_from_minuspod_log() -> List[Tuple[str, float, str]]:
    """Extract rejected candidates from MinusPod log."""
    candidates = []
    if not MINUSPOD_LOG_FILE.exists():
        logger.warning("Log file not found: %s", MINUSPOD_LOG_FILE)
        return candidates

    pattern1 = re.compile(
        r'Rejecting suspected content:\s+([\d.]+)s-([\d.]+)s\s+\((\d+)s\)\s+-\s+'
        r'no sponsor identified in reason:\s*(.+)'
    )
    pattern2 = re.compile(
        r'Rejecting low-confidence non-sponsor:\s+([\d.]+)s-([\d.]+)s\s+\((\d+)s.*?\)\s+-\s+'
        r'reason:\s*(.+)'
    )

    with open(MINUSPOD_LOG_FILE) as f:
        for line in f:
            for pattern in (pattern1, pattern2):
                match = pattern.search(line)
                if match:
                    duration = float(match.group(3))
                    reason = match.group(4).strip()
                    ep_match = re.search(r'\[([^:]+):([^\]]+)\]', line)
                    episode_id = (
                        f"{ep_match.group(1)}:{ep_match.group(2)}"
                        if ep_match else "unknown"
                    )
                    candidates.append((episode_id, duration, reason))
    return candidates


def extract_brand_from_reason(reason: str) -> Optional[str]:
    """Extract a brand name from reason text (reporting only)."""
    if not reason:
        return None
    reason_lower = reason.lower()
    for pattern, canonical in HOUSE_BRAND_PATTERNS:
        if re.search(pattern, reason_lower):
            return canonical
    return None


def get_known_sponsors() -> Set[str]:
    """Fetch known sponsors from MinusPod API."""
    try:
        resp = requests.get(f"{MINUSPOD_API_BASE}/sponsors", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {s['name'].lower() for s in data.get('sponsors', [])}
    except Exception as e:
        logger.error("Failed to fetch known sponsors: %s", e)
        return set()


def add_sponsor(name: str, aliases: List[str], category: str = "house_promo") -> bool:
    """Add a sponsor via the MinusPod API."""
    try:
        resp = requests.post(
            f"{MINUSPOD_API_BASE}/sponsors",
            json={"name": name, "aliases": aliases, "category": category},
            timeout=10,
        )
        if resp.status_code == 201:
            logger.info("Added sponsor: %s", name)
            return True
        if resp.status_code == 409:
            logger.info("Sponsor already exists: %s", name)
            return True
        logger.error("Failed to add sponsor %s: %s %s", name, resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("Error adding sponsor %s: %s", name, e)
        return False


def sync_curated_sponsors(house_sponsors: List[Dict], known_sponsors: Set[str], apply: bool) -> int:
    """Sync curated house_sponsors.json entries to known_sponsors."""
    missing = [hs for hs in house_sponsors if hs['name'].lower() not in known_sponsors]

    if missing:
        logger.info("\n--- Missing curated sponsors ---")
        for hs in missing:
            logger.info("  %s (%s)", hs['name'], hs.get('category', 'house_promo'))
        if apply:
            logger.info("\n--- Applying curated sponsors ---")
            for hs in missing:
                add_sponsor(
                    hs['name'],
                    hs.get('aliases', []),
                    hs.get('category', 'house_promo'),
                )
    else:
        logger.info("All curated sponsors present in known_sponsors.")

    return len(missing)


def audit(apply: bool = False) -> int:
    """Run the sponsor audit."""
    house_sponsors = load_house_sponsors()
    known_sponsors = get_known_sponsors()

    ad_marker_candidates = load_ad_markers_from_db()
    log_candidates = extract_from_minuspod_log()
    all_candidates = ad_marker_candidates + log_candidates

    # Informational: brands spotted in ad reasons / rejections (not written on --apply).
    found_brands: Dict[str, List[Tuple[str, float, str]]] = {}
    for episode_id, duration, reason in all_candidates:
        brand = extract_brand_from_reason(reason)
        if brand:
            found_brands.setdefault(brand.lower(), []).append((episode_id, duration, reason))

    missing_curated = sync_curated_sponsors(house_sponsors, known_sponsors, apply)

    logger.info("\n=== Sponsor Audit Results ===")
    logger.info("Candidates analyzed (informational): %d", len(all_candidates))
    logger.info("Brands spotted in reasons (informational): %d", len(found_brands))
    logger.info("Known sponsors in DB: %d", len(known_sponsors))
    logger.info("Missing curated sponsors: %d", missing_curated)

    if found_brands:
        logger.info("\n--- Brands spotted (report only, not auto-added) ---")
        for brand_lower, occurrences in sorted(found_brands.items()):
            total_duration = sum(d for _, d, _ in occurrences)
            logger.info(
                "  %s: %d occurrences, %.0fs total",
                brand_lower, len(occurrences), total_duration,
            )

    return missing_curated


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Audit and sync sponsor list")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Sync curated house_sponsors.json entries to MinusPod (no heuristic auto-add)",
    )
    args = parser.parse_args()

    missing_count = audit(apply=args.apply)
    sys.exit(0 if missing_count == 0 else 1)


if __name__ == "__main__":
    main()
