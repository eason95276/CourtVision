-- 问题：锁定配置在多个滚动赛季的评估表现是否稳定？只用全赛季切片。
SELECT
    evaluation_season,
    task,
    model,
    window_years,
    n_eval,
    ROUND(log_loss, 4) AS log_loss,
    ROUND(accuracy, 4) AS accuracy,
    ROUND(mae, 4) AS mae,
    ROUND(rmse, 4) AS rmse
FROM fact_model_stability
WHERE analysis = 'locked_selected_configuration'
ORDER BY task, evaluation_season;

