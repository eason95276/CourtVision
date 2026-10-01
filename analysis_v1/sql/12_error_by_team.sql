-- 问题：冻结测试季中，哪些球队参与的比赛更容易出现大误差？每队至少 20 场。
WITH team_prediction_games AS (
    SELECT g.home_team_id AS team_id, e.*
    FROM dim_game g
    JOIN fact_prediction_error e ON e.game_id = g.game_id
    UNION ALL
    SELECT g.away_team_id AS team_id, e.*
    FROM dim_game g
    JOIN fact_prediction_error e ON e.game_id = g.game_id
)
SELECT
    t.team_name,
    COUNT(*) AS games,
    ROUND(AVG(x.win_log_loss), 4) AS mean_win_log_loss,
    ROUND(AVG(x.margin_absolute_error), 2) AS margin_mae,
    ROUND(AVG(x.total_absolute_error), 2) AS total_mae,
    ROUND(AVG(CASE WHEN x.margin_absolute_error >= 20 OR x.total_absolute_error >= 25 THEN 1.0 ELSE 0.0 END), 4) AS large_error_share
FROM team_prediction_games x
JOIN dim_team t ON t.team_id = x.team_id
GROUP BY t.team_name
HAVING COUNT(*) >= 20
ORDER BY large_error_share DESC, margin_mae DESC;

