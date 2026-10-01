-- 问题：总分预测误差的均值、分位近似分桶和大误差占比如何？
WITH ranked AS (
    SELECT
        total_absolute_error,
        NTILE(4) OVER (ORDER BY total_absolute_error) AS error_quartile
    FROM fact_prediction_error
)
SELECT
    error_quartile,
    COUNT(*) AS games,
    ROUND(MIN(total_absolute_error), 2) AS min_error,
    ROUND(AVG(total_absolute_error), 2) AS mean_error,
    ROUND(MAX(total_absolute_error), 2) AS max_error,
    ROUND(AVG(CASE WHEN total_absolute_error >= 25 THEN 1.0 ELSE 0.0 END), 4) AS share_error_25_plus
FROM ranked
GROUP BY error_quartile
ORDER BY error_quartile;
