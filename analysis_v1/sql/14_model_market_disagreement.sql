-- 问题：模型与市场分歧最大的比赛有哪些？分歧仅用于复核，不代表投注价值。
WITH disagreement AS (
    SELECT
        g.game_id,
        g.game_date,
        ht.team_name AS home_team,
        at.team_name AS away_team,
        p.predicted_margin,
        m.market_expected_home_margin,
        ROUND(p.predicted_margin - m.market_expected_home_margin, 2) AS signed_margin_difference,
        p.predicted_total,
        m.market_total,
        ROUND(p.predicted_total - m.market_total, 2) AS signed_total_difference,
        m.model_market_divergence,
        RANK() OVER (ORDER BY m.model_market_divergence DESC) AS divergence_rank
    FROM fact_prediction p
    JOIN fact_market_benchmark m ON m.game_id = p.game_id
    JOIN dim_game g ON g.game_id = p.game_id
    JOIN dim_team ht ON ht.team_id = g.home_team_id
    JOIN dim_team at ON at.team_id = g.away_team_id
)
SELECT *
FROM disagreement
WHERE divergence_rank <= 20
ORDER BY divergence_rank, game_date;

