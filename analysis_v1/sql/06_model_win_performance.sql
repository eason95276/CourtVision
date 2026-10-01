-- 问题：胜负模型在冻结测试季的准确率、对数损失和概率偏差如何？
SELECT
    p.evaluation_season,
    COUNT(*) AS games,
    ROUND(AVG(e.win_correct), 4) AS accuracy,
    ROUND(AVG(e.win_log_loss), 4) AS log_loss,
    ROUND(AVG(p.win_probability), 4) AS mean_predicted_home_win_probability,
    ROUND(AVG(g.home_win), 4) AS actual_home_win_rate,
    ROUND(AVG(p.win_probability) - AVG(g.home_win), 4) AS probability_bias
FROM fact_prediction p
JOIN fact_prediction_error e ON e.game_id = p.game_id
JOIN dim_game g ON g.game_id = p.game_id
GROUP BY p.evaluation_season;

