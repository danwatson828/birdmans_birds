#!/usr/bin/env python3
"""Build/refresh the persistent ESPN -> BallDontLie player ID map. READ-ONLY.

Design spec section 4: joining the two sources by normalised name silently failed
on real draftable players (Nic Claxton / "Nicolas", Bones Hyland / "Nah'Shon", an
A.J./AJ punctuation mismatch). The fix is a committed map with manual overrides,
not fuzzy matching on every run.

The map is a CSV in the project root -- deliberately NOT inside the snapshot,
which ingestion owns and republishes. It is meant to be edited by hand: fill in a
bdl_player_id for an UNMATCHED row and set match_method to `manual`, and this
script will preserve it on every later refresh.

    python build_id_map.py              # refresh, preserving manual entries
    python build_id_map.py --review     # print only what needs human attention
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"
ESPN_PLAYERS = PROJECT_ROOT / "espn_cache" / "espn_players.json"
ID_MAP = PROJECT_ROOT / "espn_bdl_id_map.csv"
RECENT_SEASONS = (2023, 2024, 2025)

# Nickname/legal-name pairs found by auditing every unmatched ESPN player against
# the snapshot by surname. Evidence-driven, not guessed.
NAME_ALIASES = {
    "nic claxton": "nicolas claxton",
    "alex sarr": "alexandre sarr",
    "bub carrington": "carlton carrington",
    "bones hyland": "nahshon hyland",
    "kj martin": "kenyon martin",
    "nigel hayes davis": "nigel hayes",
    # Airious "Ace" Bailey, 2025 draft -- ESPN uses the nickname exclusively.
    # Found by auditing the unmatched list against the FULL players table rather
    # than only recent played rows, which is what missed him the first time.
    "ace bailey": "airious bailey",
}


def norm_name(s: str) -> str:
    """Punctuation is DELETED, not replaced with a space: replacing it split
    "A.J. Green" into "a j green" while ESPN's "AJ Green" became "aj green", so
    the two never matched -- a silent miss rather than an error."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    for junk in (".", "'", "’", "`", "-", ",", "*"):
        s = s.replace(junk, "")
    parts = [p for p in s.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    key = " ".join(parts)
    return NAME_ALIASES.get(key, key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", action="store_true")
    args = ap.parse_args()
    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")

    espn = pd.DataFrame(json.loads(ESPN_PLAYERS.read_text()))
    espn["key"] = espn["name"].apply(norm_name)

    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        bdl = con.execute(f"""
            SELECT player_id AS bdl_player_id,
                   any_value(player_name) AS bdl_name,
                   count(*) AS played_rows
              FROM v_player_games
             WHERE game_type = 'regular' AND NOT is_dnp
               AND season IN {RECENT_SEASONS}
             GROUP BY player_id""").df()
    finally:
        con.close()
    bdl["key"] = bdl["bdl_name"].apply(norm_name)

    # A key mapping to several BallDontLie ids is a genuine ambiguity, not a
    # match -- flag it rather than silently taking the first.
    dupes = bdl[bdl.duplicated("key", keep=False)].sort_values("key")
    uniq = bdl.drop_duplicates("key", keep=False)

    m = espn.merge(uniq[["key", "bdl_player_id", "bdl_name", "played_rows"]],
                   on="key", how="left")

    # Second pass: reversed name order. ESPN renders some Chinese names
    # given-name-last ("Yang Hansen") where BallDontLie uses "Hansen Yang", and no
    # surname heuristic catches that -- the surnames simply do not line up. Trying
    # the reversal fixes the whole class rather than one player.
    rev = uniq.copy()
    rev["key"] = rev["bdl_name"].apply(lambda n: norm_name(" ".join(
        reversed(norm_name(n).split()))))
    rev = rev.drop_duplicates("key", keep=False)
    miss = m["bdl_player_id"].isna()
    if miss.any():
        fill = m.loc[miss, ["key"]].merge(
            rev[["key", "bdl_player_id", "bdl_name", "played_rows"]],
            on="key", how="left")
        for col in ("bdl_player_id", "bdl_name", "played_rows"):
            m.loc[miss, col] = fill[col].values
        m["_reversed"] = False
        m.loc[miss & m["bdl_player_id"].notna(), "_reversed"] = True
    else:
        m["_reversed"] = False
    m["match_method"] = None
    matched = m["bdl_player_id"].notna() & ~m["_reversed"].fillna(False)
    m.loc[matched, "match_method"] = m.loc[matched, "name"].apply(
        lambda n: "alias" if norm_name(n) != " ".join(
            p for p in "".join(
                ch for ch in unicodedata.normalize("NFKD", n.lower())
                if not unicodedata.combining(ch)
            ).replace(".", "").replace("'", "").replace("-", "").split()
            if p not in ("jr", "sr", "ii", "iii", "iv", "v"))
        else "normalized_name")
    m.loc[m["_reversed"].fillna(False), "match_method"] = "reversed_name_order"
    m.loc[m["key"].isin(set(dupes["key"])), "match_method"] = "AMBIGUOUS"
    still = m["bdl_player_id"].isna()
    m.loc[still & ~m["key"].isin(set(dupes["key"])), "match_method"] = "UNMATCHED"

    out = m[["espn_id", "name", "bdl_player_id", "bdl_name", "match_method",
             "draft_rank", "pro_team"]].rename(columns={"name": "espn_name"})
    out = out.sort_values(["match_method", "draft_rank"])

    # Preserve hand-entered rows across refreshes -- that is the whole point of
    # the file being committed and editable.
    kept = 0
    if ID_MAP.exists():
        prev = pd.read_csv(ID_MAP)
        manual = prev[prev["match_method"].astype(str).str.lower() == "manual"]
        for _, r in manual.iterrows():
            hit = out["espn_id"] == r["espn_id"]
            if hit.any():
                out.loc[hit, ["bdl_player_id", "bdl_name", "match_method"]] = [
                    r["bdl_player_id"], r["bdl_name"], "manual"]
                kept += 1
    out.to_csv(ID_MAP, index=False)

    counts = out["match_method"].value_counts()
    print(f"wrote {ID_MAP.name}: {len(out)} rows")
    for k, v in counts.items():
        print(f"  {k:<18} {v}")
    if kept:
        print(f"  (preserved {kept} hand-entered manual row(s))")

    need = out[out["match_method"].isin(["UNMATCHED", "AMBIGUOUS"])]
    if len(need):
        print(f"\nNEEDS MANUAL REVIEW ({len(need)}). To resolve: put the BallDontLie")
        print("player_id in bdl_player_id and set match_method to `manual`.")
        print(f"  {'method':<11}{'ESPN#':>6}  {'ESPN name':<26}{'team':<5}")
        for _, r in need.sort_values("draft_rank").iterrows():
            rank = "" if pd.isna(r["draft_rank"]) else int(r["draft_rank"])
            print(f"  {r['match_method']:<11}{str(rank):>6}  {r['espn_name']:<26}{r['pro_team']:<5}")
    if len(dupes):
        print(f"\n  {len(dupes)} BallDontLie rows share a normalised name "
              f"(ambiguous, never auto-matched):")
        for _, r in dupes.iterrows():
            print(f"    id={r['bdl_player_id']:<8} {r['bdl_name']:<26} "
                  f"key={r['key']!r}  {r['played_rows']} played rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
