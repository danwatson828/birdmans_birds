#!/usr/bin/env python3
"""Team brand colours and logos for the 30 active franchises.

WHY THIS IS A FILE AND NOT A QUERY. The snapshot has no colour, logo, arena or
founding-year column -- `teams` carries id, conference, division, city, name,
full_name, abbreviation and nothing else. BallDontLie does not serve branding.
So the colours below are a curated constant, not ingested data, and anything
built on them has to say so rather than implying the dataset knows them.

Colours are each franchise's two primary brand colours, chosen so that
`primary` is dark enough to carry white text at normal weights -- a few teams'
true primary is a mid-tone (Lakers gold, Pacers yellow, Jazz yellow) and those
are kept as `accent` with the darker colour leading, otherwise a themed header
becomes unreadable.

Logos come from ESPN's CDN, which this project already talks to for the fantasy
league sync. `fetch_logos()` downloads them once into web/logos/ so the app does
not depend on the network at page-view time. ESPN's slugs are not the same
strings as BallDontLie's abbreviations (GSW -> gs, NYK -> ny, UTA -> utah), so
the mapping is explicit rather than lowercased.

    python team_brand.py --fetch-logos
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOGO_DIR = Path(__file__).parent / "web" / "logos"
ESPN_LOGO = "https://a.espncdn.com/i/teamlogos/nba/500/{slug}.png"

# team_id -> (abbrev, primary, accent, ink_on_primary, espn_slug)
# `ink_on_primary` is the text colour that meets contrast on `primary`; it is
# stated per team rather than computed, because the few mid-tone primaries are
# exactly the cases a luminance threshold gets wrong at the boundary.
BRAND: dict[int, tuple[str, str, str, str, str]] = {
    1:  ("ATL", "#c8102e", "#fdb927", "#ffffff", "atl"),
    2:  ("BOS", "#007a33", "#ba9653", "#ffffff", "bos"),
    3:  ("BKN", "#1d1d1b", "#777777", "#ffffff", "bkn"),
    4:  ("CHA", "#1d1160", "#00788c", "#ffffff", "cha"),
    5:  ("CHI", "#ce1141", "#111111", "#ffffff", "chi"),
    6:  ("CLE", "#860038", "#fdbb30", "#ffffff", "cle"),
    7:  ("DAL", "#00538c", "#b8c4ca", "#ffffff", "dal"),
    8:  ("DEN", "#0e2240", "#fec524", "#ffffff", "den"),
    9:  ("DET", "#c8102e", "#1d42ba", "#ffffff", "det"),
    10: ("GSW", "#1d428a", "#ffc72c", "#ffffff", "gs"),
    11: ("HOU", "#ce1141", "#2f3c47", "#ffffff", "hou"),
    12: ("IND", "#002d62", "#fdbb30", "#ffffff", "ind"),
    13: ("LAC", "#c8102e", "#1d428a", "#ffffff", "lac"),
    14: ("LAL", "#552583", "#fdb927", "#ffffff", "lal"),
    15: ("MEM", "#5d76a9", "#12173f", "#ffffff", "mem"),
    16: ("MIA", "#98002e", "#f9a01b", "#ffffff", "mia"),
    17: ("MIL", "#00471b", "#eee1c6", "#ffffff", "mil"),
    18: ("MIN", "#0c2340", "#78be20", "#ffffff", "min"),
    19: ("NOP", "#0c2340", "#c8102e", "#ffffff", "no"),
    20: ("NYK", "#006bb6", "#f58426", "#ffffff", "ny"),
    21: ("OKC", "#007ac1", "#ef3b24", "#ffffff", "okc"),
    22: ("ORL", "#0077c0", "#c4ced4", "#ffffff", "orl"),
    23: ("PHI", "#006bb6", "#ed174c", "#ffffff", "phi"),
    24: ("PHX", "#1d1160", "#e56020", "#ffffff", "phx"),
    25: ("POR", "#e03a3e", "#1d1d1b", "#ffffff", "por"),
    26: ("SAC", "#5a2d81", "#63727a", "#ffffff", "sac"),
    27: ("SAS", "#36393b", "#c4ced4", "#ffffff", "sa"),
    28: ("TOR", "#ce1141", "#000000", "#ffffff", "tor"),
    29: ("UTA", "#002b5c", "#f9a01b", "#ffffff", "utah"),
    30: ("WAS", "#002b5c", "#e31837", "#ffffff", "wsh"),
}

# Defunct and relocated franchises keep the neutral treatment: there is no
# modern brand to borrow, and inventing one would dress up a 1949 roster as
# something it is not.
NEUTRAL = ("#3a3632", "#8c8578", "#ffffff", None)


def brand_for(team_id: int | None) -> dict:
    """Always returns a usable theme, so callers never branch on missing teams."""
    row = BRAND.get(team_id or -1)
    if row is None:
        primary, accent, ink, slug = NEUTRAL
        return {"team_id": team_id, "abbrev": None, "primary": primary,
                "accent": accent, "ink": ink, "logo": None, "branded": False}
    abbrev, primary, accent, ink, slug = row
    return {"team_id": team_id, "abbrev": abbrev, "primary": primary,
            "accent": accent, "ink": ink,
            "logo": f"/logos/{slug}.png", "branded": True}


def all_brands() -> dict[int, dict]:
    return {tid: brand_for(tid) for tid in BRAND}


def have_logos() -> tuple[int, int]:
    """(present, expected) -- lets the API report honestly instead of serving 404s."""
    expected = {row[4] for row in BRAND.values()}
    present = {p.stem for p in LOGO_DIR.glob("*.png")} if LOGO_DIR.is_dir() else set()
    return len(expected & present), len(expected)


def fetch_logos(force: bool = False) -> int:
    """Download each franchise logo once from ESPN's CDN into web/logos/.

    Fetched locally rather than hotlinked so the app renders with no network,
    and so a CDN path change breaks the download step rather than every page.
    """
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for abbrev, _p, _a, _i, slug in BRAND.values():
        dest = LOGO_DIR / f"{slug}.png"
        if dest.exists() and not force:
            print(f"  have   {abbrev:4s} {dest.name}")
            continue
        url = ESPN_LOGO.format(slug=slug)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-analytics/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  FAILED {abbrev:4s} {url} -- {exc}", file=sys.stderr)
            continue
        if not body.startswith(b"\x89PNG"):
            print(f"  FAILED {abbrev:4s} not a PNG ({len(body)} bytes)", file=sys.stderr)
            continue
        dest.write_bytes(body)
        total += len(body)
        print(f"  got    {abbrev:4s} {dest.name}  {len(body):,} bytes")
        time.sleep(0.15)      # courtesy pacing; 30 files is not a load test
    have, want = have_logos()
    print(f"\n{have}/{want} logos present, {total:,} bytes downloaded this run")
    return 0 if have == want else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-logos", action="store_true",
                    help="download the 30 franchise logos into web/logos/")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()
    if args.fetch_logos:
        return fetch_logos(force=args.force)
    have, want = have_logos()
    print(f"{len(BRAND)} branded franchises; {have}/{want} logos present in {LOGO_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
