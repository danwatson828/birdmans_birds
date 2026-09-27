"""Print raw BallDontLie responses for the four endpoints this schema is built on.

Run this after any API tier change and diff the field names against schema.sql
before trusting a backfill. /stats and /season_averages return 401 on the free
tier, which is why player_stats in schema.sql is marked unverified.
"""
import json, os, sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
KEY = os.environ["BALLDONTLIE_API_KEY"]
BASE = "https://api.balldontlie.io/v1"
H = {"Authorization": KEY}

def probe(path, params):
    url = f"{BASE}/{path}"
    print("=" * 78)
    print(f"GET {url}  params={params}")
    print("=" * 78)
    r = requests.get(url, headers=H, params=params, timeout=30)
    print(f"HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text[:2000])
    print()

# NOTE: /teams ignores per_page and returns all ~89 teams it knows about,
# including defunct and international clubs. Response has no meta block.
probe("teams",   {"per_page": 2})
probe("players", {"per_page": 2, "search": "jokic"})
probe("games",   {"per_page": 2, "seasons[]": 2024})
# Requires ALL-STAR or GOAT (401 on free tier). Verified 200 on GOAT.
probe("stats",   {"per_page": 2, "seasons[]": 2024})
# /season_averages takes a SINGLE player_id, not the player_ids[] array
# form the other endpoints use; the array form returns HTTP 400.
probe("season_averages", {"season": 2024, "player_id": 246})
