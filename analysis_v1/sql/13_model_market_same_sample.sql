-- 问题：模型与市场在完全相同的 1,322 场样本中，分差和总分误差谁更低？
WITH same_sample AS (
    SELECT
        g.game_id,
        e.margin_absolute_error AS model_margin_error,
        ABS(g.point_margin - m.market_expected_home_margin) AS market_margin_error,
        e.total_absolute_error AS model_total_error,
        ABS(g.total_points - m.market_total) AS market_total_error
    FROM dim_game g
    JOIN fact_prediction_error e ON e.game_id = g.game_id
    JOIN fact_market_benchmark m ON m.game_id = g.game_id
    WHERE m.market_expected_home_margin IS NOT NULL
      AND m.market_total IS NOT NULL
)
SELECT
    COUNT(*) AS games,
    ROUND(AVG(model_margin_error), 4) AS model_margin_mae,
    ROUND(AVG(market_margin_error), 4) AS market_margin_mae,
    ROUND(AVG(model_margin_error - market_margin_error), 4) AS model_minus_market_margin_mae,
    ROUND(AVG(model_total_error), 4) AS model_total_mae,
    ROUND(AVG(market_total_error), 4) AS market_total_mae,
    ROUND(AVG(model_total_error - market_total_error), 4) AS model_minus_market_total_mae
FROM same_sample;

