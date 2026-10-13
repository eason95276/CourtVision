from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_player_role_evidence.py"
INJURY = ROOT / "data" / "pregame_snapshots" / "injury_import_0022500564.json"
spec = importlib.util.spec_from_file_location("player_role_evidence", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class PlayerRoleEvidenceTests(unittest.TestCase):
    def test_reason_classification_keeps_health_and_non_health_distinct(self):
        self.assertEqual(module.classify_reason("Injury/Illness - Right Hamstring; Strain")["primary"], "health_related")
        concussion = module.classify_reason("Concussion Protocol")
        self.assertEqual(concussion["primary"], "concussion_protocol")
        self.assertEqual(concussion["group"], "health_related")
        self.assertEqual(module.classify_reason("G League - Two-Way")["primary"], "g_league_two_way")
        self.assertEqual(module.classify_reason("Not With Team")["primary"], "not_with_team")
        self.assertNotIn("health_related", module.classify_reason("Not With Team")["tags"])

    def test_report_contains_all_ten_and_identity_resolution_is_exact(self):
        records = module.load_report_records(INJURY)
        ids = {
            "Jamaree Bouyea": 1631123,
            "Jordan Goodwin": 1630692,
            "Jalen Green": 1630224,
            "Myron Gardner": 1642066,
            "Vladislav Goldin": 1642884,
            "Tyler Herro": 1629639,
            "Pelle Larsson": 1641796,
            "Norman Powell": 1626181,
            "Terry Rozier": 1626179,
            "Jahmir Young": 1642443,
        }
        common = [
            {"DISPLAY_FIRST_LAST": name, "PERSON_ID": pid, "TEAM_ID": 0, "TEAM_ABBREVIATION": "", "ROSTERSTATUS": 0}
            for name, pid in ids.items()
        ]
        resolved = module.resolve_identity(records, common)
        self.assertEqual(len(resolved), 10)
        self.assertEqual({row["player_name"] for row in resolved}, set(ids))
        self.assertTrue(all(row["identity_match_status"] == "exact_unique" for row in resolved))

    def test_cutoff_excludes_target_and_post_cutoff_rows(self):
        rows = [
            {"SEASON_YEAR": "2025-26", "GAME_ID": "before", "GAME_DATE": "2026-01-12T00:00:00", "TEAM_ID": 1, "MIN": 10},
            {"SEASON_YEAR": "2025-26", "GAME_ID": "0022500564", "GAME_DATE": "2026-01-13T00:00:00", "TEAM_ID": 1, "MIN": 20},
            {"SEASON_YEAR": "2025-26", "GAME_ID": "after", "GAME_DATE": "2026-01-14T00:00:00", "TEAM_ID": 1, "MIN": 30},
        ]
        kept = module.cutoff_player_rows(rows, season="2025-26")
        self.assertEqual([row["GAME_ID"] for row in kept], ["before"])

    def test_team_stints_do_not_mix_traded_teams(self):
        rows = [
            {"SEASON_YEAR": "2024-25", "GAME_ID": "a1", "GAME_DATE": "2024-11-01", "TEAM_ID": 10, "TEAM_ABBREVIATION": "AAA", "TEAM_NAME": "A", "PLAYER_ID": 7, "MIN": 20, "PTS": 10, "FGA": 8, "AST": 2, "REB": 4},
            {"SEASON_YEAR": "2024-25", "GAME_ID": "b1", "GAME_DATE": "2025-02-01", "TEAM_ID": 20, "TEAM_ABBREVIATION": "BBB", "TEAM_NAME": "B", "PLAYER_ID": 7, "MIN": 30, "PTS": 20, "FGA": 12, "AST": 4, "REB": 6},
        ]
        result = module.aggregate_team_stints(rows, season="2024-25", starts={(7, 10): 1, (7, 20): 1}, starts_available=True)
        self.assertEqual([(row["team_id"], row["games_played"], row["starts"]) for row in result], [(10, 1, 1), (20, 1, 1)])
        self.assertEqual([row["points_per_appearance"] for row in result], [10.0, 20.0])

    def _team_games(self):
        games = []
        for index in range(10):
            game_date = date(2026, 1, 12 - index)
            game_id = f"g{10-index}"
            games.append({"Game_ID": game_id, "GAME_DATE": game_date.strftime("%b %d, %Y"), "MATCHUP": f"MIA vs. X{index}", "_parsed_date": game_date, "_game_id": game_id})
        return games

    def test_recent_window_uses_team_games_and_keeps_starter_source(self):
        team_games = self._team_games()
        current_rows = [
            {"SEASON_YEAR": "2025-26", "GAME_ID": "g8", "GAME_DATE": "2026-01-10T00:00:00", "TEAM_ID": 5, "MIN": 31.0, "MIN_SEC": "31:00", "PTS": 12, "FGA": 10, "AST": 3, "REB": 5}
        ]
        box = {
            "boxScoreTraditional": {
                "homeTeam": {"teamId": 5, "players": [{"personId": 7, "position": "G", "comment": "", "statistics": {"minutes": "31:00"}}]},
                "awayTeam": {"teamId": 99, "players": []},
            }
        }
        payloads = {game["_game_id"]: (box if game["_game_id"] == "g8" else {"boxScoreTraditional": {"homeTeam": {"teamId": 5, "players": []}, "awayTeam": {"teamId": 99, "players": []}}}) for game in team_games}
        statuses = {game["_game_id"]: "cached_no_request" for game in team_games}
        result = module.recent_window_for_player(
            player_id=7,
            report_team_id=5,
            team_window=team_games,
            all_team_games=team_games,
            current_player_rows=current_rows,
            boxscore_payloads=payloads,
            boxscore_status=statuses,
            current_logs_available=True,
            team_logs_available=True,
        )
        self.assertEqual(result["team_window_games_returned"], 10)
        self.assertEqual(result["appearance_count_in_recent_team_window"], 1)
        self.assertEqual(result["minutes_average_when_appeared"], 31.0)
        self.assertEqual(result["consecutive_report_team_games_without_appearance_record"], 2)
        self.assertEqual(result["games"][2]["starter_record"], True)
        self.assertEqual(result["games"][2]["starter_source_value"], "G")

    def test_no_player_row_is_unknown_cause_and_schedule_count_is_censored(self):
        team_games = self._team_games()[:3]
        result = module.recent_window_for_player(
            player_id=7,
            report_team_id=5,
            team_window=team_games,
            all_team_games=team_games,
            current_player_rows=[],
            boxscore_payloads={game["_game_id"]: {"boxScoreTraditional": {"homeTeam": {"teamId": 5, "players": []}, "awayTeam": {"teamId": 99, "players": []}}} for game in team_games},
            boxscore_status={game["_game_id"]: "success" for game in team_games},
            current_logs_available=True,
            team_logs_available=True,
        )
        self.assertEqual(result["appearance_count_in_recent_team_window"], 0)
        self.assertEqual(result["consecutive_report_team_games_without_appearance_record"], 3)
        self.assertTrue(result["consecutive_count_censored_by_available_schedule"])
        self.assertIn("absence cause unknown", result["games"][0]["missingness"])

    def test_write_json_once_protects_existing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            self.assertTrue(module.write_json_once(path, {"value": 1}))
            self.assertFalse(module.write_json_once(path, {"value": 2}))
            self.assertEqual(module.load_json(path), {"value": 1})

    def test_failed_raw_artifact_is_cached_as_failed_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.json"
            calls = {"count": 0}

            def fail_once():
                calls["count"] += 1
                raise RuntimeError("synthetic endpoint failure")

            payload, first = module.request_and_store(
                path=path,
                label="synthetic",
                endpoint="synthetic",
                parameters={"safe": True},
                call=fail_once,
            )
            self.assertIsNone(payload)
            self.assertEqual(first["status"], "failed")
            self.assertEqual(module.load_json(path)["status"], "failed")
            payload, second = module.request_and_store(
                path=path,
                label="synthetic",
                endpoint="synthetic",
                parameters={"safe": True},
                call=fail_once,
            )
            self.assertIsNone(payload)
            self.assertEqual(second["status"], "cached_failed")
            self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
