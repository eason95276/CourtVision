-- 问题：模型置信度提高时，实际准确率是否同步提高？每桶报告样本量。
WITH bucketed AS (
    SELECT
        CASE
            WHEN ABS(win_probability - 0.5) < 0.10 THEN '0.50-0.60'
            WHEN ABS(win_probability - 0.5) < 0.20 THEN '0.60-0.70'
            WHEN ABS(win_probability - 0.5) < 0.30 THEN '0.70-0.80'
            ELSE '0.80-1.00'
        END AS confidence_band,
        CASE
            WHEN ABS(win_probability - 0.5) < 0.10 THEN 1
            WHEN ABS(win_probability - 0.5) < 0.20 THEN 2
            WHEN ABS(win_probability - 0.5) < 0.30 THEN 3
            ELSE 4
        END AS band_order,
        ABS(win_probability - 0.5) + 0.5 AS predicted_side_confidence,
        e.win_correct,
        e.win_log_loss
    FROM fact_prediction p
    JOIN fact_prediction_error e ON e.game_id = p.game_id
)
SELECT
    confidence_band,
    COUNT(*) AS games,
    ROUND(AVG(predicted_side_confidence), 4) AS mean_confidence,
    ROUND(AVG(win_correct), 4) AS actual_accuracy,
    ROUND(AVG(predicted_side_confidence) - AVG(win_correct), 4) AS confidence_gap,
    ROUND(AVG(win_log_loss), 4) AS mean_log_loss
FROM bucketed
GROUP BY confidence_band, band_order
ORDER BY band_order;

