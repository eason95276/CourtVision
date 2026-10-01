-- 问题：各队主客场胜率是否存在稳定差异？样本量小于 20 的切片不展示。
WITH team_location AS (
    SELECT
        team_id,
        is_home,
        COUNT(*) AS games,
        AVG(win) AS win_rate
    FROM fact_team_game
    GROUP BY team_id, is_home
    HAVING COUNT(*) >= 20
)
SELECT
    t.team_name,
    MAX(CASE WHEN tl.is_home = 1 THEN tl.games END) AS home_games,
    ROUND(MAX(CASE WHEN tl.is_home = 1 THEN tl.win_rate END), 4) AS home_win_rate,
    MAX(CASE WHEN tl.is_home = 0 THEN tl.games END) AS away_games,
    ROUND(MAX(CASE WHEN tl.is_home = 0 THEN tl.win_rate END), 4) AS away_win_rate,
    ROUND(
        MAX(CASE WHEN tl.is_home = 1 THEN tl.win_rate END)
        - MAX(CASE WHEN tl.is_home = 0 THEN tl.win_rate END),
        4
    ) AS home_away_gap
FROM team_location tl
JOIN dim_team t ON t.team_id = tl.team_id
GROUP BY t.team_name
ORDER BY home_away_gap DESC, t.team_name;

