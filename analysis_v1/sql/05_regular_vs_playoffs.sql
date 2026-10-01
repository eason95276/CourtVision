-- 问题：球队在常规赛与季后赛或附加赛的表现差异多大？季后赛样本至少 10 场。
WITH phase AS (
    SELECT
        team_id,
        is_playoffs,
        COUNT(*) AS games,
        AVG(win) AS win_rate,
        AVG(point_margin) AS avg_margin
    FROM fact_team_game
    GROUP BY team_id, is_playoffs
), eligible AS (
    SELECT team_id
    FROM phase
    WHERE is_playoffs = 1 AND games >= 10
)
SELECT
    t.team_name,
    MAX(CASE WHEN p.is_playoffs = 0 THEN p.games END) AS regular_games,
    ROUND(MAX(CASE WHEN p.is_playoffs = 0 THEN p.win_rate END), 4) AS regular_win_rate,
    MAX(CASE WHEN p.is_playoffs = 1 THEN p.games END) AS playoff_games,
    ROUND(MAX(CASE WHEN p.is_playoffs = 1 THEN p.win_rate END), 4) AS playoff_win_rate,
    ROUND(
        MAX(CASE WHEN p.is_playoffs = 1 THEN p.avg_margin END)
        - MAX(CASE WHEN p.is_playoffs = 0 THEN p.avg_margin END),
        2
    ) AS playoff_minus_regular_margin
FROM phase p
JOIN eligible e ON e.team_id = p.team_id
JOIN dim_team t ON t.team_id = p.team_id
GROUP BY t.team_name
ORDER BY playoff_minus_regular_margin DESC;

