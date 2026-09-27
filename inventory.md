# Data Inventory

Generated 2026-09-27 17:09 EDT from `nba_snapshot.duckdb` by `inventory.py`. Every figure below is measured, not remembered -- regenerate after any ingest.

## At a glance

- **1,685,039** box score lines across **80 seasons** (1946-2025, games dated 1946-11-01 to 2026-06-13)
- **945,542** advanced-stat lines, **30 seasons** (1996-2025) -- 30/80 of the seasons the box scores cover
- **73,174** games, **5,147** players, **89** teams (30 current NBA franchises; the rest are defunct or international)
- **209,144** of 1,685,039 box score rows (12.4%) are did-not-play rows -- excluded by `games_played`, included in `games_on_roster`
- game types: regular 68,524, playoff 4,617, play_in 30, cup_final 3

**Read the snapshot, not the live file.** `nba.duckdb` belongs to ingestion, which holds an exclusive lock while it runs. `nba_snapshot.duckdb` is published only after ingestion completes cleanly *and* `tests/` passes, so it is the only copy that has been verified.

## Raw tables

Column names mirror the API field-for-field; nothing is cleaned here.

### `teams` -- 89 rows
One row per team the API knows about -- not just the NBA. Includes defunct BAA clubs and international sides.

`id`, `conference`, `division`, `city`, `name`, `full_name`, `abbreviation`, `_ingested_at`

### `players` -- 5,147 rows
One row per player. `team_id` is a mutable snapshot of where they are now and is unsafe for historical questions.

`id`, `first_name`, `last_name`, `position`, `height`, `weight`, `jersey_number`, `college`, `country`, `draft_year`, `draft_round`, `draft_number`, `team_id`, `_ingested_at`

### `games` -- 73,174 rows
One row per game, with quarter/OT scores and both teams. `postseason` is a two-way flag over four kinds of game -- use `v_game_type`.

`id`, `date`, `season`, `status`, `status_state`, `period`, `time`, `postseason`, `postponed`, `home_team_id`, `visitor_team_id`, `home_team_score`, `visitor_team_score`, `datetime`, `home_q1`, `home_q2`, `home_q3`, `home_q4`, `home_ot1`, `home_ot2`, `home_ot3`, `home_timeouts_remaining`, `home_in_bonus`, `visitor_q1`, `visitor_q2`, `visitor_q3`, `visitor_q4`, `visitor_ot1`, `visitor_ot2`, `visitor_ot3`, `visitor_timeouts_remaining`, `visitor_in_bonus`, `ist_stage`, `_ingested_at`

### `player_stats` -- 1,685,039 rows
The box score. One row per player per game. Era-limited: see the coverage section.

`id`, `player_id`, `game_id`, `team_id`, `min`, `fgm`, `fga`, `fg_pct`, `fg3m`, `fg3a`, `fg3_pct`, `ftm`, `fta`, `ft_pct`, `oreb`, `dreb`, `reb`, `ast`, `stl`, `blk`, `turnover`, `pf`, `pts`, `plus_minus`, `_ingested_at`

### `player_advanced_stats` -- 945,542 rows
Advanced metrics (ratings, usage, pace, PIE) per player per game. 1996+ only.

`id`, `player_id`, `game_id`, `team_id`, `pie`, `pace`, `assist_percentage`, `assist_ratio`, `assist_to_turnover`, `defensive_rating`, `defensive_rebound_percentage`, `effective_field_goal_percentage`, `net_rating`, `offensive_rating`, `offensive_rebound_percentage`, `rebound_percentage`, `true_shooting_percentage`, `turnover_ratio`, `usage_percentage`, `_ingested_at`

### `_ingest_log` -- 256 rows
Ingestion bookkeeping and resume checkpoints. One row per unit attempt; the latest row per unit is the live state.

`id`, `endpoint`, `params`, `season`, `rows_upserted`, `http_status`, `started_at`, `finished_at`, `error`

## Semantic views

Canonical definitions. Prefer these over the raw tables: a metric re-derived ad hoc can disagree with the same metric asked another way, and nothing then says which is right.

| view | what it holds |
|---|---|
| `v_cup_final_games` | The NBA Cup final, derived structurally (one per season, 2023+). |
| `v_game_classification_audit` | Self-check: regular games must equal scheduled x teams / 2. |
| `v_game_type` | **The season-type authority.** regular / play_in / cup_final / playoff. |
| `v_games` | Game context with team abbreviations, margin, and `game_type`. |
| `v_play_in_games` | Play-in games, derived from per-team game counts. |
| `v_player_game_per36` | Per-36-minute rates, per game. |
| `v_player_game_shooting` | Per-game TS%, eFG%, and true shooting attempts. |
| `v_player_games` | **The spine.** Box score + game context + parsed minutes + `is_dnp`. |
| `v_player_rolling_10` | Trailing 10-game form. Same exclusive/inclusive split. |
| `v_player_rolling_5` | Trailing 5-game form. `*_last_n` excludes the current game. |
| `v_player_season_averages` | Season per-game and rate lines, computed from totals. |
| `v_player_season_totals` | Season totals per player per team per game_type. |
| `v_players` | Players with height/weight/jersey parsed to numbers. |
| `v_season_schedule` | Per season: scheduled games per team and whether the schedule was uniform. |
| `v_stat_first_tracked` | **The era authority.** When each stat first appears and first becomes reliable. |
| `v_team_game_slots` | One row per team per non-postseason game. Plumbing for the classification. |
| `v_teams` | Teams with whitespace junk nulled and `is_nba_franchise` flagged. |

| macro | what it computes |
|---|---|
| `efg_pct` | Effective FG%. Threes counted as 1.5 twos. |
| `parse_minutes` | The one place the `min` string becomes a number. |
| `per36` | Per-36-minute rate. NULL below a real minutes base. |
| `player_rolling_avg` | Table macro: rolling form for an arbitrary window. |
| `ts_pct` | True shooting %. PTS / (2 x (FGA + 0.44 x FTA)). |

## Coverage and trust

A NULL in this data means **not recorded**, which is not zero. `SUM` and `AVG` skip NULLs silently, so a query spanning an era boundary ranks players partly by whether the league was recording the stat while they played. `v_stat_first_tracked` is the authority; the table below is that view plus measured per-season coverage.

Coverage is measured over rows where the player **actually played** (`NOT is_dnp`). Measuring over all rows understates everything, because DNP rows are all-NULL by nature.

| stat | first appears | reliable from | 1950 | 1960 | 1970 | 1975 | 1980 | 1983 | 1990 | 2000 | 2020 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `pts` | 1946 | 1946 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `fgm` | 1946 | 1946 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `ftm` | 1946 | 1946 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `fta` | 1946 | 1948 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `fga` | 1946 | 1974 | 17% | 64% | 97% | 98% | 99% | 100% | 100% | 100% | 100% |
| `ast` | 1946 | 1974 | 15% | 39% | 95% | 99% | 98% | 100% | 100% | 100% | 100% |
| `pf` | 1946 | 1975 | 41% | 99% | 93% | 97% | 99% | 100% | 100% | 100% | 100% |
| `reb` | 1950 | 1974 | 6% | 71% | 97% | 99% | 99% | 100% | 100% | 100% | 100% |
| `oreb` | 1962 | 1983 | 0% | 0% | 0% | 53% | 53% | 100% | 100% | 100% | 100% |
| `dreb` | 1962 | 1983 | 0% | 0% | 0% | 53% | 53% | 100% | 100% | 100% | 100% |
| `turnover` | 1963 | 1983 | 0% | 0% | 0% | 0% | 67% | 100% | 100% | 100% | 100% |
| `stl` | 1970 | 1983 | 0% | 0% | 0% | 62% | 68% | 100% | 100% | 100% | 100% |
| `blk` | 1973 | 1983 | 0% | 0% | 0% | 65% | 84% | 100% | 100% | 100% | 100% |
| `fg3m` | 1979 | 1979 | 0% | 0% | 0% | 0% | 100% | 100% | 100% | 100% | 100% |
| `fg3a` | 1979 | 1983 | 0% | 0% | 0% | 0% | 88% | 100% | 100% | 100% | 100% |
| `plus_minus` | 2019 | 2024 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 99% |

`reliable from` is the first season at or after which coverage stays at 95% or better. The gap between the two columns is often a decade: several stats debut with a *single recorded row* in the whole season, so `first appears` is close to useless as a filter and `reliable from` is the one to use for comparisons.

### Coverage that regressed

Seasons where coverage fell below 95% **after** the stat had already reached 95% -- gaps in the source rather than the era ramp. Exclude these by name rather than loosening a threshold, so a new gap elsewhere still shows up:

- `fga`: **1971 (93%)**, **1972 (94%)**, **1973 (92%)**
- `pf`: **1964 (84%)**, **1965 (93%)**, **1966 (80%)**, **1970 (93%)**, **1971 (90%)**, **1972 (91%)**, **1973 (89%)**, **1974 (95%)**
- `reb`: **1971 (93%)**, **1972 (95%)**, **1973 (93%)**
- `plus_minus`: **2023 (46%)**

### The specific fields to be careful with

**Steals (`stl`)** -- appears 1970, reliable from 1983.
  No data at all for the 24 seasons 1946-1969: any player whose career fell entirely in that window is structurally absent from a `stl` leaderboard, so an unqualified all-time ranking is wrong even when the names look plausible.
  Still zero in 2 later season(s) (1971-1972) despite the first appearance in 1970 -- the debut is a single stray row, not the start of real recording.

**Blocks (`blk`)** -- appears 1973, reliable from 1983.
  No data at all for the 27 seasons 1946-1972: any player whose career fell entirely in that window is structurally absent from a `blk` leaderboard, so an unqualified all-time ranking is wrong even when the names look plausible.

**Turnovers (`turnover`)** -- appears 1963, reliable from 1983.
  No data at all for the 17 seasons 1946-1962: any player whose career fell entirely in that window is structurally absent from a `turnover` leaderboard, so an unqualified all-time ranking is wrong even when the names look plausible.
  Still zero in 11 later season(s) (1964-1976) despite the first appearance in 1963 -- the debut is a single stray row, not the start of real recording.

**Plus/minus (`plus_minus`)** -- appears 2019, reliable from 2024.
  No data at all for the 73 seasons 1946-2018: any player whose career fell entirely in that window is structurally absent from a `plus_minus` leaderboard, so an unqualified all-time ranking is wrong even when the names look plausible.
  Regressed after reaching full coverage: 2023 (46%).

**Rebounds (`reb`)** -- appears 1950, reliable from 1974.
  No data at all for the 4 seasons 1946-1949: any player whose career fell entirely in that window is structurally absent from a `reb` leaderboard, so an unqualified all-time ranking is wrong even when the names look plausible.
  Regressed after reaching full coverage: 1971 (93%), 1972 (95%), 1973 (93%).

### Minutes

`player_stats.min` is a string in two different formats depending on era: integer-string 1,261,905, MM:SS 309,671, NULL 113,463. `v_player_games.minutes_played` is the parsed number -- never parse it again yourself.

Minutes are essentially absent early on: 1946 0%, 1947 0%, 1948 0%, 1949 0%, 1950 0%, 1951 9.8%, 1952 8.3%, 1953 25%, 1954 23.6%, 1955 31.4%. That is why `is_dnp` cannot be defined as "no minutes" -- doing so read the entire pre-1951 era as did-not-play and turned Jerry West's 27.0 career average into 48.7.

### Season-type classification

Of 80 seasons, 73 have a uniform schedule (every team played at least a full slate) and **all 73 of those land exactly on `scheduled x teams / 2` regular-season games** -- the arithmetic check in `v_game_classification_audit`.

The 7 non-uniform seasons are [1949, 1950, 1952, 1953, 1955, 2012, 2019]. In those the count-based derivation is meaningless (early-league uneven schedules, the 2011-12 lockout, the 2019-20 bubble), so the classification declines to guess and calls everything `regular` rather than inventing play-in or cup games.

### Advanced stats coverage

1996-2025, 30 seasons, gaps: none.

Within that range **784,956 of 785,149 box score lines for players who actually played (99.98%)** have a matching advanced row -- effectively complete.

Measured against *all* rows including did-not-play it is 945,472/989,326 (95.6%), but that is the wrong denominator: advanced metrics are rates, and a player with zero minutes has none to compute, so DNP rows are only partly represented upstream. The recent-season DNP count roughly tripled (about 5,000 a season before 2022, about 18,000 after), which makes the all-rows figure look like it degrades over time when coverage of actual appearances does not.

**1996 is a hard floor.** The 50 seasons from 1946 to 1995 have box scores but no advanced stats at all, so any join must be a LEFT JOIN unless the query is deliberately scoped to 1996+.

Percentage fields are **fractions** (`usage_percentage` 0.108 = 10.8%). Ratings are per-game and therefore volatile in small samples -- a single-game `defensive_rating` of 154 is noise, not an outlier to clean. Join on `(game_id, player_id)`: the advanced row's `id` is its own sequence, unrelated to `player_stats.id`.

## Traps worth restating

1. **Season type** -- use `v_game_type.game_type`, never the raw `postseason` flag. `postseason = false` also contains play-in games and the NBA Cup final, which official statistics exclude. Using the raw flag reported Shai Gilgeous-Alexander at 32.5 ppg over 77 games against an official 32.7 over 76.
2. **Team context** -- `players.team_id` is where a player is *now*. For history use the per-game `team_id`. Luka Doncic's `players.team_id` is the Lakers, so his Dallas games are invisible through it.
3. **Era limits** -- check `v_stat_first_tracked` before any career or all-time question touching steals, blocks, turnovers, offensive/defensive rebounds, threes, or plus/minus.
4. **Plus/minus** -- pair any total with `games_with_plus_minus`. +140 over 12 recorded games and +140 over 82 are otherwise indistinguishable.
5. **Shooting efficiency before 1974** -- `fga` was barely recorded, so TS% and eFG% are unreliable however confidently they compute.

