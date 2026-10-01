-- 问题：各队长期场均得分、失分和净胜分如何？
SELECT
    t.team_name,
    COUNT(*) AS games,
    ROUND(AVG(f.points_for), 2) AS avg_points_for,
    ROUND(AVG(f.points_against), 2) AS avg_points_against,
    ROUND(AVG(f.point_margin), 2) AS avg_point_margin,
    ROUND(AVG(f.win), 4) AS win_rate
FROM fact_team_game f
JOIN dim_team t ON t.team_id = f.team_id
GROUP BY t.team_name
HAVING COUNT(*) >= 100
ORDER BY avg_point_margin DESC, games DESC;

