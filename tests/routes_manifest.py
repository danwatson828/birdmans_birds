"""Every addressable page type in the web app, in one place.

Both the contact-sheet capture and the 420px overflow check read this, so
"every route" means the same thing to both, and a page type added here is
covered by both without anyone remembering to update two lists.

The nav routes come from app.js (parsed, so a new nav item cannot be missed).
The DRILL entries are the sub-views that live under a nav route and are only
reachable by a `view=` parameter -- a team page, a box score, a player page.
Those carry fixed ids so before/after screenshots show the same subject.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fixed subjects, so a before/after pair is the same page and not two pages
# that merely share a layout.
TEAM_ID = 2             # Boston Celtics: curated brand, green, good contrast
PLAYER_ID = 132         # Luka Doncic: full modern season, all stats tracked
GAME_ID = 18446826      # a completed 2025-26 Celtics game, has a line score
ANALYSIS_ID = 57        # the validated three-point finding, has 2 charts
SEASON = 2025


def nav_routes() -> list[str]:
    """Route ids, parsed out of app.js so the nav is the source of truth."""
    src = (PROJECT_ROOT / "web" / "app.js").read_text()
    block = src[src.index("export const ROUTES = ["):]
    block = block[:block.index("\n];")]
    ids = re.findall(r'\{\s*id:\s*"([a-z]+)"', block)
    if len(ids) < 5:
        raise RuntimeError(f"could not parse ROUTES from app.js (got {ids})")
    return ids


# Sub-views that are page TYPES in their own right rather than a mode of the
# page above them. Each is (label, query string).
DRILL = [
    ("team",        f"?v=teams&view=team&id={TEAM_ID}&season={SEASON}"),
    ("player",      f"?v=players&view=player&id={PLAYER_ID}&season={SEASON}"),
    ("game",        f"?v=teams&view=game&id={GAME_ID}&team={TEAM_ID}&season={SEASON}"),
]

# Nav routes that need a parameter to show their real content rather than an
# empty state.
ROUTE_QUERY = {
    "ask": f"?v=ask&analysis={ANALYSIS_ID}",
}


def all_pages() -> list[tuple[str, str]]:
    """[(label, query string)] covering every distinct page type."""
    pages = [(r, ROUTE_QUERY.get(r, f"?v={r}")) for r in nav_routes()]
    return pages + DRILL
