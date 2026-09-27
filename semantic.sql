-- =============================================================================
-- semantic.sql — the semantic layer: one canonical definition per metric
-- =============================================================================
-- Per design spec section 5, this file is the mechanism that makes validity
-- enforceable. Every derived number the agent can reach is defined exactly once
-- here. Nothing downstream -- not the text-to-SQL agent, not the feature
-- engineering for the projection model, not the showcase artifact -- should
-- re-derive true shooting percentage or a per-36 rate inline. If two answers to
-- the same question ever disagree, the cause should be findable in this file.
--
-- Layering, bottom to top:
--   macros            parse_minutes, ts_pct, efg_pct, per36 -- the atoms
--   v_teams           normalised teams
--   v_players         normalised players
--   v_game_type       regular / play_in / cup_final / playoff per game
--   v_games           game context, one row per game
--   v_stat_first_tracked  which stats exist in which eras
--   v_player_games    THE SPINE: box score + game context + parsed minutes
--   v_player_game_shooting / _per36     per-game derived rates
--   player_rolling_avg(n) + v_player_rolling_5 / _10   trailing form
--   v_player_season_totals / _averages  season rollups
--
-- Run after schema.sql. Views are CREATE OR REPLACE so this file can be
-- re-applied over an existing database to pick up metric changes without
-- touching the data.
-- =============================================================================


-- =============================================================================
-- 1. Atoms — scalar macros
-- =============================================================================
-- Every macro casts its inputs to DOUBLE explicitly. DuckDB parses a literal
-- like 0.44 as DECIMAL, and DECIMAL propagates: without the casts,
-- fga + 0.44 * fta comes back as a DECIMAL, survives into window SUMs, and
-- reaches Python as decimal.Decimal instead of float. Forcing DOUBLE at the
-- bottom keeps every derived number a plain float for the whole layer.

-- The API sends minutes as a string: "39", "39:24", "" or "0:00" for a DNP, and
-- NULL. This is the single place that string becomes a number; every
-- minutes-denominated metric goes through it.
CREATE OR REPLACE MACRO parse_minutes(min_text) AS (
    CASE
        WHEN min_text IS NULL OR trim(min_text) = '' THEN NULL
        WHEN strpos(min_text, ':') > 0 THEN
            try_cast(split_part(min_text, ':', 1) AS DOUBLE)
            + coalesce(try_cast(split_part(min_text, ':', 2) AS DOUBLE), 0) / 60.0
        ELSE try_cast(min_text AS DOUBLE)
    END
);

-- True shooting percentage: points per scoring attempt, where a scoring attempt
-- is a field goal attempt plus 0.44 of a free throw attempt.
--     TS% = PTS / (2 * (FGA + 0.44 * FTA))
-- The 0.44 is the standard estimate of what share of FTAs end a possession (it
-- accounts for and-ones, technicals, and the first of two). It is a convention,
-- not a measurement -- documented here because it is exactly the kind of buried
-- constant that makes two "true shooting" numbers disagree.
--
-- Returns NULL, not 0, when there were no attempts: a player who did not shoot
-- has no true shooting percentage, and averaging a fabricated 0 into a season
-- number would quietly drag it down.
--
-- A NULL in ANY input also yields NULL, which matters more than it sounds. fga
-- is unrecorded for most pre-1974 rows (9.7% coverage in 1946), so 65,450
-- pre-1974 rows have points and free throws but no computable TS% -- Wilbert
-- Kautz scoring 17 in 1946 with fga NULL is not a 0% shooting night, it is an
-- unknown one. Any cross-era TS% comparison silently drops those rows, so scope
-- to season >= 1974 (see v_stat_first_tracked) rather than trusting the average
-- of what survives.
CREATE OR REPLACE MACRO ts_pct(pts, fga, fta) AS (
    cast(pts AS DOUBLE)
    / nullif(2.0 * (cast(fga AS DOUBLE) + 0.44 * cast(fta AS DOUBLE)), 0)
);

-- Effective field goal percentage: field goal percentage with a three counted
-- as 1.5 twos. Unlike TS% it ignores free throws.
CREATE OR REPLACE MACRO efg_pct(fgm, fg3m, fga) AS (
    (cast(fgm AS DOUBLE) + 0.5 * cast(fg3m AS DOUBLE)) / nullif(cast(fga AS DOUBLE), 0)
);

-- Per-36-minutes rate. NULL when minutes are unknown or zero.
--
-- A caveat that belongs with the definition rather than in a caller's head:
-- this is a linear extrapolation and it is only meaningful over a real minutes
-- base. Four points in three minutes becomes 48 per 36, which is arithmetic,
-- not information. Every view exposing per-36 rates also exposes
-- minutes_played so the caller can filter, and the season-level views apply a
-- threshold explicitly.
CREATE OR REPLACE MACRO per36(stat, minutes) AS (
    cast(stat AS DOUBLE) * 36.0 / nullif(cast(minutes AS DOUBLE), 0)
);


-- =============================================================================
-- 2. Normalised dimensions
-- =============================================================================

-- /v1/teams returns more than the NBA: defunct BAA franchises and
-- international/exhibition sides, whose conference is the literal string
-- '    ' and whose division and city are ''. Those become NULL here, and
-- is_nba_franchise marks the 30 current teams so that "league-wide" queries do
-- not silently sweep in Real Madrid.
CREATE OR REPLACE VIEW v_teams AS
SELECT
    id                                   AS team_id,
    nullif(trim(conference), '')         AS conference,
    nullif(trim(division), '')           AS division,
    nullif(trim(city), '')               AS city,
    nullif(trim(name), '')               AS name,
    nullif(trim(full_name), '')          AS full_name,
    nullif(trim(abbreviation), '')       AS abbreviation,
    -- The 30 current franchises are exactly those with a real conference.
    (nullif(trim(conference), '') IN ('East', 'West')) AS is_nba_franchise
FROM teams;

-- Players with the API's string fields parsed. height arrives as "6-11"
-- (feet-inches) and weight as "284".
CREATE OR REPLACE VIEW v_players AS
SELECT
    p.id                                       AS player_id,
    p.first_name,
    p.last_name,
    p.first_name || ' ' || p.last_name         AS full_name,
    nullif(trim(p.position), '')               AS position,
    p.height                                   AS height_raw,
    CASE WHEN strpos(p.height, '-') > 0
         THEN try_cast(split_part(p.height, '-', 1) AS INTEGER) * 12
              + try_cast(split_part(p.height, '-', 2) AS INTEGER)
    END                                        AS height_inches,
    try_cast(p.weight AS INTEGER)              AS weight_lbs,
    nullif(trim(p.jersey_number), '')          AS jersey_number,
    nullif(trim(p.college), '')                AS college,
    nullif(trim(p.country), '')                AS country,
    p.draft_year,
    p.draft_round,
    p.draft_number,
    -- Snapshot of the player's team as of ingestion, NOT their team in any
    -- particular game. Historical questions must use v_player_games.team_id.
    p.team_id                                  AS current_team_id,
    t.abbreviation                             AS current_team_abbreviation
FROM players p
LEFT JOIN v_teams t ON t.team_id = p.team_id;

-- One row per game, with both teams resolved and the final margin.
--
-- total_points and margin use home_team_score / visitor_team_score, never the
-- sum of the period columns: the API exposes only ot1..ot3, so a longer game's
-- period columns do not add up to the final score.
-- -----------------------------------------------------------------------------
-- Game classification: regular / play_in / cup_final / playoff
-- -----------------------------------------------------------------------------
-- The raw `postseason` boolean conflates three different kinds of game into its
-- false branch, and official NBA statistics treat all three differently:
--
--   true regular season  counts toward regular-season averages
--   play-in tournament   counts toward NEITHER regular season nor playoffs
--   NBA Cup final        counts toward neither (the earlier Cup rounds DO count
--                        as regular-season games; only the final does not)
--
-- Left unclassified this silently inflates season lines. Shai Gilgeous-Alexander
-- reads 32.53 ppg over 77 games from the raw flag; official is 32.7 over 76. The
-- single extra game is the 2024 Cup final.
--
-- `ist_stage` cannot help -- the column exists but the API leaves it NULL on all
-- 1,321 games of the 2024 season. And the API's own `postseason` flag is
-- inconsistent about play-in games across seasons: they are postseason=false in
-- 2022-2025, postseason=true in 2019 and 2021, and SPLIT in 2020 (one of the six
-- games false, five true). So neither field can be trusted directly and the
-- classification has to be derived structurally.
--
-- Derivation, and why each piece is needed -- all measured against seasons
-- 2018-2025, with the three real Cup finals as ground truth:
--
--   scheduled_games  the modal per-team count of non-postseason games. 82 in a
--                    normal season, 72 in 2020-21.
--   schedule_is_uniform  whether the minimum per-team count equals that mode.
--                    This is the precondition for the whole count-based
--                    argument: it says every team played at least a full slate,
--                    so anything beyond it is an extra game. It is FALSE for
--                    2019 (the COVID bubble, where teams played 64-75 games by
--                    design and "extra" is meaningless). When it is false the
--                    classification declines to guess and calls everything
--                    regular -- which is correct there, since that season's
--                    play-in game is flagged postseason=true anyway.
--   cup_final        season >= 2023, December, the only game on its date, and
--                    BOTH teams over scheduled_games. All four conditions do
--                    real work: 2022-12-01 (DET-DAL) is a solo December game
--                    but both teams sit at 82, and 2025-11-06 (PHX-LAC) has both
--                    teams over but falls in November. Together they select
--                    exactly one game per season -- 2023-12-09 LAL-IND,
--                    2024-12-17 OKC-MIL, 2025-12-16 NYK-SAS -- and nothing in
--                    2018-2022.
--   play_in          after removing the Cup final, a team's extra games are its
--                    chronologically LAST ones, because the play-in is played
--                    after the regular season ends. So rank each team's games
--                    backwards and take the first (count - scheduled). Requiring
--                    BOTH teams to qualify avoids picking up ordinary games.
--                    Yields exactly 6 games for 2022-2025 and 1 for 2020,
--                    leaving 1,230 regular-season games in every 82-game season.
--
-- The Cup final rule is the weaker of the two: it leans on the NBA continuing to
-- stage the final as a standalone December game. If that ever changes, this view
-- is the one place to fix -- and v_game_classification_audit below is the check
-- that would catch it, since the regular-season game count would stop landing on
-- the schedule's own arithmetic.
--
-- PRECONDITION -- THE SEASON MUST BE FULLY INGESTED. The whole derivation rests
-- on per-team game counts, so a half-loaded season produces a nonsense
-- scheduled_games and misclassifies. Both rules are therefore gated on
-- schedule_is_uniform, which a partial season fails (loading 180 of 1,321 games
-- of 2024 yields scheduled_games=5, min=3, uniform=false), and the whole season
-- then reads as 'regular' -- wrong in detail but not silently inventing play-in
-- or cup games. Measured: without that gate on the Cup rule, a 180-game slice of
-- 2024 produced four spurious cup_final games.
--
-- This dovetails with ingestion: nba_snapshot.duckdb is only published after a
-- complete run, so readers never query a database whose seasons are half-loaded.
-- Check v_game_classification_audit.regular_count_matches before trusting
-- season-level numbers from a database you did not just ingest.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_team_game_slots AS
SELECT season, id AS game_id, date, home_team_id    AS team_id FROM games WHERE NOT postseason
UNION ALL
SELECT season, id AS game_id, date, visitor_team_id AS team_id FROM games WHERE NOT postseason;

CREATE OR REPLACE VIEW v_season_schedule AS
WITH per_team AS (
    SELECT season, team_id, count(*) AS team_games
      FROM v_team_game_slots GROUP BY season, team_id
), modal AS (
    SELECT season, team_games,
           row_number() OVER (PARTITION BY season
                              ORDER BY count(*) DESC, team_games DESC) AS rk
      FROM per_team GROUP BY season, team_games
)
SELECT p.season,
       m.team_games                        AS scheduled_games,
       min(p.team_games)                   AS min_team_games,
       max(p.team_games)                   AS max_team_games,
       (min(p.team_games) = m.team_games)  AS schedule_is_uniform
  FROM per_team p
  JOIN modal m ON m.season = p.season AND m.rk = 1
 GROUP BY p.season, m.team_games;

CREATE OR REPLACE VIEW v_cup_final_games AS
WITH per_team AS (
    SELECT season, team_id, count(*) AS team_games
      FROM v_team_game_slots GROUP BY season, team_id
), per_date AS (
    SELECT season, date, count(*) AS games_that_date
      FROM games WHERE NOT postseason GROUP BY season, date
)
SELECT g.id AS game_id, g.season, g.date
  FROM games g
  JOIN per_date d  ON d.season = g.season AND d.date = g.date
  JOIN v_season_schedule s ON s.season = g.season
  JOIN per_team h  ON h.season = g.season AND h.team_id = g.home_team_id
  JOIN per_team v  ON v.season = g.season AND v.team_id = g.visitor_team_id
 WHERE NOT g.postseason
   AND s.schedule_is_uniform            -- see note below: needs a complete season
   AND g.season >= 2023                 -- the NBA Cup began in 2023-24
   AND month(g.date) = 12
   AND d.games_that_date = 1
   AND h.team_games > s.scheduled_games
   AND v.team_games > s.scheduled_games;

CREATE OR REPLACE VIEW v_play_in_games AS
WITH slots AS (
    SELECT t.* FROM v_team_game_slots t
     WHERE t.game_id NOT IN (SELECT game_id FROM v_cup_final_games)
), per_team AS (
    SELECT season, team_id, count(*) AS team_games FROM slots GROUP BY season, team_id
), ranked AS (
    SELECT s.*, row_number() OVER (PARTITION BY s.season, s.team_id
                                   ORDER BY s.date DESC, s.game_id DESC) AS rn_from_end
      FROM slots s
)
SELECT r.game_id, r.season
  FROM ranked r
  JOIN per_team p ON p.season = r.season AND p.team_id = r.team_id
  JOIN v_season_schedule sc ON sc.season = r.season
 WHERE sc.schedule_is_uniform
   AND p.team_games > sc.scheduled_games
   AND r.rn_from_end <= p.team_games - sc.scheduled_games
 GROUP BY r.game_id, r.season
HAVING count(*) = 2;                    -- both teams must be past their slate

CREATE OR REPLACE VIEW v_game_type AS
SELECT g.id AS game_id,
       g.season,
       CASE WHEN g.postseason            THEN 'playoff'
            WHEN c.game_id IS NOT NULL   THEN 'cup_final'
            WHEN p.game_id IS NOT NULL   THEN 'play_in'
            ELSE 'regular' END AS game_type
  FROM games g
  LEFT JOIN v_cup_final_games c ON c.game_id = g.id
  LEFT JOIN v_play_in_games   p ON p.game_id = g.id;

-- Self-check. In an 82-game season a correct classification leaves exactly
-- scheduled_games * 30 / 2 regular-season games. Any drift means the Cup-final
-- or play-in rule has stopped matching reality.
-- Counts are aggregated per season BEFORE joining. Joining v_game_type and
-- v_team_game_slots together on season alone fans out to games x team-slots
-- (3,043,020 rows for a 1,230-game season), so each side is reduced separately.
CREATE OR REPLACE VIEW v_game_classification_audit AS
WITH type_counts AS (
    SELECT season,
           count(*) FILTER (WHERE game_type = 'regular')   AS regular_games,
           count(*) FILTER (WHERE game_type = 'play_in')   AS play_in_games,
           count(*) FILTER (WHERE game_type = 'cup_final') AS cup_final_games,
           count(*) FILTER (WHERE game_type = 'playoff')   AS playoff_games
      FROM v_game_type
     GROUP BY season
), team_counts AS (
    SELECT season, count(DISTINCT team_id) AS teams
      FROM v_team_game_slots
     GROUP BY season
)
SELECT s.season,
       s.scheduled_games,
       s.schedule_is_uniform,
       tc.regular_games,
       tc.play_in_games,
       tc.cup_final_games,
       tc.playoff_games,
       mc.teams,
       s.scheduled_games * mc.teams / 2 AS expected_regular_games,
       (tc.regular_games = s.scheduled_games * mc.teams / 2) AS regular_count_matches
  FROM v_season_schedule s
  JOIN type_counts tc ON tc.season = s.season
  JOIN team_counts mc ON mc.season = s.season;


CREATE OR REPLACE VIEW v_games AS
SELECT
    g.id                                          AS game_id,
    g.date                                        AS game_date,
    g.season,
    -- 2024 means the 2024-25 season; spell it out for anything user-facing.
    g.season || '-' || right(cast(g.season + 1 AS VARCHAR), 2) AS season_label,
    g.postseason,
    -- Prefer game_type over the raw postseason flag: see the classification
    -- block above. 'regular' here means officially regular season, with the
    -- play-in and the Cup final broken out.
    gt.game_type,
    g.status_state,
    g.postponed,
    g.datetime                                    AS tipoff_utc,
    g.period,
    -- The API exposes only three overtime periods, so period > 4 is the
    -- reliable overtime signal rather than checking ot1..ot3 for non-NULL.
    (g.period > 4)                                AS went_to_overtime,
    g.ist_stage,
    g.home_team_id,
    ht.abbreviation                               AS home_team_abbreviation,
    g.visitor_team_id,
    vt.abbreviation                               AS visitor_team_abbreviation,
    g.home_team_score,
    g.visitor_team_score,
    g.home_team_score + g.visitor_team_score      AS total_points,
    g.home_team_score - g.visitor_team_score      AS home_margin,
    CASE WHEN g.home_team_score > g.visitor_team_score THEN g.home_team_id
         WHEN g.visitor_team_score > g.home_team_score THEN g.visitor_team_id
    END                                           AS winning_team_id
FROM games g
LEFT JOIN v_teams ht ON ht.team_id = g.home_team_id
LEFT JOIN v_teams vt ON vt.team_id = g.visitor_team_id
LEFT JOIN v_game_type gt ON gt.game_id = g.id;


-- -----------------------------------------------------------------------------
-- v_stat_first_tracked — when each stat starts existing
-- -----------------------------------------------------------------------------
-- Measured over the complete 1.68M-row database, not sampled. An earlier version
-- of this table was built from a 100-row-per-season sample and was wrong on five
-- of sixteen stats -- it put steals at 1973 when one 1970 row has them, and
-- turnovers at 1977 when one 1963 row does. Sampling finds the era, not the edge.
--
-- Coverage is measured over rows where the player actually played (NOT is_dnp).
-- Measuring over all rows instead understates every stat, because DNP rows are
-- all-NULL by nature: that mistake is what made plus_minus look like a ~63%
-- field when among played rows it is ~99.5% complete.
--
-- This belongs in the semantic layer because it is a definition, and because a
-- NULL that means "not recorded" behaves exactly like a NULL that means "did
-- not happen" once it reaches an aggregate. SUM and AVG skip NULLs silently, so
-- "career steals" over the full history compares players who had steals
-- recorded against players who did not, and returns a confident wrong ranking.
--
-- Join against this to make a cross-era query state its own scope:
--     SELECT ... FROM v_player_games g
--     JOIN v_stat_first_tracked t ON t.stat = 'stl'
--     WHERE g.season >= t.first_full_season
--
-- first_tracked_season is where a value first appears AT ALL, and for several
-- stats that is a single row in the whole season (oreb, dreb, stl and turnover
-- each debut with exactly one). first_full_season is the first season from which
-- coverage is >=95% and stays there. The gap between them is wide -- steals
-- appear in 1970 but are not dependable until 1983 -- so first_tracked_season is
-- close to useless as a filter and first_full_season is the one to use for
-- anything comparative.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_stat_first_tracked AS
SELECT * FROM (VALUES
    ('pts',        1946, 1946),
    ('fgm',        1946, 1946),
    ('ftm',        1946, 1946),
    ('fta',        1946, 1948),
    -- fga is the surprise: present from 1946 but only 9.7% of played rows that
    -- year, 17% in 1950, 58% in 1955, not reliable until 1974. Early box scores
    -- recorded makes, not attempts. TS% and eFG% both divide by attempts, so
    -- neither is trustworthy before 1974 however confidently they compute.
    ('fga',        1946, 1974),
    ('pf',         1946, 1975),
    ('ast',        1946, 1974),
    ('reb',        1950, 1974),
    ('oreb',       1962, 1983),
    ('dreb',       1962, 1983),
    ('stl',        1970, 1983),
    ('blk',        1973, 1983),
    ('turnover',   1963, 1983),
    ('fg3m',       1979, 1979),
    ('fg3a',       1979, 1983),
    -- plus_minus is ~99.5%+ complete among played rows from 2019 -- EXCEPT
    -- season 2023, which is only 45.7% and is a genuine hole in the source.
    -- first_full_season is 2024 under the strict "and every season after" rule;
    -- 2019-2022 are equally usable, 2023 is the one to exclude.
    ('plus_minus', 2019, 2024)
) AS t(stat, first_tracked_season, first_full_season);


-- =============================================================================
-- 3. v_player_games — the spine
-- =============================================================================
-- One row per player per game: the raw box score line, the game context needed
-- to slice it (date, season, home/road, opponent), and parsed minutes.
-- Everything above this point in the file is a dimension; everything below it
-- builds on this view. Joining player_stats to games by hand in a query is the
-- thing this view exists to stop.
--
-- player_game_seq is the player's chronological game number across all the data
-- in the database. It gives the rolling-window macros a dense integer to order
-- by, and it is what the point-in-time feature joins for the projection model
-- will key on later.
-- =============================================================================
CREATE OR REPLACE VIEW v_player_games AS
SELECT
    ps.id                                    AS stat_id,
    ps.player_id,
    pl.full_name                             AS player_name,
    pl.position,
    ps.game_id,
    g.game_date,
    g.season,
    g.season_label,
    g.postseason,
    g.game_type,
    g.status_state,
    -- Team the player actually played for in THIS game; survives trades.
    ps.team_id,
    CASE WHEN ps.team_id = g.home_team_id THEN g.visitor_team_id
         WHEN ps.team_id = g.visitor_team_id THEN g.home_team_id
    END                                      AS opponent_team_id,
    (ps.team_id = g.home_team_id)            AS is_home,
    ps."min"                                 AS min_raw,
    parse_minutes(ps."min")                  AS minutes_played,
    -- Whether minutes were RECORDED at all, which is a different question from
    -- whether the player played. Minutes are essentially absent before 1951 and
    -- patchy through the 1950s (9.5% of rows in 1951, 31% in 1955).
    (coalesce(parse_minutes(ps."min"), 0) > 0) AS minutes_recorded,
    -- A DNP is a row for a player who did not play. Defining that as "no
    -- minutes" is correct from about 1996 onward and catastrophically wrong
    -- before it, because the early box scores simply have no minutes column:
    -- every 1946 row then reads as a DNP, and 5,790 of those 6,867 rows scored
    -- points. Jerry West came out at 517 games played instead of 932, turning
    -- his 27.0 career average into 48.7.
    --
    -- So minutes are only trusted when present; otherwise fall back to whether
    -- the row shows any statistical activity at all. Effect: 1946 DNPs drop from
    -- 6,867 to 609 and 1960 from 4,565 to 89, while 2024 barely moves
    -- (17,996 -> 17,975) because modern minutes are reliable.
    --
    -- Residual limitation: a pre-1951 player who appeared but recorded nothing
    -- in any column is indistinguishable from one who never left the bench, and
    -- is counted as a DNP. That is a floor on what the source can support, not
    -- something a better rule fixes.
    CASE
        WHEN coalesce(parse_minutes(ps."min"), 0) > 0 THEN false
        WHEN coalesce(ps.pts, 0) + coalesce(ps.fga, 0) + coalesce(ps.fta, 0)
           + coalesce(ps.reb, 0) + coalesce(ps.ast, 0) + coalesce(ps.pf, 0) > 0
             THEN false
        ELSE true
    END                                      AS is_dnp,
    ps.fgm, ps.fga, ps.fg_pct,
    ps.fg3m, ps.fg3a, ps.fg3_pct,
    ps.ftm, ps.fta, ps.ft_pct,
    ps.oreb, ps.dreb, ps.reb,
    ps.ast, ps.stl, ps.blk,
    ps.turnover, ps.pf, ps.pts,
    -- Sparse by nature (2019+, ~63% coverage). NULL here means "not recorded",
    -- never "even". See v_stat_first_tracked before aggregating it.
    ps.plus_minus,
    row_number() OVER (
        PARTITION BY ps.player_id
        ORDER BY g.game_date, ps.game_id
    )                                        AS player_game_seq
FROM player_stats ps
JOIN v_games   g  ON g.game_id   = ps.game_id
LEFT JOIN v_players pl ON pl.player_id = ps.player_id;


-- =============================================================================
-- 4. Per-game derived rates
-- =============================================================================

-- Shooting efficiency, per game.
CREATE OR REPLACE VIEW v_player_game_shooting AS
SELECT
    stat_id,
    player_id,
    player_name,
    game_id,
    game_date,
    season,
    postseason,
    game_type,
    team_id,
    opponent_team_id,
    is_home,
    minutes_played,
    pts, fga, fta, fgm, fg3m,
    -- Total scoring attempts, the denominator of TS% before the 2x. Exposed
    -- because it is the honest sample-size measure for a TS% value: a .900 TS%
    -- on two attempts is not a shooting performance.
    cast(fga AS DOUBLE) + 0.44 * cast(fta AS DOUBLE) AS true_shooting_attempts,
    ts_pct(pts, fga, fta)          AS ts_pct,
    efg_pct(fgm, fg3m, fga)        AS efg_pct
FROM v_player_games;

-- Per-36-minute rates, per game.
--
-- minutes_played is carried through deliberately: these rates are only
-- meaningful over a real minutes base, and the caller needs the denominator in
-- hand to filter on it. See the per36 macro's note.
CREATE OR REPLACE VIEW v_player_game_per36 AS
SELECT
    stat_id,
    player_id,
    player_name,
    game_id,
    game_date,
    season,
    postseason,
    game_type,
    team_id,
    opponent_team_id,
    is_home,
    minutes_played,
    per36(pts,      minutes_played) AS pts_per36,
    per36(reb,      minutes_played) AS reb_per36,
    per36(oreb,     minutes_played) AS oreb_per36,
    per36(dreb,     minutes_played) AS dreb_per36,
    per36(ast,      minutes_played) AS ast_per36,
    per36(stl,      minutes_played) AS stl_per36,
    per36(blk,      minutes_played) AS blk_per36,
    per36(fg3m,     minutes_played) AS fg3m_per36,
    per36(turnover, minutes_played) AS turnover_per36,
    per36(pf,       minutes_played) AS pf_per36,
    per36(fga,      minutes_played) AS fga_per36,
    per36(fta,      minutes_played) AS fta_per36
FROM v_player_games;


-- =============================================================================
-- 5. Rolling N-game form
-- =============================================================================
-- player_rolling_avg(window_games) is a table macro, so N is a parameter rather
-- than a hardcoded set of views: SELECT * FROM player_rolling_avg(7).
--
-- The parameter is deliberately NOT named `n`: a single-letter macro parameter
-- collides during binding with any same-named column reachable through the view
-- tree below it, which fails with "Conflicting column names for column n". The
-- name is positional, so call sites are unaffected.
--
-- Each stat comes in two variants, and the distinction is the whole point:
--
--   *_last<n>      TRAILING AND EXCLUSIVE -- the n games BEFORE this one, not
--                  including it. This is the point-in-time-correct version:
--                  what was knowable going into the game. This is what feeds
--                  the projection model. Using the inclusive version as a
--                  feature leaks the outcome into its own predictor and
--                  produces a validation score that looks excellent and means
--                  nothing.
--
--   *_last<n>_incl INCLUSIVE of the current game -- the descriptive version,
--                  for "what are his last 10 games looking like". Correct for
--                  reporting, never correct as a feature.
--
-- Rolling TS% is computed from summed points and summed attempts across the
-- window, NOT as the average of the per-game TS% values. Averaging ratios
-- weights a 2-attempt night equally with a 20-attempt night. Same reasoning as
-- the season views below.
--
-- The window is ordered by player_game_seq and partitioned by player_id, so it
-- spans a player's whole history: across seasons, across trades, AND across the
-- regular-season/playoff boundary (the streak follows the player, which is what
-- recent form means). That last one is easy to miss -- a player's first playoff
-- game carries his late-regular-season form, and his first game of next season
-- carries his playoff form. game_type is exposed on every row so a call site can
-- scope the window (e.g. regular season only); filter on season or game_type if a
-- boundary should reset it.
--
-- A second NULL subtlety, separate from DNPs: avg() ignores NULLs, so a window
-- spanning a stat's tracking boundary averages only the games where the stat
-- was recorded, over a games_in_window count that includes the games where it
-- was not. For stl/blk/turnover before ~1980 the rolling value is therefore
-- computed off a smaller base than it appears to be. Join v_stat_first_tracked
-- and filter the season range for any historical work.
--
-- OPEN DECISION -- DNPs are currently INCLUDED in the window. A game the player
-- sat out enters the average as a zero and drags it down. That is the right
-- reading of "his last 5 games" for fantasy purposes, where a missed game
-- really is zero production, and the wrong reading of "his recent form", where
-- you want production per game actually played. games_in_window and the is_dnp
-- flag on v_player_games make the choice visible rather than silent, but this
-- should get settled deliberately before the projection model consumes these
-- columns -- filtering DNPs out of the window changes every feature value.
-- =============================================================================
CREATE OR REPLACE MACRO player_rolling_avg(window_games) AS TABLE
SELECT
    player_id,
    player_name,
    game_id,
    game_date,
    season,
    game_type,
    team_id,
    opponent_team_id,
    is_home,
    player_game_seq,
    minutes_played,
    pts, reb, ast, stl, blk, fg3m, turnover,

    -- games actually available in the trailing window; < n early in a career
    count(*)      OVER w_prior AS games_in_window,

    avg(minutes_played) OVER w_prior AS minutes_last_n,
    avg(pts)      OVER w_prior AS pts_last_n,
    avg(reb)      OVER w_prior AS reb_last_n,
    avg(ast)      OVER w_prior AS ast_last_n,
    avg(stl)      OVER w_prior AS stl_last_n,
    avg(blk)      OVER w_prior AS blk_last_n,
    avg(fg3m)     OVER w_prior AS fg3m_last_n,
    avg(turnover) OVER w_prior AS turnover_last_n,
    ts_pct(sum(pts) OVER w_prior,
           sum(fga) OVER w_prior,
           sum(fta) OVER w_prior)  AS ts_pct_last_n,

    avg(minutes_played) OVER w_incl AS minutes_last_n_incl,
    avg(pts)      OVER w_incl AS pts_last_n_incl,
    avg(reb)      OVER w_incl AS reb_last_n_incl,
    avg(ast)      OVER w_incl AS ast_last_n_incl,
    avg(stl)      OVER w_incl AS stl_last_n_incl,
    avg(blk)      OVER w_incl AS blk_last_n_incl,
    avg(fg3m)     OVER w_incl AS fg3m_last_n_incl,
    avg(turnover) OVER w_incl AS turnover_last_n_incl,
    ts_pct(sum(pts) OVER w_incl,
           sum(fga) OVER w_incl,
           sum(fta) OVER w_incl)  AS ts_pct_last_n_incl
FROM v_player_games
WINDOW
    w_prior AS (PARTITION BY player_id ORDER BY player_game_seq
                ROWS BETWEEN window_games PRECEDING AND 1 PRECEDING),
    w_incl  AS (PARTITION BY player_id ORDER BY player_game_seq
                ROWS BETWEEN window_games - 1 PRECEDING AND CURRENT ROW);

-- Convenience views for the two windows this project will reach for most.
CREATE OR REPLACE VIEW v_player_rolling_5  AS SELECT * FROM player_rolling_avg(5);
CREATE OR REPLACE VIEW v_player_rolling_10 AS SELECT * FROM player_rolling_avg(10);


-- =============================================================================
-- 6. Season rollups
-- =============================================================================

-- Grouped by game_type, NOT by the raw postseason flag. game_type='regular' is
-- what matches official regular-season statistics; play_in and cup_final become
-- their own rows instead of silently padding the regular-season line. A caller
-- wanting the old "everything that was not a playoff game" behaviour can sum
-- across game_type IN ('regular','play_in','cup_final'), but should not want to.
--
-- Season totals per player per team. Split by team_id rather than collapsed, so
-- a traded player produces one row per stint -- which is what makes the trade
-- case in design spec section 4 checkable: the same player_id should appear
-- twice in one season with two different team_ids. Sum across the rows for a
-- combined season line.
CREATE OR REPLACE VIEW v_player_season_totals AS
SELECT
    player_id,
    player_name,
    season,
    season_label,
    game_type,
    postseason,
    team_id,
    count(*)                        AS games_on_roster,
    count(*) FILTER (WHERE NOT is_dnp) AS games_played,
    sum(minutes_played)             AS minutes,
    sum(fgm)  AS fgm,  sum(fga)  AS fga,
    sum(fg3m) AS fg3m, sum(fg3a) AS fg3a,
    sum(ftm)  AS ftm,  sum(fta)  AS fta,
    sum(oreb) AS oreb, sum(dreb) AS dreb, sum(reb) AS reb,
    sum(ast)  AS ast,  sum(stl)  AS stl,  sum(blk) AS blk,
    sum(turnover) AS turnover, sum(pf) AS pf, sum(pts) AS pts,
    -- plus_minus is summed only over the games that actually have it, and the
    -- count comes with it. Without that denominator a season total of +140 over
    -- 12 recorded games is indistinguishable from +140 over 82, which is the
    -- trap this column is most likely to spring.
    sum(plus_minus)                          AS plus_minus,
    count(plus_minus)                        AS games_with_plus_minus
FROM v_player_games
GROUP BY player_id, player_name, season, season_label, game_type, postseason, team_id;

-- Season per-game and rate lines, built from the totals above.
--
-- The rate metrics here are computed from season TOTALS, not as the average of
-- the per-game rate values in v_player_game_shooting. These give different
-- answers, and only the totals version is right: averaging per-game TS% lets a
-- 3-attempt night count as much as a 25-attempt night. The same applies to
-- fg_pct and the per-36 rates. This is the single most likely place for the
-- agent to produce a plausible wrong number, which is why the season line is
-- defined here once rather than left to be assembled per question.
--
-- Per-game averages divide by games_played (games with minutes), not by
-- games_on_roster, so DNPs do not dilute them.
CREATE OR REPLACE VIEW v_player_season_averages AS
SELECT
    player_id,
    player_name,
    season,
    season_label,
    game_type,
    postseason,
    team_id,
    games_played,
    minutes,
    cast(minutes  AS DOUBLE) / nullif(games_played, 0) AS mpg,
    cast(pts      AS DOUBLE) / nullif(games_played, 0) AS ppg,
    cast(reb      AS DOUBLE) / nullif(games_played, 0) AS rpg,
    cast(ast      AS DOUBLE) / nullif(games_played, 0) AS apg,
    cast(stl      AS DOUBLE) / nullif(games_played, 0) AS spg,
    cast(blk      AS DOUBLE) / nullif(games_played, 0) AS bpg,
    cast(fg3m     AS DOUBLE) / nullif(games_played, 0) AS fg3mpg,
    cast(turnover AS DOUBLE) / nullif(games_played, 0) AS topg,

    cast(fgm  AS DOUBLE) / nullif(cast(fga  AS DOUBLE), 0) AS fg_pct,
    cast(fg3m AS DOUBLE) / nullif(cast(fg3a AS DOUBLE), 0) AS fg3_pct,
    cast(ftm  AS DOUBLE) / nullif(cast(fta  AS DOUBLE), 0) AS ft_pct,
    ts_pct(pts, fga, fta)                  AS ts_pct,
    efg_pct(fgm, fg3m, fga)                AS efg_pct,

    per36(pts,      minutes) AS pts_per36,
    per36(reb,      minutes) AS reb_per36,
    per36(ast,      minutes) AS ast_per36,
    per36(stl,      minutes) AS stl_per36,
    per36(blk,      minutes) AS blk_per36,
    per36(fg3m,     minutes) AS fg3m_per36,
    per36(turnover, minutes) AS turnover_per36
FROM v_player_season_totals;
