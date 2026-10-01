-- 问题：哪些比赛在胜负、分差或总分维度构成最高优先级 Bad Case？
WITH scored AS (
    SELECT
        g.game_id,
        g.game_date,
        ht.team_name AS home_team,
        at.team_name AS away_team,
        p.win_probability,
        p.predicted_margin,
        g.point_margin,
        p.predicted_total,
        g.total_points,
        e.win_log_loss,
        e.margin_absolute_error,
        e.total_absolute_error,
        RANK() OVER (ORDER BY e.margin_absolute_error DESC) AS margin_error_rank,
        RANK() OVER (ORDER BY e.total_absolute_error DESC) AS total_error_rank,
        RANK() OVER (ORDER BY e.win_log_loss DESC) AS win_error_rank
    FROM fact_prediction p
    JOIN fact_prediction_error e ON e.game_id = p.game_id
    JOIN dim_game g ON g.game_id = p.game_id
    JOIN dim_team ht ON ht.team_id = g.home_team_id
    JOIN dim_team at ON at.team_id = g.away_team_id
)
SELECT *
FROM scored
WHERE margin_error_rank <= 10 OR total_error_rank <= 10 OR win_error_rank <= 10
ORDER BY MIN(margin_error_rank, total_error_rank, win_error_rank), game_date
LIMIT 30;

