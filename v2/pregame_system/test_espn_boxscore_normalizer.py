from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from v2.pregame_system.espn_boxscore_normalizer import normalize_espn_summary_payload
from v2.pregame_system.espn_boxscore_normalizer import map_scoreboard_events_to_nba_games
from v2.pregame_system.player_knowledge import PlayerKnowledgeStore


def game():
    return {
        "game_id": "0022500001",
        "season": "2025-26",
        "season_type": "Regular Season",
        "game_date": "2025-10-21",
        "home_team_id": 1610612760,
        "away_team_id": 1610612745,
    }


def payload():
    labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]
    return {
        "header": {
            "id": "401809243",
            "competitions": [{"competitors": [
                {"team": {"id": "10"}, "homeAway": "away"},
                {"team": {"id": "25"}, "homeAway": "home"},
            ]}],
        },
        "boxscore": {"players": [
            {"team": {"id": "10", "abbreviation": "HOU"}, "statistics": [{"labels": labels, "athletes": [
                {"athlete": {"id": "3202", "displayName": "Player One", "position": {"abbreviation": "F"}, "jersey": "7"},
                 "starter": True, "didNotPlay": False, "reason": "", "stats": ["34", "23", "9-16", "0-4", "5-6", "9", "3", "4", "0", "0", "0", "9", "6", "+2"]},
                {"athlete": {"id": "1111", "displayName": "Bench Player", "position": {"abbreviation": "G"}, "jersey": "8"},
                 "starter": False, "didNotPlay": True, "reason": "COACH'S DECISION", "stats": ["DNP", "0", "0-0", "0-0", "0-0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]},
            ]}]},
            {"team": {"id": "25", "abbreviation": "OKC"}, "statistics": [{"labels": labels, "athletes": [
                {"athlete": {"id": "9999", "displayName": "Home Player", "position": {"abbreviation": "G"}, "jersey": "2"},
                 "starter": True, "didNotPlay": False, "reason": "", "stats": ["0", "0", "0-0", "0-0", "0-0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]},
            ]}]},
        ]},
    }


class ESPNBoxscoreNormalizerTests(unittest.TestCase):
    def test_normalizes_espn_ids_minutes_precision_and_dnp_without_imputation(self):
        rows = normalize_espn_summary_payload(
            game(), payload(), expected_event_id="401809243"
        )
        by_id = {row["player_id"]: row for row in rows}
        starter = by_id["3202"]
        self.assertEqual(starter["team_id"], 1610612745)
        self.assertEqual(starter["opponent_team_id"], 1610612760)
        self.assertEqual(starter["home_away"], "away")
        self.assertEqual(starter["starter"], 1)
        self.assertEqual(starter["minutes_raw"], "34")
        self.assertEqual(starter["minutes_seconds"], 2040)
        self.assertEqual(json.loads(starter["field_status_json"])["minutes_seconds"], "present_whole_minute_precision")
        self.assertIn("source_minutes_whole_minute_precision", json.loads(starter["normalization_warnings_json"]))
        self.assertEqual(starter["fgm"], 9)
        self.assertEqual(starter["fga"], 16)
        self.assertEqual(starter["assists"], 3)

        dnp = by_id["1111"]
        self.assertEqual(dnp["participation_status"], "did_not_play_confirmed")
        self.assertIsNone(dnp["minutes_seconds"])
        self.assertIsNone(dnp["points"])
        self.assertEqual(dnp["missing_status"], "not_applicable")

        zero = by_id["9999"]
        self.assertEqual(zero["participation_status"], "played")
        self.assertEqual(zero["minutes_seconds"], 0)
        self.assertEqual(zero["points"], 0)

    def test_rejects_game_id_or_team_identity_mismatch(self):
        wrong_event = payload()
        wrong_event["header"]["id"] = "different"
        with self.assertRaisesRegex(ValueError, "espn_event_id_mismatch"):
            normalize_espn_summary_payload(game(), wrong_event, {"10": 1610612745, "25": 1610612760},
                                           expected_event_id="401809243")

        wrong_teams = payload()
        wrong_teams["boxscore"]["players"][0]["team"]["id"] = "999"
        with self.assertRaisesRegex(ValueError, "espn_boxscore_team_identity_mismatch"):
            normalize_espn_summary_payload(game(), wrong_teams, {"10": 1610612745, "25": 1610612760},
                                           expected_event_id="401809243")

    def test_missing_player_id_dnp_is_preserved_without_inventing_identity(self):
        sample = payload()
        unknown_dnp = sample["boxscore"]["players"][0]["statistics"][0]["athletes"][1]
        unknown_dnp["athlete"].pop("id")
        unknown_dnp["athlete"].pop("displayName")
        unknown_dnp["athlete"]["shortName"] = "Olbrich"
        rows = normalize_espn_summary_payload(
            game(), sample, {"10": 1610612745, "25": 1610612760}, expected_event_id="401809243"
        )
        anonymous = next(row for row in rows if row["player_id"] is None)
        self.assertEqual(anonymous["player_name"], "Olbrich")
        self.assertEqual(anonymous["participation_status"], "did_not_play_confirmed")
        self.assertEqual(anonymous["missing_status"], "data_missing")
        self.assertIsNone(anonymous["points"])
        self.assertIn("player_id", json.loads(anonymous["missing_fields_json"]))
        self.assertIn("player_id_missing_identity_unresolved", json.loads(anonymous["normalization_warnings_json"]))

    def test_scoreboard_event_maps_by_ordered_team_names_and_requires_final(self):
        target = game()
        payload_value = {"events": [{
            "id": "401809243",
            "date": "2025-10-21T23:30:00Z",
            "status": {"type": {"description": "Final"}},
            "competitions": [{"competitors": [
                {"homeAway": "away", "team": {"name": "Rockets", "displayName": "Houston Rockets"}},
                {"homeAway": "home", "team": {"name": "Thunder", "displayName": "Oklahoma City Thunder"}},
            ]}],
        }]}
        mapping = map_scoreboard_events_to_nba_games(
            "2025-10-21", [target], payload_value,
            {"rockets": 1610612745, "thunder": 1610612760},
        )
        self.assertEqual(mapping["0022500001"]["event_id"], "401809243")
        payload_value["events"][0]["status"]["type"]["description"] = "Scheduled"
        with self.assertRaisesRegex(ValueError, "not_final"):
            map_scoreboard_events_to_nba_games(
                "2025-10-21", [target], payload_value,
                {"rockets": 1610612745, "thunder": 1610612760},
            )

    def test_provider_specific_coverage_and_active_source_do_not_mix_id_namespaces(self):
        target = game()
        rows = normalize_espn_summary_payload(
            target, payload(), {"10": 1610612745, "25": 1610612760}, expected_event_id="401809243"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            store.ingest_boxscore(
                target, payload(), normalized_rows=rows,
                collection_time_utc="2026-08-31T16:40:00Z",
                response_sha256="a" * 64,
                response_bytes=100,
                provider="ESPN",
                endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401809243",
                client_version="stdlib urllib ESPN summary probe",
                request_parameters={"game_id": target["game_id"], "event_id": "401809243"},
            )
            self.assertEqual(store.coverage(provider="NBA Stats")["collected_games"], 0)
            self.assertEqual(store.coverage(provider="ESPN")["collected_games"], 1)
            store.set_active_provider("ESPN")
            default_coverage = store.coverage()
            self.assertEqual(default_coverage["provider"], "ESPN")
            self.assertEqual(default_coverage["collected_games"], 1)
            summary = store.team_player_summaries(1610612745)
            player_profile = next(row for row in summary["profiles"] if row["player_id"] == "3202")
            self.assertEqual(player_profile["minutes_precision"]["status"], "whole_minute_resolution")
            self.assertIn("ESPN", player_profile["source_providers"])
            def team_roster(team_id, players):
                return {
                    "team_id": team_id,
                    "official_team_roster": {
                        "status": "available",
                        "players": players,
                        "source": {"captured_at": "2026-09-15T12:00:00Z", "source_type": "official_team_roster_page"},
                        "coverage": {"source_roster_completeness": "verified_complete"},
                    },
                    "cold_start": {"season_start": 2026},
                }
            roster = {"teams": {
                "home": team_roster(1610612760, [{"player_id": "nba-9999", "name": "Home Player"}]),
                "away": team_roster(1610612745, [
                    {"player_id": "nba-3202", "name": "Player One"},
                    {"player_id": "nba-1111", "name": "Bench Player"},
                ]),
            }}
            context = store.matchup_context(
                {"game_id": "espn:future", "season_start": 2026, "tipoff": "2026-10-20T19:00:00Z",
                 "home": {"id": 1610612760}, "away": {"id": 1610612745}},
                "2026-10-19T00:00:00Z", roster,
            )
            player = next(
                row for row in context["teams"]["away"]["previous_season_players"]
                if row["player_id"] == "nba-3202"
            )
            self.assertEqual(player["source_player_id"], "3202")
            self.assertEqual(player["source_player_id_namespace"], "ESPN")
            self.assertEqual(player["identity_match_method"], "unique_normalized_full_name_in_complete_league_dataset")
            self.assertEqual(player["previous_season_status"], "observed_on_current_team")
            self.assertEqual(context["coverage"]["provider"], "ESPN")

    def test_successful_resume_clears_failed_game_count_but_keeps_attempt_receipt(self):
        target = game()
        rows = normalize_espn_summary_payload(
            target, payload(), {"10": 1610612745, "25": 1610612760}, expected_event_id="401809243"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            store.record_failure(
                target["game_id"], error_type="invalid_json", error_message="probe failed",
                collection_time_utc="2026-08-30T00:00:00Z", provider="ESPN",
                endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401809243",
                client_version="probe",
            )
            store.ingest_boxscore(
                target, payload(), normalized_rows=rows,
                collection_time_utc="2026-08-31T16:40:00Z", response_sha256="d" * 64,
                provider="ESPN", endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401809243",
                client_version="probe",
            )
            coverage = store.coverage(provider="ESPN")
            self.assertEqual(coverage["collected_games"], 1)
            self.assertEqual(coverage["failed_games"], 0)
            self.assertEqual(coverage["failed_requests"], 1)
            self.assertEqual(coverage["not_collected_games"], 0)

    def test_ambiguous_same_name_provider_ids_are_not_crosswalked_and_keep_availability(self):
        def summary(event_id, home_espn_id, away_espn_id, home_id, away_id, home_player, away_player):
            labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]

            def line(player_id, name):
                return {
                    "athlete": {"id": player_id, "displayName": name, "position": {"abbreviation": "G"}, "jersey": "0"},
                    "starter": True, "didNotPlay": False, "reason": "",
                    "stats": ["20", "10", "4-9", "1-3", "1-1", "2", "3", "1", "0", "0", "0", "2", "1", "+1"],
                }

            return {
                "header": {"id": event_id, "competitions": [{"competitors": [
                    {"team": {"id": away_espn_id}, "homeAway": "away"},
                    {"team": {"id": home_espn_id}, "homeAway": "home"},
                ]}]},
                "boxscore": {"players": [
                    {"team": {"id": away_espn_id, "abbreviation": "AWY"},
                     "statistics": [{"labels": labels, "athletes": [line(*away_player)]}]},
                    {"team": {"id": home_espn_id, "abbreviation": "HOM"},
                     "statistics": [{"labels": labels, "athletes": [line(*home_player)]}]},
                ]},
            }

        first = game()
        first.update({"game_id": "0022500001", "game_date": "2025-10-21", "home_team_id": 2, "away_team_id": 1})
        second = game()
        second.update({"game_id": "0022500002", "game_date": "2025-10-22", "home_team_id": 4, "away_team_id": 3})
        first_payload = summary("event-1", "25", "10", 2, 1, ("200", "Home One"), ("100", "Alex Smith"))
        second_payload = summary("event-2", "26", "11", 4, 3, ("400", "Home Two"), ("300", "Alex Smith"))
        first_rows = normalize_espn_summary_payload(first, first_payload, {"10": 1, "25": 2})
        second_rows = normalize_espn_summary_payload(second, second_payload, {"11": 3, "26": 4})

        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([first, second])
            store.ingest_boxscore(
                first, first_payload, normalized_rows=first_rows,
                collection_time_utc="2026-08-31T16:40:00Z", response_sha256="b" * 64,
                provider="ESPN", endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/summary?event=event-1",
            )
            store.ingest_boxscore(
                second, second_payload, normalized_rows=second_rows,
                collection_time_utc="2026-08-31T16:41:00Z", response_sha256="c" * 64,
                provider="ESPN", endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/summary?event=event-2",
            )
            store.set_active_provider("ESPN")
            roster = {"teams": {
                "home": {"team_id": 2, "official_team_roster": {
                    "status": "available", "players": [{"player_id": "nba-200", "name": "Home One"}],
                    "source": {"captured_at": "2026-09-15T12:00:00Z"},
                    "coverage": {"source_roster_completeness": "verified_complete"},
                }},
                "away": {"team_id": 1, "official_team_roster": {
                    "status": "available", "players": [{"player_id": "nba-alex", "name": "Alex Smith"}],
                    "source": {"captured_at": "2026-09-15T12:00:00Z"},
                    "coverage": {"source_roster_completeness": "verified_complete"},
                }},
            }}
            availability = [{
                "player_id": "nba-alex", "team_id": 1, "player_name": "Alex Smith",
                "availability_status": "Questionable", "source_url": "https://example.test/injuries.pdf",
                "source_published_at": "2026-10-18T12:00:00Z", "captured_at": "2026-10-18T12:05:00Z",
            }]
            context = store.matchup_context(
                {"game_id": "future", "season_start": 2026, "tipoff": "2026-10-20T19:00:00Z",
                 "home": {"id": 2}, "away": {"id": 1}},
                "2026-10-19T00:00:00Z", roster, availability_records=availability,
            )
            alex = context["teams"]["away"]["previous_season_players"][0]
            self.assertEqual(alex["source_player_id"], None)
            self.assertEqual(alex["identity_resolution_status"], "ambiguous_provider_player_id_crosswalk")
            self.assertEqual(alex["previous_season_status"], "history_unavailable_due_to_identity_ambiguity")
            self.assertEqual(alex["current_availability"]["status"], "Questionable")
            self.assertEqual(context["teams"]["away"]["players_with_identity_crosswalk_unresolved"], 1)

    def test_complete_matchup_team_knowledge_remains_available_with_partial_espn_league_coverage(self):
        target = game()
        unrelated = {**target, "game_id": "0022500002", "game_date": "2025-10-22",
                     "home_team_id": 1610612742, "away_team_id": 1610612743}
        rows = normalize_espn_summary_payload(
            target, payload(), {"10": 1610612745, "25": 1610612760}, expected_event_id="401809243"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target, unrelated])
            store.ingest_boxscore(
                target, payload(), normalized_rows=rows,
                collection_time_utc="2026-08-31T16:40:00Z", response_sha256="e" * 64,
                provider="ESPN", endpoint="site.api.espn.com/nba/summary",
                source_url="https://site.api.espn.com/summary?event=401809243",
            )
            store.set_active_provider("ESPN")
            roster = {"teams": {
                "home": {"team_id": 1610612760, "official_team_roster": {
                    "status": "available", "players": [{"player_id": "nba-9999", "name": "Home Player",
                                                               "player_id_aliases": {"ESPN": "9999"}}],
                    "source": {"captured_at": "2026-09-15T12:00:00Z"},
                    "coverage": {"source_roster_completeness": "verified_complete"},
                }},
                "away": {"team_id": 1610612745, "official_team_roster": {
                    "status": "available", "players": [{"player_id": "nba-3202", "name": "Player One",
                                                               "player_id_aliases": {"ESPN": "3202"}}],
                    "source": {"captured_at": "2026-09-15T12:00:00Z"},
                    "coverage": {"source_roster_completeness": "verified_complete"},
                }},
            }}
            context = store.matchup_context(
                {"game_id": "future", "season_start": 2026, "tipoff": "2026-10-20T19:00:00Z",
                 "home": {"id": 1610612760}, "away": {"id": 1610612745}},
                "2026-10-19T00:00:00Z", roster,
            )
            home = context["teams"]["home"]
            home_player = home["previous_season_players"][0]
            self.assertEqual(context["coverage"]["status"], "partial")
            self.assertEqual(context["status"], "available")
            self.assertEqual(home["status"], "available")
            self.assertEqual(home_player["source_player_id"], "9999")
            self.assertEqual(home_player["current_team_last_season_stint"]["source_providers"], ["ESPN"])
            self.assertTrue(home["historical_team_leaders"])


if __name__ == "__main__":
    unittest.main()
