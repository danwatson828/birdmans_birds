-- =============================================================================
-- schema.sql — raw landing tables for BallDontLie API data
-- =============================================================================
-- Design rules for this layer:
--   1. Column names match the BallDontLie JSON field names exactly, so that
--      ingestion is a dumb field-for-field copy with no mapping table to keep
--      in sync. Where the API nests an object (e.g. game.home_team), the
--      foreign key is flattened to <name>_id and the rest is dropped -- it is
--      already stored once in the parent table.
--   2. No cleaning, no derivation, no unit conversion. The API's own types are
--      preserved even when they are inconvenient (minutes arrive as the string
--      "39:24"; height as "6-11"; weight as "284"). Everything derived lives in
--      the semantic layer (semantic.sql) so there is exactly one definition of
--      each metric and the raw layer stays a faithful mirror of the source.
--   3. Primary keys are the API's own ids, which makes the daily ingestion job
--      idempotent: it can INSERT ... ON CONFLICT DO UPDATE and re-running a day
--      is harmless.
--
-- All four tables verified against live GOAT-tier API responses on 2026-09-26,
-- including a 1,800-row cross-era sample and a 3,000-row constraint check for
-- player_stats. Re-run probe_api.py after any API change and diff before
-- trusting a backfill.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- teams
-- -----------------------------------------------------------------------------
-- Verified shape: GET /v1/teams
--   {"id":1,"conference":"East","division":"Southeast","city":"Atlanta",
--    "name":"Hawks","full_name":"Atlanta Hawks","abbreviation":"ATL"}
--
-- Two things the live response makes clear:
--   * The endpoint ignores per_page and returns every team it knows about --
--     the 30 current franchises, defunct BAA/NBA clubs (Chicago Stags id 37,
--     Waterloo Hawks id 48), and international/exhibition sides (Real Madrid
--     id 3142, London Lions id 216597). Non-NBA rows carry conference '    '
--     (four spaces) and empty-string division/city. Stored verbatim; the
--     semantic layer normalises the whitespace and flags the real franchises.
--   * abbreviation is NOT unique: 'WAS' is both the Wizards (id 30) and the
--     defunct Washington Capitols (id 42); 'MTA' appears twice. Never key or
--     join on abbreviation -- use team_id.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS teams (
    id            INTEGER PRIMARY KEY,
    conference    VARCHAR,
    division      VARCHAR,
    city          VARCHAR,
    name          VARCHAR,
    full_name     VARCHAR,
    abbreviation  VARCHAR,
    _ingested_at  TIMESTAMP DEFAULT current_timestamp
);

-- -----------------------------------------------------------------------------
-- players
-- -----------------------------------------------------------------------------
-- Verified shape: GET /v1/players?search=jokic and GET /v1/players/246
--   {"id":246,"first_name":"Nikola","last_name":"Jokic","position":"C",
--    "height":"6-11","weight":"284","jersey_number":"15",
--    "college":"Mega Basket","country":"Serbia","draft_year":2014,
--    "draft_round":2,"draft_number":41,"team":{...}}
--
-- height/weight/jersey_number are STRINGS in the API ("6-11", "284", "15"),
-- not numbers, and position can be an empty string for older players. Kept as
-- VARCHAR here; parsed into inches/pounds in the semantic layer.
--
-- team_id is flattened from the nested team object and is the player's CURRENT
-- team as of the moment the row was fetched -- a mutable snapshot, not history.
-- Per design spec section 4, the team a player actually played for in a given
-- game comes from player_stats.team_id, which is per-game and therefore
-- survives mid-season trades. Do not use players.team_id for historical
-- questions: in a 3,000-row sample of 2024 stats, 1,690 rows (56%) had a
-- stats.team_id that disagreed with the player's current players.team_id.
--
-- Verified on the season's clearest trade case: Luka Doncic (player_id 132) has
-- 89 rows in season 2024 under ONE player_id, 49 with team_id 7 (DAL) through
-- 2025-01-31 and 40 with team_id 14 (LAL) from 2025-02-04. So BallDontLie's
-- player_id does hold across a trade, as design spec section 4 expects.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS players (
    id             INTEGER PRIMARY KEY,
    first_name     VARCHAR,
    last_name      VARCHAR,
    position       VARCHAR,
    height         VARCHAR,     -- "6-11"
    weight         VARCHAR,     -- "284"
    jersey_number  VARCHAR,     -- "15"
    college        VARCHAR,
    country        VARCHAR,
    draft_year     INTEGER,
    draft_round    INTEGER,
    draft_number   INTEGER,
    team_id        INTEGER,     -- current team snapshot; see note above
    _ingested_at   TIMESTAMP DEFAULT current_timestamp
);

-- -----------------------------------------------------------------------------
-- games
-- -----------------------------------------------------------------------------
-- Verified shape: GET /v1/games?seasons[]=2024 and ?seasons[]=1995
--   id, date, season, status, status_state, period, time, postseason,
--   postponed, home_team_score, visitor_team_score, datetime,
--   home_q1..home_q4, home_ot1..home_ot3, home_timeouts_remaining,
--   home_in_bonus, visitor_q1..visitor_q4, visitor_ot1..visitor_ot3,
--   visitor_timeouts_remaining, visitor_in_bonus, ist_stage,
--   home_team{...}, visitor_team{...}
--
-- Field notes from the live responses:
--   * season is the START year of the season (2024 == the 2024-25 season).
--   * date is a plain calendar date ("2024-10-22"); datetime is a UTC instant
--     ("2024-10-22T23:30:00.000Z") and is NULL for older games (1995 returned
--     datetime: null). Order by date for anything historical, and treat
--     datetime as best-effort.
--   * status is a display string ("Final"); status_state is the machine-
--     readable one ("final"). time is " " or "Final" -- cosmetic, not a clock.
--   * timeouts_remaining / in_bonus are NULL for older games; ot1..ot3 are
--     NULL when the game did not go to that overtime. Only three OT periods
--     are exposed, so a 4OT game's later periods are not in the quarter
--     columns even though the final score includes them -- use
--     home_team_score / visitor_team_score as the authoritative final score,
--     never the sum of the period columns.
--   * ist_stage is the In-Season Tournament stage, NULL outside it.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    id                          INTEGER PRIMARY KEY,
    date                        DATE,
    season                      INTEGER,      -- start year: 2024 => 2024-25
    status                      VARCHAR,      -- "Final"
    status_state                VARCHAR,      -- "final"
    period                      INTEGER,
    time                        VARCHAR,
    postseason                  BOOLEAN,
    postponed                   BOOLEAN,
    home_team_id                INTEGER,
    visitor_team_id             INTEGER,
    home_team_score             INTEGER,
    visitor_team_score          INTEGER,
    datetime                    TIMESTAMP WITH TIME ZONE,  -- NULL for old games
    home_q1                     INTEGER,
    home_q2                     INTEGER,
    home_q3                     INTEGER,
    home_q4                     INTEGER,
    home_ot1                    INTEGER,
    home_ot2                    INTEGER,
    home_ot3                    INTEGER,
    home_timeouts_remaining     INTEGER,
    home_in_bonus               BOOLEAN,
    visitor_q1                  INTEGER,
    visitor_q2                  INTEGER,
    visitor_q3                  INTEGER,
    visitor_q4                  INTEGER,
    visitor_ot1                 INTEGER,
    visitor_ot2                 INTEGER,
    visitor_ot3                 INTEGER,
    visitor_timeouts_remaining  INTEGER,
    visitor_in_bonus            BOOLEAN,
    ist_stage                   VARCHAR,
    _ingested_at                TIMESTAMP DEFAULT current_timestamp
);

-- -----------------------------------------------------------------------------
-- player_stats  (the box score line: one row per player per game)
-- -----------------------------------------------------------------------------
-- Verified shape: GET /v1/stats on the GOAT tier, 2026-09-26.
--   {"id":15531180,"min":"30","fgm":14,"fga":18,"fg_pct":0.778,"fg3m":8,
--    "fg3a":11,"fg3_pct":0.727,"ftm":1,"fta":2,"ft_pct":0.5,"oreb":0,"dreb":4,
--    "reb":4,"ast":10,"stl":1,"blk":1,"turnover":1,"pf":1,"pts":37,
--    "plus_minus":26,"player":{...},"team":{...},"game":{...}}
--
-- Key set confirmed identical across 1,800 sampled rows spanning seasons 1946,
-- 1960, 1979, 1985, 1996, 2005, 2015, 2023 and 2024, regular and postseason --
-- no endpoint-version drift between eras.
--
-- Constraints verified against 3,000 real 2024 rows:
--   * id is unique                      -> safe as PRIMARY KEY
--   * (game_id, player_id) is unique    -> safe as the grain constraint, and
--     what protects against one box score line arriving twice under two ids
--
-- The turnovers field is singular ("turnover"), not "turnovers".
--
-- min is a string in BOTH forms in live data -- 1,394 bare integers ("30", "0",
-- "48") and 277 clock strings ("24:33", "40:47") in the same sample, plus 129
-- NULLs. Any parser that assumes one form silently mangles the other. Stored
-- verbatim; parse_minutes() in the semantic layer handles both.
--
-- EVERY counting stat is nullable, including pts, fgm, ftm and min. A NULL is
-- "not recorded", which is not zero, and the distinction has to survive into
-- aggregates -- see the era table below. Verified that no row has NULL min with
-- pts > 0, and none has min = '0' with pts > 0, so an absent/zero minute value
-- is a reliable did-not-play signal.
--
-- reb == oreb + dreb in every row where all three are present, so reb is the
-- derived total rather than an independent measurement -- but reb is populated
-- from 1950 while the split only starts in 1973, so reb is the only rebounding
-- number that spans eras.
--
-- WHEN EACH STAT STARTS EXISTING (measured, 100-row sample per season, all 80
-- seasons). This is the single biggest silent-invalidity risk in the dataset:
--   first appears / first >=95% complete, measured over played rows:
--   pts fgm ftm  1946/1946    fta 1946/1948    fga 1946/1974    pf 1946/1975
--   ast 1946/1974   reb 1950/1974   oreb dreb 1962/1983   turnover 1963/1983
--   stl 1970/1983   blk 1973/1983   fg3m 1979/1979   fg3a 1979/1983
--   plus_minus 2019/2024 (2023 is a hole)
-- The onset years are not clean cutoffs -- several stats debut with a SINGLE row
-- in the whole season (oreb, dreb, stl, turnover) and take a decade to become
-- dependable. "Career steals leaders" over the full history ranks by when a
-- player played as much as by how they played. v_stat_first_tracked in the semantic layer makes this
-- checkable in SQL instead of relying on whoever writes the query remembering.
--
-- plus_minus starts in 2019. Measured over ALL rows it looks badly incomplete
-- (~61% in 2024), but that is an artifact of counting did-not-play rows, which
-- are all-NULL by nature. Among rows where the player actually played it is
-- ~99.5%+ complete from 2019 on -- with ONE real exception: season 2023 is only
-- 45.7% and is a genuine hole in the source. Condition on non-NULL when
-- aggregating, and exclude 2023 from any plus_minus comparison.
--
-- The nested objects differ from the dedicated endpoints, which matters for
-- ingestion: the player object here carries a flattened team_id (the /players
-- endpoint nests a full team object instead), and the game object carries
-- flattened home_team_id / visitor_team_id (the /games endpoint nests full team
-- objects). The nested game object is otherwise a complete game record, so
-- ingesting /stats can populate games as a side effect rather than needing a
-- separate pass.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS player_stats (
    id            BIGINT PRIMARY KEY,
    player_id     INTEGER NOT NULL,
    game_id       INTEGER NOT NULL,
    team_id       INTEGER,        -- team the player played for IN THIS GAME
    min           VARCHAR,        -- "30" or "24:33"; both forms occur
    fgm           INTEGER,
    fga           INTEGER,
    fg_pct        DOUBLE,
    fg3m          INTEGER,        -- from 1979
    fg3a          INTEGER,        -- from 1979
    fg3_pct       DOUBLE,
    ftm           INTEGER,
    fta           INTEGER,
    ft_pct        DOUBLE,
    oreb          INTEGER,        -- from 1973
    dreb          INTEGER,        -- from 1973
    reb           INTEGER,        -- from 1950; == oreb + dreb when both present
    ast           INTEGER,        -- from 1947
    stl           INTEGER,        -- from 1973
    blk           INTEGER,        -- from 1973
    turnover      INTEGER,        -- singular, per the API; from 1977
    pf            INTEGER,
    pts           INTEGER,
    plus_minus    INTEGER,        -- from 2019; ~99.5% of played rows, but 2023 is half-missing
    _ingested_at  TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (game_id, player_id)
);

-- -----------------------------------------------------------------------------
-- player_advanced_stats  (advanced metrics: one row per player per game)
-- -----------------------------------------------------------------------------
-- Verified shape: GET /v1/stats/advanced on the GOAT tier, 2026-09-27.
--   {"id":3296931,"pie":0.021,"pace":94.83,"assist_percentage":0.097,
--    "assist_ratio":27.3,"assist_to_turnover":0,"defensive_rating":154.5,
--    "defensive_rebound_percentage":0.156,
--    "effective_field_goal_percentage":0.143,"net_rating":-37.2,
--    "offensive_rating":117.4,"offensive_rebound_percentage":0,
--    "rebound_percentage":0.082,"true_shooting_percentage":0.254,
--    "turnover_ratio":0,"usage_percentage":0.108,
--    "player":{...},"team":{...},"game":{...}}
--
-- ERA COVERAGE IS MUCH NARROWER THAN THE BOX SCORES. Measured by probing every
-- season from 1946 to 2025: the first season with any advanced rows is 1996,
-- and 1996-2025 is complete with no interior gaps. The 50 seasons from 1946 to
-- 1995 return zero rows. So this table covers 30 of the 80 seasons in `games`,
-- and any join against it silently drops everything before 1996 -- which is a
-- LEFT JOIN decision, not an inner-join one, unless the query is scoped to
-- 1996+ on purpose. v_stat_first_tracked records this alongside the box-score
-- stats so one place answers the era question.
--
-- All 16 scalar fields were present with zero NULLs across 700 rows sampled
-- from 1996, 2000, 2001, 2010, 2020, 2024 and 2025, so unlike the box-score
-- table there is no within-era sparsity to work around. Values arrive as JSON
-- numbers that are sometimes integral (assist_to_turnover: 0) and sometimes
-- fractional, hence DOUBLE throughout.
--
-- Percentage fields are FRACTIONS, not percentages: usage_percentage 0.108
-- means 10.8%. The ratio and rating fields are on their own scales
-- (assist_ratio 27.3, offensive_rating 117.4), and the ratings are per-game and
-- therefore wild in small samples -- a defensive_rating of 154.5 in one game is
-- normal noise, not an outlier to clean.
--
-- Nested objects follow the /stats convention exactly (player carries a
-- flattened team_id; game carries flattened home_team_id / visitor_team_id), so
-- ingestion reuses the same writer.
--
-- Its `id` is its own sequence, unrelated to player_stats.id -- the advanced row
-- for a game is id 3296931 while the box score row for the same game is in the
-- 15-million range. Join on (game_id, player_id), never on id. Both `id` and
-- (game_id, player_id) were verified unique across a 2,000-row paged pull.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS player_advanced_stats (
    id                                BIGINT PRIMARY KEY,
    player_id                         INTEGER NOT NULL,
    game_id                           INTEGER NOT NULL,
    team_id                           INTEGER,
    pie                               DOUBLE,   -- player impact estimate
    pace                              DOUBLE,
    assist_percentage                 DOUBLE,   -- fraction, not percent
    assist_ratio                      DOUBLE,
    assist_to_turnover                DOUBLE,
    defensive_rating                  DOUBLE,
    defensive_rebound_percentage      DOUBLE,
    effective_field_goal_percentage   DOUBLE,
    net_rating                        DOUBLE,
    offensive_rating                  DOUBLE,
    offensive_rebound_percentage      DOUBLE,
    rebound_percentage                DOUBLE,
    true_shooting_percentage          DOUBLE,
    turnover_ratio                    DOUBLE,
    usage_percentage                  DOUBLE,
    _ingested_at                      TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (game_id, player_id)
);

-- -----------------------------------------------------------------------------
-- _ingest_log — bookkeeping for the daily job (built in the next step)
-- -----------------------------------------------------------------------------
-- Not an API table. Exists so the ingestion script has somewhere to record
-- what it fetched and so a backfill can be resumed rather than restarted.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS _ingest_log (
    id            BIGINT PRIMARY KEY,
    endpoint      VARCHAR NOT NULL,   -- 'teams' | 'players' | 'games' | 'stats'
    params        VARCHAR,            -- JSON blob of the request params
    season        INTEGER,
    rows_upserted INTEGER,
    http_status   INTEGER,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    error         VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS _ingest_log_seq START 1;
