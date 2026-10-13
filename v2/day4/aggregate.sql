-- Grain: one validated game/team row. Both opponents must be present.
-- Scores are already oriented to team_id, independent of home/away.
SELECT team_id, COUNT(*) AS games, SUM(win) AS wins,
       SUM(points) AS points, SUM(opponent_points) AS opponent_points,
       SUM(points-opponent_points) AS margin,
       SUM(fgm) AS fgm, SUM(fga) AS fga,
       SUM(fg3m) AS fg3m, SUM(fg3a) AS fg3a,
       SUM(ftm) AS ftm, SUM(fta) AS fta,
       SUM(oreb) AS oreb, SUM(dreb) AS dreb, SUM(reb) AS reb,
       SUM(ast) AS ast, SUM(tov) AS tov,
       SUM(team_tov) AS team_tov, SUM(team_reb) AS team_reb
FROM team_game
GROUP BY team_id ORDER BY team_id;
