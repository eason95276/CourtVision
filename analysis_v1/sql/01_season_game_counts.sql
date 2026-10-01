-- 问题：每个赛季有多少场已完成比赛，常规赛与季后赛如何分布？
WITH season_games AS (
    SELECT
        season_start,
        COUNT(*) AS games,
        SUM(CASE WHEN is_playoffs = 0 THEN 1 ELSE 0 END) AS regular_games,
        SUM(CASE WHEN is_playoffs = 1 THEN 1 ELSE 0 END) AS playoff_or_play_in_games
    FROM dim_game
    GROUP BY season_start
)
SELECT
    s.season_label,
    sg.games,
    sg.regular_games,
    sg.playoff_or_play_in_games,
    ROUND(1.0 * sg.playoff_or_play_in_games / sg.games, 4) AS playoff_share
FROM season_games sg
JOIN dim_season s ON s.season_start = sg.season_start
ORDER BY sg.season_start;

