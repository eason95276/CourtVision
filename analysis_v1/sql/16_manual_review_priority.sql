-- 问题：用透明规则生成赛后人工复核优先级，避免把单一误差阈值当成解释。
WITH evidence AS (
    SELECT
        g.game_id,
        g.game_date,
        ht.team_name AS home_team,
        at.team_name AS away_team,
        e.win_correct,
        e.margin_absolute_error,
        e.total_absolute_error,
        m.model_market_divergence,
        CASE WHEN e.win_correct = 0 THEN 2 ELSE 0 END
        + CASE WHEN e.margin_absolute_error >= 20 THEN 2 WHEN e.margin_absolute_error >= 12 THEN 1 ELSE 0 END
        + CASE WHEN e.total_absolute_error >= 25 THEN 2 WHEN e.total_absolute_error >= 15 THEN 1 ELSE 0 END
        + CASE WHEN m.model_market_divergence >= 12 THEN 1 ELSE 0 END AS review_score
    FROM fact_prediction_error e
    JOIN dim_game g ON g.game_id = e.game_id
    JOIN fact_market_benchmark m ON m.game_id = e.game_id
    JOIN dim_team ht ON ht.team_id = g.home_team_id
    JOIN dim_team at ON at.team_id = g.away_team_id
)
SELECT
    *,
    CASE
        WHEN review_score >= 6 THEN 'P0_manual_review'
        WHEN review_score >= 4 THEN 'P1_manual_review'
        ELSE 'P2_watchlist'
    END AS review_priority
FROM evidence
WHERE review_score >= 4
ORDER BY review_score DESC, margin_absolute_error DESC, total_absolute_error DESC
LIMIT 50;
