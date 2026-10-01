-- 粒度：冻结的 2025 至 2026 历史 holdout 测试集中，每个 game_id 恰好一行。
-- 市场字段仅代表缺少可靠采集时间、无法确认初盘或终盘的历史市场基准。
-- 方向命中率的分母只包含 status 为 hit 或 miss 的比赛。
-- market_unavailable、push、no_model_market_disagreement 均单独保留，不进入命中率分母。
WITH joined AS (
    SELECT
        g.game_id,
        g.game_date,
        g.game_type,
        g.is_playoffs,
        ht.team_name AS home_team,
        ht.team_abbr AS home_team_abbr,
        at.team_name AS away_team,
        at.team_abbr AS away_team_abbr,
        htg.pregame_back_to_back AS home_back_to_back,
        atg.pregame_back_to_back AS away_back_to_back,
        p.predicted_margin,
        m.market_expected_home_margin,
        g.point_margin,
        p.predicted_margin - m.market_expected_home_margin AS model_spread_gap,
        g.point_margin - m.market_expected_home_margin AS actual_spread_gap,
        p.predicted_total,
        m.market_total,
        g.total_points,
        p.predicted_total - m.market_total AS model_total_gap,
        g.total_points - m.market_total AS actual_total_gap
    FROM fact_prediction p
    JOIN dim_game g
      ON g.game_id = p.game_id
     AND p.evaluation_season = 2025
    JOIN dim_team ht ON ht.team_id = g.home_team_id
    JOIN dim_team at ON at.team_id = g.away_team_id
    JOIN fact_team_game htg
      ON htg.game_id = g.game_id
     AND htg.team_id = g.home_team_id
     AND htg.is_home = 1
    JOIN fact_team_game atg
      ON atg.game_id = g.game_id
     AND atg.team_id = g.away_team_id
     AND atg.is_home = 0
    LEFT JOIN fact_market_benchmark m ON m.game_id = p.game_id
),
directions AS (
    SELECT
        *,
        CASE
            WHEN market_expected_home_margin IS NULL THEN NULL
            WHEN ABS(model_spread_gap) <= 1e-9 THEN 'no_disagreement'
            WHEN model_spread_gap > 0 THEN 'home'
            ELSE 'away'
        END AS model_spread_direction,
        CASE
            WHEN market_expected_home_margin IS NULL THEN NULL
            WHEN ABS(actual_spread_gap) <= 1e-9 THEN 'push'
            WHEN actual_spread_gap > 0 THEN 'home'
            ELSE 'away'
        END AS actual_spread_direction,
        CASE
            WHEN market_total IS NULL THEN NULL
            WHEN ABS(model_total_gap) <= 1e-9 THEN 'no_disagreement'
            WHEN model_total_gap > 0 THEN 'over'
            ELSE 'under'
        END AS model_total_direction,
        CASE
            WHEN market_total IS NULL THEN NULL
            WHEN ABS(actual_total_gap) <= 1e-9 THEN 'push'
            WHEN actual_total_gap > 0 THEN 'over'
            ELSE 'under'
        END AS actual_total_direction
    FROM joined
),
graded AS (
    SELECT
        *,
        CASE
            WHEN market_expected_home_margin IS NULL THEN 'market_unavailable'
            WHEN model_spread_direction = 'no_disagreement' THEN 'no_model_market_disagreement'
            WHEN actual_spread_direction = 'push' THEN 'push'
            WHEN model_spread_direction = actual_spread_direction THEN 'hit'
            ELSE 'miss'
        END AS spread_status,
        CASE
            WHEN market_total IS NULL THEN 'market_unavailable'
            WHEN model_total_direction = 'no_disagreement' THEN 'no_model_market_disagreement'
            WHEN actual_total_direction = 'push' THEN 'push'
            WHEN model_total_direction = actual_total_direction THEN 'hit'
            ELSE 'miss'
        END AS total_status
    FROM directions
)
SELECT
    game_id,
    game_date,
    game_type,
    is_playoffs,
    home_team,
    home_team_abbr,
    away_team,
    away_team_abbr,
    home_back_to_back,
    away_back_to_back,
    predicted_margin,
    market_expected_home_margin,
    point_margin,
    model_spread_gap,
    actual_spread_gap,
    model_spread_direction,
    actual_spread_direction,
    spread_status,
    CASE WHEN spread_status = 'hit' THEN 1 WHEN spread_status = 'miss' THEN 0 ELSE NULL END AS spread_hit,
    predicted_total,
    market_total,
    total_points,
    model_total_gap,
    actual_total_gap,
    model_total_direction,
    actual_total_direction,
    total_status,
    CASE WHEN total_status = 'hit' THEN 1 WHEN total_status = 'miss' THEN 0 ELSE NULL END AS total_hit
FROM graded
ORDER BY game_date, game_id;
