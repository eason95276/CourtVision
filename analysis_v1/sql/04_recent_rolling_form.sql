-- 问题：每支球队每场赛后最近 5 场与 10 场的滚动表现如何？窗口严格按比赛时间排序。
WITH ordered AS (
    SELECT
        f.*,
        ROW_NUMBER() OVER (
            PARTITION BY f.team_id ORDER BY f.game_date, f.game_id
        ) AS team_game_number
    FROM fact_team_game f
), rolling AS (
    SELECT
        team_id,
        game_id,
        game_date,
        season_start,
        team_game_number,
        AVG(win) OVER (
            PARTITION BY team_id ORDER BY game_date, game_id
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS win_rate_last5,
        AVG(point_margin) OVER (
            PARTITION BY team_id ORDER BY game_date, game_id
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS margin_last5,
        AVG(win) OVER (
            PARTITION BY team_id ORDER BY game_date, game_id
            ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
        ) AS win_rate_last10,
        AVG(point_margin) OVER (
            PARTITION BY team_id ORDER BY game_date, game_id
            ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
        ) AS margin_last10
    FROM ordered
), ranked AS (
    SELECT
        r.*,
        ROW_NUMBER() OVER (
            PARTITION BY r.team_id ORDER BY r.game_date DESC, r.game_id DESC
        ) AS recency_rank
    FROM rolling r
    WHERE r.team_game_number >= 10
)
SELECT
    t.team_name,
    r.game_date,
    r.game_id,
    ROUND(r.win_rate_last5, 3) AS win_rate_last5,
    ROUND(r.margin_last5, 2) AS margin_last5,
    ROUND(r.win_rate_last10, 3) AS win_rate_last10,
    ROUND(r.margin_last10, 2) AS margin_last10
FROM ranked r
JOIN dim_team t ON t.team_id = r.team_id
WHERE r.recency_rank = 1
ORDER BY margin_last10 DESC, t.team_name;

