#!/usr/bin/env python3
"""Bake the draft room into one self-contained HTML file. READ-ONLY on inputs.

web/draft.html normally runs inside the local app, which serves the engine
from /static and the boards from /api/fantasy/draft-board. This writes a copy
with both baked in, so the draft room also works with no server at all:
double-click the file, or open it on a phone. Without the app there is no
ESPN sync -- picks are entered by hand -- and that is the only difference.

    python build_draft_page.py                      # -> dist/draft-room.html
    python build_draft_page.py --out my.html
    python build_draft_page.py --fragment --out x   # no <html>/<head>/<body>,
                                                    # for hosts that add their own

Re-run it after rebuilding a board (draft_board.py), or the baked copy keeps
serving the old numbers.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
WEB = PROJECT_ROOT / "web"
BOARDS = {"projected": "draft_board-projected.csv", "blend": "draft_board.csv"}

# Only what the page and the engine read. The rest of the board (match method,
# raw games, pool flag) is provenance for draft_board.py, not draft-night data.
KEEP = ["name", "pro_team", "pos_str", "value_source", "w_gp", "availability",
        "total_value", "injury_status", "draft_rank", "espn_id", "board_rank",
        "pts", "reb", "ast", "stl", "blk", "fg3m", "turnover",
        "fg_pct", "ft_pct", "fga", "fta",
        "z_pts", "z_reb", "z_ast", "z_stl", "z_blk", "z_fg3m",
        "z_fgpct", "z_ftpct", "z_turnover"]
TEXT = {"name", "pro_team", "pos_str", "value_source", "injury_status"}

GOOGLE_FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                'family=Archivo:wdth,wght@75..100,400..800&'
                'family=IBM+Plex+Mono:wght@400;600&display=swap">')


def read_board(path: Path) -> list[dict]:
    out = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            row = {}
            for k in KEEP:
                v = r.get(k)
                if v in (None, ""):
                    continue
                if k in TEXT:
                    row[k] = v
                else:
                    try:
                        x = float(v)
                    except ValueError:
                        continue
                    row[k] = int(x) if x.is_integer() and k in ("espn_id", "draft_rank", "board_rank") else round(x, 4)
            out.append(row)
    return out


def built_on(path: Path) -> str:
    """When the board's numbers were produced. The commit date, not the file's
    mtime: a fresh checkout stamps every file with today."""
    try:
        stamp = subprocess.run(["git", "log", "-1", "--format=%cI", "--", path.name],
                               cwd=PROJECT_ROOT, capture_output=True, text=True,
                               check=True).stdout.strip()
        if stamp:
            return stamp
    except (OSError, subprocess.CalledProcessError):
        pass
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def build(fragment: bool) -> str:
    html = (WEB / "draft.html").read_text()
    engine = (WEB / "draft_engine.js").read_text()

    boards, meta = {}, {}
    for key, fname in BOARDS.items():
        p = PROJECT_ROOT / fname
        if not p.exists():
            if key == "projected":
                raise SystemExit(f"{fname} is missing -- run draft_board.py --window projected")
            continue
        boards[key] = read_board(p)
        meta[key] = {"generated": built_on(p), "source": fname}

    data = json.dumps({"boards": boards, "meta": meta}, separators=(",", ":"))
    # A player name can never close the script element early.
    data = data.replace("</", "<\\/")

    html = re.sub(r"<!-- FONTS -->.*?<!-- /FONTS -->", GOOGLE_FONTS, html, flags=re.S)
    html = html.replace("<!-- DATA -->", f"<script>window.DRAFT_DATA = {data};</script>")
    tag = '<script src="/static/draft_engine.js"></script>'
    if html.count(tag) != 1:
        raise SystemExit("draft.html no longer loads the engine from /static; update this script")
    html = html.replace(tag, "<script>\n" + engine.replace("</script", "<\\/script") + "\n</script>")

    if fragment:
        for pat in (r"<!doctype html>\s*", r"<html[^>]*>\s*", r"</html>\s*", r"<head>\s*",
                    r"</head>\s*", r"<body>\s*", r"</body>\s*",
                    r'<meta charset="utf-8">\s*', r'<meta name="viewport"[^>]*>\s*'):
            html = re.sub(pat, "", html, count=1, flags=re.I)
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "dist" / "draft-room.html")
    ap.add_argument("--fragment", action="store_true",
                    help="omit the document skeleton, for a host that supplies one")
    args = ap.parse_args()
    html = build(args.fragment)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"wrote {args.out} ({len(html) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
