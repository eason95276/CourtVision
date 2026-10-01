-- CourtVision V2 single-game postgame fact queries.
--
-- Every named section is an independently readable SELECT statement. Set
-- :game_id to the target game before execution. The build script executes the
-- sections separately and only reshapes their result rows for JSON/Markdown.
--
-- A scoring event is a row whose non-NULL score snapshot changes from the
-- previous non-NULL snapshot by adding 1 to 3 points to exactly one team.
-- Rows with NULL scores and repeated snapshots on period/replay rows are not
-- scoring events.

-- query: game
-- What was the scheduled game, final result, margin, and actual total?
SELECT
    g.game_id,
    g.game_date,
    g.season,
    g.season_type,
    g.away_team_id,
    g.away_team_tricode,
    away.team_city || ' ' || away.team_name AS away_team_name,
    g.away_points,
    g.home_team_id,
    g.home_team_tricode,
    home.team_city || ' ' || home.team_name AS home_team_name,
    g.home_points,
    ABS(g.home_points - g.away_points) AS final_margin,
    g.home_points + g.away_points AS actual_total,
    CASE
        WHEN g.home_points > g.away_points THEN g.home_team_tricode
        WHEN g.away_points > g.home_points THEN g.away_team_tricode
        ELSE NULL
    END AS winner_tricode
FROM games AS g
JOIN team_boxscores AS away
    ON away.game_id = g.game_id AND away.side = 'away'
JOIN team_boxscores AS home
    ON home.game_id = g.game_id AND home.side = 'home'
WHERE g.game_id = :game_id;

-- query: score_by_period
-- How many points did each team score in each regulation period?
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT
        period,
        CASE WHEN score_away > prior_away THEN score_away - prior_away ELSE 0 END
            AS away_points_added,
        CASE WHEN score_home > prior_home THEN score_home - prior_home ELSE 0 END
            AS home_points_added
    FROM score_snapshots
    WHERE
        (score_away > prior_away AND score_home = prior_home
            AND score_away - prior_away BETWEEN 1 AND 3)
        OR
        (score_home > prior_home AND score_away = prior_away
            AND score_home - prior_home BETWEEN 1 AND 3)
),
periods(period) AS (VALUES (1), (2), (3), (4))
SELECT
    periods.period,
    g.away_team_tricode,
    COALESCE(SUM(scoring_events.away_points_added), 0) AS away_points,
    g.home_team_tricode,
    COALESCE(SUM(scoring_events.home_points_added), 0) AS home_points,
    COALESCE(SUM(scoring_events.home_points_added), 0)
        - COALESCE(SUM(scoring_events.away_points_added), 0)
        AS home_minus_away
FROM periods
CROSS JOIN games AS g
LEFT JOIN scoring_events ON scoring_events.period = periods.period
WHERE g.game_id = :game_id
GROUP BY periods.period, g.away_team_tricode, g.home_team_tricode
ORDER BY periods.period;

-- query: score_by_half
-- What were the first-half and second-half scores?
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT
        CASE WHEN period <= 2 THEN 1 ELSE 2 END AS half,
        CASE WHEN score_away > prior_away THEN score_away - prior_away ELSE 0 END
            AS away_points_added,
        CASE WHEN score_home > prior_home THEN score_home - prior_home ELSE 0 END
            AS home_points_added
    FROM score_snapshots
    WHERE
        period BETWEEN 1 AND 4
        AND (
            (score_away > prior_away AND score_home = prior_home
                AND score_away - prior_away BETWEEN 1 AND 3)
            OR
            (score_home > prior_home AND score_away = prior_away
                AND score_home - prior_home BETWEEN 1 AND 3)
        )
),
halves(half) AS (VALUES (1), (2))
SELECT
    halves.half,
    g.away_team_tricode,
    COALESCE(SUM(scoring_events.away_points_added), 0) AS away_points,
    g.home_team_tricode,
    COALESCE(SUM(scoring_events.home_points_added), 0) AS home_points,
    COALESCE(SUM(scoring_events.home_points_added), 0)
        - COALESCE(SUM(scoring_events.away_points_added), 0)
        AS home_minus_away
FROM halves
CROSS JOIN games AS g
LEFT JOIN scoring_events ON scoring_events.half = halves.half
WHERE g.game_id = :game_id
GROUP BY halves.half, g.away_team_tricode, g.home_team_tricode
ORDER BY halves.half;

-- query: flow_summary
-- How many times did the lead change, and how many scoring events produced a tie?
-- A lead change occurs when the leader after a non-tied scoring event differs
-- from the leader after the previous non-tied scoring event. The opening 0-0
-- is excluded. A tie is counted whenever a scoring event makes the score equal.
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT *
    FROM score_snapshots
    WHERE
        (score_away > prior_away AND score_home = prior_home
            AND score_away - prior_away BETWEEN 1 AND 3)
        OR
        (score_home > prior_home AND score_away = prior_away
            AND score_home - prior_home BETWEEN 1 AND 3)
),
non_tied_leaders AS (
    SELECT
        action_id,
        CASE WHEN score_away > score_home THEN 'away' ELSE 'home' END AS leader_side
    FROM scoring_events
    WHERE score_away <> score_home
),
leader_sequence AS (
    SELECT
        leader_side,
        LAG(leader_side) OVER (ORDER BY action_id) AS prior_leader_side
    FROM non_tied_leaders
)
SELECT
    SUM(CASE
        WHEN prior_leader_side IS NOT NULL AND leader_side <> prior_leader_side
            THEN 1 ELSE 0
    END) AS lead_changes,
    (SELECT COUNT(*) FROM scoring_events WHERE score_away = score_home) AS ties,
    (SELECT COUNT(*) FROM scoring_events) AS scoring_event_count
FROM leader_sequence;

-- query: maximum_leads
-- What was each team's largest lead, and at which first scoring event was it reached?
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT *
    FROM score_snapshots
    WHERE
        (score_away > prior_away AND score_home = prior_home
            AND score_away - prior_away BETWEEN 1 AND 3)
        OR
        (score_home > prior_home AND score_away = prior_away
            AND score_home - prior_home BETWEEN 1 AND 3)
),
team_leads AS (
    SELECT
        g.away_team_tricode AS team_tricode,
        s.action_id,
        s.period,
        s.clock,
        s.score_away,
        s.score_home,
        s.score_away - s.score_home AS lead
    FROM scoring_events AS s
    JOIN games AS g ON g.game_id = s.game_id
    UNION ALL
    SELECT
        g.home_team_tricode AS team_tricode,
        s.action_id,
        s.period,
        s.clock,
        s.score_away,
        s.score_home,
        s.score_home - s.score_away AS lead
    FROM scoring_events AS s
    JOIN games AS g ON g.game_id = s.game_id
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY team_tricode ORDER BY lead DESC, action_id
        ) AS row_number
    FROM team_leads
)
SELECT
    team_tricode,
    MAX(lead, 0) AS maximum_lead,
    action_id AS first_reached_action_id,
    period AS first_reached_period,
    clock AS first_reached_clock,
    score_away,
    score_home
FROM ranked
WHERE row_number = 1
ORDER BY team_tricode;

-- query: scoring_runs
-- What were the maximal uninterrupted scoring runs?
-- A run is a maximal sequence of consecutive scoring events by one team. It
-- may cross a period boundary and ends only when the opponent scores.
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT
        s.game_id,
        s.action_id,
        s.period,
        s.clock,
        s.description,
        s.prior_away,
        s.prior_home,
        s.score_away,
        s.score_home,
        CASE
            WHEN s.score_away > s.prior_away THEN g.away_team_tricode
            ELSE g.home_team_tricode
        END AS scoring_team,
        CASE
            WHEN s.score_away > s.prior_away THEN s.score_away - s.prior_away
            ELSE s.score_home - s.prior_home
        END AS points_added
    FROM score_snapshots AS s
    JOIN games AS g ON g.game_id = s.game_id
    WHERE
        (s.score_away > s.prior_away AND s.score_home = s.prior_home
            AND s.score_away - s.prior_away BETWEEN 1 AND 3)
        OR
        (s.score_home > s.prior_home AND s.score_away = s.prior_away
            AND s.score_home - s.prior_home BETWEEN 1 AND 3)
),
ordered AS (
    SELECT
        *,
        LAG(scoring_team) OVER (ORDER BY period, action_id) AS prior_scoring_team
    FROM scoring_events
),
tagged AS (
    SELECT
        *,
        SUM(CASE
            WHEN prior_scoring_team = scoring_team THEN 0 ELSE 1
        END) OVER (ORDER BY period, action_id) AS run_id
    FROM ordered
),
run_totals AS (
    SELECT
        run_id,
        scoring_team,
        SUM(points_added) AS run_points,
        COUNT(*) AS scoring_event_count,
        MIN(action_id) AS start_action_id,
        MAX(action_id) AS end_action_id
    FROM tagged
    GROUP BY run_id, scoring_team
),
ranked AS (
    SELECT
        *,
        DENSE_RANK() OVER (ORDER BY run_points DESC) AS run_rank
    FROM run_totals
)
SELECT
    r.run_id,
    r.run_rank,
    r.scoring_team,
    r.run_points,
    r.scoring_event_count,
    r.start_action_id,
    start.period AS start_period,
    start.clock AS start_clock,
    r.end_action_id,
    finish.period AS end_period,
    finish.clock AS end_clock,
    start.prior_away AS score_before_away,
    start.prior_home AS score_before_home,
    finish.score_away AS score_after_away,
    finish.score_home AS score_after_home
FROM ranked AS r
JOIN tagged AS start
    ON start.run_id = r.run_id AND start.action_id = r.start_action_id
JOIN tagged AS finish
    ON finish.run_id = r.run_id AND finish.action_id = r.end_action_id
ORDER BY r.run_points DESC, r.start_action_id;

-- query: maximum_run_events
-- Which score-changing events form every run tied for the game's maximum?
WITH score_snapshots AS (
    SELECT
        p.*,
        LAG(p.score_away) OVER (ORDER BY p.period, p.action_id) AS prior_away,
        LAG(p.score_home) OVER (ORDER BY p.period, p.action_id) AS prior_home
    FROM play_by_play AS p
    WHERE
        p.game_id = :game_id
        AND p.score_away IS NOT NULL
        AND p.score_home IS NOT NULL
),
scoring_events AS (
    SELECT
        s.game_id,
        s.action_id,
        s.period,
        s.clock,
        s.description,
        s.prior_away,
        s.prior_home,
        s.score_away,
        s.score_home,
        CASE
            WHEN s.score_away > s.prior_away THEN g.away_team_tricode
            ELSE g.home_team_tricode
        END AS scoring_team,
        CASE
            WHEN s.score_away > s.prior_away THEN s.score_away - s.prior_away
            ELSE s.score_home - s.prior_home
        END AS points_added
    FROM score_snapshots AS s
    JOIN games AS g ON g.game_id = s.game_id
    WHERE
        (s.score_away > s.prior_away AND s.score_home = s.prior_home
            AND s.score_away - s.prior_away BETWEEN 1 AND 3)
        OR
        (s.score_home > s.prior_home AND s.score_away = s.prior_away
            AND s.score_home - s.prior_home BETWEEN 1 AND 3)
),
ordered AS (
    SELECT
        *,
        LAG(scoring_team) OVER (ORDER BY period, action_id) AS prior_scoring_team
    FROM scoring_events
),
tagged AS (
    SELECT
        *,
        SUM(CASE
            WHEN prior_scoring_team = scoring_team THEN 0 ELSE 1
        END) OVER (ORDER BY period, action_id) AS run_id
    FROM ordered
),
run_totals AS (
    SELECT run_id, SUM(points_added) AS run_points
    FROM tagged
    GROUP BY run_id
),
maximum AS (SELECT MAX(run_points) AS run_points FROM run_totals)
SELECT
    t.run_id,
    t.scoring_team,
    t.action_id,
    t.period,
    t.clock,
    t.points_added,
    t.prior_away AS score_before_away,
    t.prior_home AS score_before_home,
    t.score_away AS score_after_away,
    t.score_home AS score_after_home,
    t.description
FROM tagged AS t
JOIN run_totals AS r ON r.run_id = t.run_id
JOIN maximum AS m ON m.run_points = r.run_points
ORDER BY t.run_id, t.period, t.action_id;

-- query: four_factors
-- What are the two teams' four-factor values from team box-score fields?
SELECT
    t.side,
    t.team_id,
    t.team_tricode,
    t.field_goals_made AS fgm,
    t.field_goals_attempted AS fga,
    t.three_pointers_made AS fg3m,
    t.free_throws_attempted AS fta,
    t.turnovers AS tov,
    t.rebounds_offensive AS orb,
    opponent.rebounds_defensive AS opponent_drb,
    CASE WHEN t.field_goals_attempted = 0 THEN NULL
        ELSE (t.field_goals_made + 0.5 * t.three_pointers_made)
            / t.field_goals_attempted
    END AS efg_raw,
    CASE WHEN t.field_goals_attempted = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0
            * (t.field_goals_made + 0.5 * t.three_pointers_made)
            / t.field_goals_attempted)
    END AS efg_display,
    CASE WHEN t.field_goals_attempted + 0.44 * t.free_throws_attempted
            + t.turnovers = 0 THEN NULL
        ELSE t.turnovers
            / (t.field_goals_attempted + 0.44 * t.free_throws_attempted
                + t.turnovers)
    END AS tov_rate_raw,
    CASE WHEN t.field_goals_attempted + 0.44 * t.free_throws_attempted
            + t.turnovers = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * t.turnovers
            / (t.field_goals_attempted + 0.44 * t.free_throws_attempted
                + t.turnovers))
    END AS tov_rate_display,
    CASE WHEN t.rebounds_offensive + opponent.rebounds_defensive = 0 THEN NULL
        ELSE 1.0 * t.rebounds_offensive
            / (t.rebounds_offensive + opponent.rebounds_defensive)
    END AS orb_rate_raw,
    CASE WHEN t.rebounds_offensive + opponent.rebounds_defensive = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * t.rebounds_offensive
            / (t.rebounds_offensive + opponent.rebounds_defensive))
    END AS orb_rate_display,
    CASE WHEN t.field_goals_attempted = 0 THEN NULL
        ELSE 1.0 * t.free_throws_attempted / t.field_goals_attempted
    END AS ftr_raw,
    CASE WHEN t.field_goals_attempted = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * t.free_throws_attempted
            / t.field_goals_attempted)
    END AS ftr_display
FROM team_boxscores AS t
JOIN team_boxscores AS opponent
    ON opponent.game_id = t.game_id AND opponent.team_id <> t.team_id
WHERE t.game_id = :game_id
ORDER BY CASE t.side WHEN 'away' THEN 1 ELSE 2 END;

-- query: players
-- What did every player with a non-NULL minutes record produce?
-- This retains a recorded 0:00 appearance and excludes rows explicitly lacking
-- an appearance time. Percentage denominators of zero return NULL.
SELECT
    p.team_id,
    p.team_tricode,
    p.person_id,
    p.full_name,
    p.starter,
    p.minutes,
    p.points,
    p.rebounds_total AS rebounds,
    p.assists,
    p.steals,
    p.blocks,
    p.turnovers,
    p.field_goals_made AS fgm,
    p.field_goals_attempted AS fga,
    CASE WHEN p.field_goals_attempted = 0 THEN NULL
        ELSE 1.0 * p.field_goals_made / p.field_goals_attempted
    END AS fg_pct_raw,
    CASE WHEN p.field_goals_attempted = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * p.field_goals_made
            / p.field_goals_attempted)
    END AS fg_pct_display,
    p.three_pointers_made AS fg3m,
    p.three_pointers_attempted AS fg3a,
    CASE WHEN p.three_pointers_attempted = 0 THEN NULL
        ELSE 1.0 * p.three_pointers_made / p.three_pointers_attempted
    END AS fg3_pct_raw,
    CASE WHEN p.three_pointers_attempted = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * p.three_pointers_made
            / p.three_pointers_attempted)
    END AS fg3_pct_display,
    p.free_throws_made AS ftm,
    p.free_throws_attempted AS fta,
    CASE WHEN p.free_throws_attempted = 0 THEN NULL
        ELSE 1.0 * p.free_throws_made / p.free_throws_attempted
    END AS ft_pct_raw,
    CASE WHEN p.free_throws_attempted = 0 THEN NULL
        ELSE printf('%.1f%%', 100.0 * p.free_throws_made
            / p.free_throws_attempted)
    END AS ft_pct_display,
    p.plus_minus_points AS plus_minus,
    CASE WHEN 2.0 * (p.field_goals_attempted + 0.44 * p.free_throws_attempted) = 0
        THEN NULL
        ELSE p.points
            / (2.0 * (p.field_goals_attempted + 0.44 * p.free_throws_attempted))
    END AS ts_pct_raw,
    CASE WHEN 2.0 * (p.field_goals_attempted + 0.44 * p.free_throws_attempted) = 0
        THEN NULL
        ELSE printf('%.1f%%', 100.0 * p.points
            / (2.0 * (p.field_goals_attempted + 0.44 * p.free_throws_attempted)))
    END AS ts_pct_display
FROM player_boxscores AS p
WHERE p.game_id = :game_id AND p.minutes IS NOT NULL
ORDER BY p.team_tricode, p.points DESC, p.full_name;

-- query: player_leaders
-- Who led each team in points, assists, rebounds, and plus/minus?
-- Ties are retained, and rows without an appearance time are excluded.
WITH appeared AS (
    SELECT * FROM player_boxscores
    WHERE game_id = :game_id AND minutes IS NOT NULL
),
metrics AS (
    SELECT team_tricode, person_id, full_name, 'points' AS metric, points * 1.0 AS value
    FROM appeared
    UNION ALL
    SELECT team_tricode, person_id, full_name, 'assists', assists * 1.0 FROM appeared
    UNION ALL
    SELECT team_tricode, person_id, full_name, 'rebounds', rebounds_total * 1.0 FROM appeared
    UNION ALL
    SELECT team_tricode, person_id, full_name, 'plus_minus', plus_minus_points FROM appeared
),
ranked AS (
    SELECT
        *,
        RANK() OVER (PARTITION BY team_tricode, metric ORDER BY value DESC) AS rank
    FROM metrics
)
SELECT team_tricode, metric, person_id, full_name, value
FROM ranked
WHERE rank = 1
ORDER BY team_tricode, metric, person_id;

-- query: shot_zones
-- How did the teams differ across the database's existing shot_zone_basic values?
-- Points use the database's shot_type and shot_made_flag fields. Each row also
-- carries both teams' attempt counts so small-sample percentage gaps stay visible.
WITH teams AS (
    SELECT game_id, side, team_id, team_tricode, field_goals_attempted
    FROM team_boxscores
    WHERE game_id = :game_id
),
zones AS (
    SELECT DISTINCT shot_zone_basic AS zone
    FROM shots
    WHERE game_id = :game_id
),
zone_grid AS (
    SELECT teams.*, zones.zone
    FROM teams CROSS JOIN zones
),
zone_totals AS (
    SELECT
        team_id,
        shot_zone_basic AS zone,
        COUNT(*) AS attempts,
        SUM(shot_made_flag) AS makes,
        SUM(CASE shot_type
            WHEN '3PT Field Goal' THEN 3 * shot_made_flag
            WHEN '2PT Field Goal' THEN 2 * shot_made_flag
            ELSE 0
        END) AS points
    FROM shots
    WHERE game_id = :game_id
    GROUP BY team_id, shot_zone_basic
),
team_zones AS (
    SELECT
        grid.side,
        grid.team_id,
        grid.team_tricode,
        grid.field_goals_attempted AS team_fga,
        grid.zone,
        COALESCE(totals.attempts, 0) AS attempts,
        COALESCE(totals.makes, 0) AS makes,
        COALESCE(totals.points, 0) AS points,
        CASE WHEN grid.field_goals_attempted = 0 THEN NULL
            ELSE 1.0 * COALESCE(totals.attempts, 0) / grid.field_goals_attempted
        END AS attempt_share_raw,
        CASE WHEN COALESCE(totals.attempts, 0) = 0 THEN NULL
            ELSE 1.0 * COALESCE(totals.makes, 0) / totals.attempts
        END AS fg_pct_raw
    FROM zone_grid AS grid
    LEFT JOIN zone_totals AS totals
        ON totals.team_id = grid.team_id AND totals.zone = grid.zone
),
comparison AS (
    SELECT
        current.*,
        opponent.team_tricode AS opponent_tricode,
        opponent.attempts AS opponent_attempts,
        opponent.attempt_share_raw AS opponent_attempt_share_raw,
        opponent.fg_pct_raw AS opponent_fg_pct_raw
    FROM team_zones AS current
    JOIN team_zones AS opponent
        ON opponent.zone = current.zone
        AND opponent.team_id <> current.team_id
)
SELECT
    side,
    team_id,
    team_tricode,
    zone,
    attempts,
    team_fga,
    attempt_share_raw,
    CASE WHEN attempt_share_raw IS NULL THEN NULL
        ELSE printf('%.1f%%', 100.0 * attempt_share_raw)
    END AS attempt_share_display,
    makes,
    fg_pct_raw,
    CASE WHEN fg_pct_raw IS NULL THEN NULL
        ELSE printf('%.1f%%', 100.0 * fg_pct_raw)
    END AS fg_pct_display,
    points,
    opponent_tricode,
    opponent_attempts,
    attempt_share_raw - opponent_attempt_share_raw AS attempt_share_diff_raw,
    CASE WHEN attempt_share_raw IS NULL OR opponent_attempt_share_raw IS NULL
        THEN NULL
        ELSE printf('%+.1f pp', 100.0
            * (attempt_share_raw - opponent_attempt_share_raw))
    END AS attempt_share_diff_display,
    CASE WHEN fg_pct_raw IS NULL OR opponent_fg_pct_raw IS NULL THEN NULL
        ELSE fg_pct_raw - opponent_fg_pct_raw
    END AS fg_pct_diff_raw,
    CASE WHEN fg_pct_raw IS NULL OR opponent_fg_pct_raw IS NULL THEN NULL
        ELSE printf('%+.1f pp', 100.0 * (fg_pct_raw - opponent_fg_pct_raw))
    END AS fg_pct_diff_display
FROM comparison
ORDER BY zone, CASE side WHEN 'away' THEN 1 ELSE 2 END;

-- query: shot_zone_share_top3
-- Which three zones had the largest absolute team attempt-share gaps?
WITH teams AS (
    SELECT team_id, team_tricode, side, field_goals_attempted
    FROM team_boxscores WHERE game_id = :game_id
),
zones AS (
    SELECT DISTINCT shot_zone_basic AS zone FROM shots WHERE game_id = :game_id
),
counts AS (
    SELECT team_id, shot_zone_basic AS zone, COUNT(*) AS attempts
    FROM shots WHERE game_id = :game_id GROUP BY team_id, shot_zone_basic
),
shares AS (
    SELECT
        t.side, t.team_id, t.team_tricode, z.zone,
        COALESCE(c.attempts, 0) AS attempts,
        CASE WHEN t.field_goals_attempted = 0 THEN NULL
            ELSE 1.0 * COALESCE(c.attempts, 0) / t.field_goals_attempted
        END AS share
    FROM teams AS t CROSS JOIN zones AS z
    LEFT JOIN counts AS c ON c.team_id = t.team_id AND c.zone = z.zone
),
pivoted AS (
    SELECT
        zone,
        MAX(CASE WHEN side = 'away' THEN team_tricode END) AS away_team_tricode,
        MAX(CASE WHEN side = 'away' THEN attempts END) AS away_attempts,
        MAX(CASE WHEN side = 'away' THEN share END) AS away_share_raw,
        MAX(CASE WHEN side = 'home' THEN team_tricode END) AS home_team_tricode,
        MAX(CASE WHEN side = 'home' THEN attempts END) AS home_attempts,
        MAX(CASE WHEN side = 'home' THEN share END) AS home_share_raw
    FROM shares GROUP BY zone
)
SELECT
    zone,
    away_team_tricode,
    away_attempts,
    away_share_raw,
    home_team_tricode,
    home_attempts,
    home_share_raw,
    away_share_raw - home_share_raw AS away_minus_home_share_raw,
    ABS(away_share_raw - home_share_raw) AS absolute_share_gap_raw,
    printf('%.1f pp', 100.0 * ABS(away_share_raw - home_share_raw))
        AS absolute_share_gap_display
FROM pivoted
ORDER BY absolute_share_gap_raw DESC, zone
LIMIT 3;

-- query: shot_zone_fg_pct_largest
-- Which zone had the largest absolute field-goal percentage gap?
-- Both attempt counts are returned to expose the comparison's sample size.
WITH teams AS (
    SELECT team_id, team_tricode, side FROM team_boxscores WHERE game_id = :game_id
),
zones AS (
    SELECT DISTINCT shot_zone_basic AS zone FROM shots WHERE game_id = :game_id
),
zone_results AS (
    SELECT
        team_id, shot_zone_basic AS zone, COUNT(*) AS attempts,
        SUM(shot_made_flag) AS makes,
        1.0 * SUM(shot_made_flag) / COUNT(*) AS fg_pct_raw
    FROM shots WHERE game_id = :game_id GROUP BY team_id, shot_zone_basic
),
pivoted AS (
    SELECT
        z.zone,
        MAX(CASE WHEN t.side = 'away' THEN t.team_tricode END) AS away_team_tricode,
        MAX(CASE WHEN t.side = 'away' THEN r.attempts END) AS away_attempts,
        MAX(CASE WHEN t.side = 'away' THEN r.makes END) AS away_makes,
        MAX(CASE WHEN t.side = 'away' THEN r.fg_pct_raw END) AS away_fg_pct_raw,
        MAX(CASE WHEN t.side = 'home' THEN t.team_tricode END) AS home_team_tricode,
        MAX(CASE WHEN t.side = 'home' THEN r.attempts END) AS home_attempts,
        MAX(CASE WHEN t.side = 'home' THEN r.makes END) AS home_makes,
        MAX(CASE WHEN t.side = 'home' THEN r.fg_pct_raw END) AS home_fg_pct_raw
    FROM zones AS z CROSS JOIN teams AS t
    LEFT JOIN zone_results AS r ON r.team_id = t.team_id AND r.zone = z.zone
    GROUP BY z.zone
)
SELECT
    *,
    away_fg_pct_raw - home_fg_pct_raw AS away_minus_home_fg_pct_raw,
    ABS(away_fg_pct_raw - home_fg_pct_raw) AS absolute_fg_pct_gap_raw,
    printf('%.1f pp', 100.0 * ABS(away_fg_pct_raw - home_fg_pct_raw))
        AS absolute_fg_pct_gap_display
FROM pivoted
WHERE away_fg_pct_raw IS NOT NULL AND home_fg_pct_raw IS NOT NULL
ORDER BY absolute_fg_pct_gap_raw DESC, zone
LIMIT 1;

-- query: primary_scoring_zones
-- Which database-defined zone generated the most made-shot points for each team?
-- Ties are retained.
WITH zone_points AS (
    SELECT
        t.team_tricode,
        s.shot_zone_basic AS zone,
        COUNT(*) AS attempts,
        SUM(s.shot_made_flag) AS makes,
        SUM(CASE s.shot_type
            WHEN '3PT Field Goal' THEN 3 * s.shot_made_flag
            WHEN '2PT Field Goal' THEN 2 * s.shot_made_flag
            ELSE 0
        END) AS points
    FROM shots AS s
    JOIN team_boxscores AS t
        ON t.game_id = s.game_id AND t.team_id = s.team_id
    WHERE s.game_id = :game_id
    GROUP BY t.team_tricode, s.shot_zone_basic
),
ranked AS (
    SELECT
        *,
        RANK() OVER (PARTITION BY team_tricode ORDER BY points DESC) AS rank
    FROM zone_points
)
SELECT team_tricode, zone, attempts, makes, points
FROM ranked
WHERE rank = 1
ORDER BY team_tricode, zone;
