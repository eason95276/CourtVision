from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from v2.pregame_system.player_knowledge import (
    PlayerKnowledgeStore,
    normalize_boxscore_payload,
    read_games_csv,
)


def game(game_id="0022500001", date="2025-10-21", home=2, away=1):
    return {
        "game_id": game_id,
        "season": "2025-26",
        "season_type": "Regular Season",
        "game_date": date,
        "home_team_id": home,
        "away_team_id": away,
    }


def stat_line(**overrides):
    value = {
        "minutes": "12:30",
        "points": 0,
        "fieldGoalsMade": 0,
        "fieldGoalsAttempted": 0,
        "fieldGoalsPercentage": None,
        "threePointersMade": 0,
        "threePointersAttempted": 0,
        "threePointersPercentage": None,
        "freeThrowsMade": 0,
        "freeThrowsAttempted": 0,
        "freeThrowsPercentage": None,
        "reboundsOffensive": 0,
        "reboundsDefensive": 0,
        "reboundsTotal": 0,
        "assists": 0,
        "steals": 0,
        "blocks": 0,
        "turnovers": 0,
        "foulsPersonal": 0,
        "plusMinusPoints": 0,
    }
    value.update(overrides)
    return value


def player(pid, first, last, *, position="", comment="", minutes="12:30", stats=None):
    stat_values = {"minutes": minutes}
    stat_values.update(stats or {})
    return {
        "personId": pid,
        "firstName": first,
        "familyName": last,
        "nameI": f"{first[0]}. {last}",
        "position": position,
        "comment": comment,
        "jerseyNum": "0",
        "statistics": stat_line(**stat_values),
    }


def boxscore(game_row, away_players=None, home_players=None):
    return {
        "boxScoreTraditional": {
            "gameId": game_row["game_id"],
            "awayTeam": {"teamId": game_row["away_team_id"], "teamTricode": "AWY",
                         "players": list(away_players or [])},
            "homeTeam": {"teamId": game_row["home_team_id"], "teamTricode": "HOM",
                         "players": list(home_players or [])},
        }
    }


def roster_context(home_players=None, away_players=None):
    def team(team_id, players):
        return {
            "team_id": team_id,
            "official_team_roster": {
                "status": "available",
                "players": [
                    {"player_id": str(player_id), "name": name, "position": "G", "number": "0"}
                    for player_id, name in players
                ],
                "source": {"captured_at": "2026-09-15T12:00:00Z", "source_type": "official_team_roster_page"},
                "coverage": {"source_roster_completeness": "source_listed_not_independently_verified"},
            },
            "cold_start": {"season_start": 2026},
        }

    return {"teams": {
        "home": team(2, home_players or [(20, "Home Player")]),
        "away": team(1, away_players or [(10, "Away Player")]),
    }}


class PlayerKnowledgeTests(unittest.TestCase):
    def test_schedule_game_ids_are_padded_to_nba_stats_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Games.csv"
            path.write_text(
                "gameId,gameDateTimeEst,gameType,hometeamId,awayteamId\n"
                "22500001,2025-10-21 19:30:00,Regular Season,2,1\n"
                "22500002,2025-10-22 19:30:00,Playoffs,4,3\n",
                encoding="utf-8",
            )
            games = read_games_csv(path)
        self.assertEqual([row["game_id"] for row in games], ["0022500001"])

    def test_v3_normalizer_preserves_source_values_and_dnp_rows(self):
        target = game()
        payload = boxscore(
            target,
            away_players=[
                player(10, "Zero", "Line", position="G", stats={"minutes": "0:00"}),
                player(11, "Bench", "Guard", stats={"minutes": "20:00", "points": 8}),
                player(12, "Dnp", "Guard", comment="DNP - Coach's Decision", minutes="",
                       stats={"minutes": "", "fieldGoalsMade": 0, "fieldGoalsAttempted": 0}),
                player(13, "Unknown", "Guard", comment="", minutes="",
                       stats={"minutes": "", "fieldGoalsMade": 0, "fieldGoalsAttempted": 0}),
            ],
            home_players=[player(20, "Home", "Player", position="G")],
        )
        rows = normalize_boxscore_payload(target, payload)
        by_id = {row["player_id"]: row for row in rows}

        self.assertEqual(by_id["10"]["participation_status"], "played")
        self.assertEqual(by_id["10"]["minutes_seconds"], 0)
        self.assertEqual(by_id["10"]["points"], 0)
        self.assertEqual(by_id["10"]["starter"], 1)
        self.assertEqual(by_id["11"]["starter"], 0)
        self.assertEqual(by_id["12"]["participation_status"], "did_not_play_confirmed")
        self.assertIsNone(by_id["12"]["points"])
        self.assertEqual(by_id["12"]["missing_status"], "not_applicable")
        self.assertEqual(by_id["13"]["participation_status"], "unknown")
        self.assertIsNone(by_id["13"]["points"])
        self.assertEqual(by_id["13"]["missing_status"], "unknown")

    def test_import_is_idempotent_and_keeps_zero_distinct_from_missing(self):
        target = game()
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            payload = boxscore(
                target,
                away_players=[player(10, "Zero", "Line", position="G", stats={"minutes": "0:00"})],
                home_players=[player(20, "Home", "Player", position="G", stats={"points": 5})],
            )
            first = store.ingest_boxscore(target, payload, collection_time_utc="2026-08-31T16:40:00Z")
            second = store.ingest_boxscore(target, payload, collection_time_utc="2026-08-31T16:40:00Z")

            self.assertEqual(first["status"], "success")
            self.assertEqual(second["status"], "cached_success")
            coverage = store.coverage("2025-26", "2026-10-19T00:00:00Z")
            self.assertEqual(coverage["status"], "complete")
            self.assertEqual(coverage["collected_games"], 1)
            summaries = store.team_player_summaries(1, "2025-26", "2026-10-19T00:00:00Z")
            zero_player = next(row for row in summaries["profiles"] if row["player_id"] == "10")
            self.assertEqual(zero_player["statistics"]["points"]["total"], 0)
            self.assertEqual(zero_player["minutes_total_seconds"], 0)
            historical = store.coverage("2025-26", "2026-01-14T00:00:00Z")
            self.assertEqual(historical["collected_games"], 0)
            historical_summary = store.team_player_summaries(1, "2025-26", "2026-01-14T00:00:00Z")
            self.assertEqual(historical_summary["profiles"], [])

    def test_complete_matchup_teams_are_available_when_unrelated_league_games_are_missing(self):
        games = [
            game("0022500001", "2025-10-21"),
            game("0022500002", "2025-10-22", home=4, away=3),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize(games)
            store.ingest_boxscore(
                games[0],
                boxscore(games[0], [player(10, "Away", "Player", position="G")],
                         [player(20, "Home", "Player", position="G")]),
                collection_time_utc="2026-08-31T16:40:00Z",
            )
            context = store.matchup_context(
                {"game_id": "future", "season_start": 2026, "tipoff": "2026-10-20T19:00Z",
                 "home": {"id": 2}, "away": {"id": 1}},
                "2026-10-19T00:00:00Z",
                roster_context(home_players=[(20, "Home Player"), (30, "Rookie Player")]),
            )
            self.assertEqual(context["coverage"]["status"], "partial")
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["matchup_coverage"]["status"], "complete")
            self.assertEqual(context["teams"]["home"]["status"], "available")
            self.assertEqual(context["teams"]["away"]["status"], "available")
            rookie = next(
                row for row in context["teams"]["home"]["previous_season_players"]
                if row["player_id"] == "30"
            )
            self.assertEqual(rookie["previous_season_status"], "history_unavailable_due_to_league_coverage")
            self.assertEqual(rookie["previous_season_history_status"], "unavailable")
            self.assertEqual(rookie["previous_season_history_reason"], "not_observed_in_partial_previous_season_dataset")
            self.assertEqual(context["teams"]["home"]["players_without_previous_season_record"], 0)
            self.assertEqual(context["teams"]["home"]["players_with_history_unavailable_due_to_coverage"], 1)

    def test_no_previous_season_record_state_and_summary_counts_are_consistent(self):
        target = game()
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            store.ingest_boxscore(
                target,
                boxscore(target,
                         [player(10, "Away", "Player", position="G", stats={"points": 8})],
                         [player(20, "Home", "Player", position="G", stats={"points": 12})]),
                collection_time_utc="2026-08-31T16:40:00Z",
            )
            context = store.matchup_context(
                {"game_id": "future", "season_start": 2026, "tipoff": "2026-10-20T19:00:00Z",
                 "home": {"id": 2}, "away": {"id": 1}},
                "2026-10-19T00:00:00Z",
                roster_context(home_players=[(20, "Home Player"), (30, "Rookie Player")]),
            )

            home = context["teams"]["home"]
            rookie = next(row for row in home["previous_season_players"] if row["player_id"] == "30")
            self.assertEqual(rookie["previous_season_status"], "no_previous_season_record")
            self.assertEqual(rookie["previous_season_history_status"], "unavailable")
            self.assertEqual(rookie["previous_season_history_reason"], "not_found_in_complete_previous_season_dataset")
            self.assertEqual(rookie["previous_season_stats_status"], "unavailable")
            self.assertIsNone(rookie["previous_season_total"])
            self.assertEqual(home["players_with_previous_season_stats"], 1)
            self.assertEqual(home["players_without_previous_season_record"], 1)
            self.assertEqual(
                [row["player_id"] for row in home["unmatched_current_roster_players"]], ["30"]
            )

    def test_new_player_availability_survives_without_history_into_analyst_context(self):
        from v2.frontend.analysis_service import SavedGameAnalysisService

        target = game()
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            store.ingest_boxscore(
                target,
                boxscore(target,
                         [player(10, "Away", "Player", position="G", stats={"points": 8})],
                         [player(20, "Home", "Player", position="G", stats={"points": 12})]),
                collection_time_utc="2026-08-31T16:40:00Z",
            )

            class FakeJudgment:
                def model_dump(self):
                    return {}

            class FakeAnalyst:
                def __init__(self):
                    self.context = None

                def judge(self, context):
                    self.context = context
                    return FakeJudgment(), {"path": None}

            published = "2026-10-18T12:00:00Z"
            captured = "2026-10-18T12:03:00Z"
            availability = {
                "status": "available",
                "source_url": "https://example.test/injury-report.pdf",
                "source_published_at": published,
                "captured_at": captured,
                "cutoff_time_utc": "2026-10-19T00:00:00Z",
                "records": [{
                    "player_id": "30",
                    "team_id": 2,
                    "player_name": "Rookie Player",
                    "availability_status": "Questionable",
                    "reason": "ankle",
                }],
            }
            game_row = {
                "id": "espn:new-player",
                "status": "scheduled",
                "tipoff": "2026-10-20T19:00:00Z",
                "home": {"id": 2, "name": "Home", "name_zh": "主队"},
                "away": {"id": 1, "name": "Away", "name_zh": "客队"},
                "relationship": {
                    "basketball": {"status": "available", "prediction": {"win_probability": 0.6}},
                    "basketball_provenance": {"market_independent": True},
                    "market": None,
                    "divergence": None,
                    "residual": None,
                    "final": {"win_probability": 0.6, "expected_margin": 2.0, "expected_total": 220.0},
                },
                "background": {"availability": availability, "schedule": {}},
                "roster_context": roster_context(
                    home_players=[(20, "Home Player"), (30, "Rookie Player")]
                ),
                "matchup_history": {"status": "unavailable"},
                "season_form": {"status": "unavailable"},
                "season_phase": {"phase": "opening_night", "season_start": 2026},
                "missing": [],
            }
            analyst = FakeAnalyst()
            service = SavedGameAnalysisService(
                {"games": [game_row]}, analyst=analyst, player_knowledge_store=store
            )
            result = service.analyze(
                "espn:new-player", allow_lookup=False, cutoff="2026-10-19T00:00:00Z"
            )

            roster_evidence = next(
                item for item in analyst.context.objective_evidence
                if item["section"] == "roster_context"
            )
            player_context = roster_evidence["value"]["player_knowledge"]
            home = player_context["teams"]["home"]
            rookie = next(row for row in home["previous_season_players"] if row["player_id"] == "30")
            self.assertEqual(player_context["coverage"]["status"], "complete")
            self.assertEqual(rookie["previous_season_history_status"], "unavailable")
            self.assertEqual(rookie["current_availability"]["status"], "Questionable")
            self.assertEqual(rookie["current_availability"]["source_url"], availability["source_url"])
            self.assertEqual(rookie["current_availability"]["captured_at"], "2026-10-18T12:03:00.000000Z")
            self.assertEqual(rookie["current_availability"]["analysis_cutoff"], "2026-10-19T00:00:00.000000Z")
            self.assertEqual(rookie["current_availability"]["time_eligibility"], "eligible_as_of_cutoff")
            unmatched = home["unmatched_current_roster_players"]
            self.assertEqual(unmatched[0]["current_availability"]["status"], "Questionable")
            self.assertEqual(result["official_numeric_prediction"], game_row["relationship"]["final"])

    def test_player_team_history_preserves_traded_stints_by_player_id(self):
        first = game("0022500001", "2025-10-21", home=2, away=1)
        second = game("0022500002", "2025-10-22", home=4, away=3)
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([first, second])
            store.ingest_boxscore(
                first,
                boxscore(first, [player(10, "Same", "Name", position="G")],
                         [player(20, "Home", "Player", position="G")]),
                collection_time_utc="2026-08-31T16:40:00Z",
            )
            store.ingest_boxscore(
                second,
                boxscore(second, [player(10, "Same", "Name", position="G")],
                         [player(40, "Other", "Player", position="G")]),
                collection_time_utc="2026-08-31T16:41:00Z",
            )
            history = store.player_team_history("10", "2025-26", "2026-10-19T00:00:00Z")
            self.assertEqual({row["team_id"] for row in history["stints"]}, {1, 3})
            self.assertTrue(all("transaction effective dates" in row["history_semantics"]
                                for row in history["stints"]))

    def test_same_name_players_remain_separate_by_player_id(self):
        target = game()
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            payload = boxscore(
                target,
                away_players=[
                    player(10, "Alex", "Smith", position="G", stats={"minutes": "20:00", "points": 5}),
                    player(11, "Alex", "Smith", position="", stats={"minutes": "10:00", "points": 2}),
                ],
                home_players=[player(20, "Home", "Player", position="G")],
            )
            store.ingest_boxscore(target, payload, collection_time_utc="2026-08-31T16:40:00Z")
            summary = store.team_player_summaries(1, "2025-26", "2026-10-19T00:00:00Z")
            matching = [row for row in summary["profiles"] if row["player_name"] == "Alex Smith"]
            self.assertEqual({row["player_id"] for row in matching}, {"10", "11"})

    def test_replacement_evidence_uses_confirmed_boxscore_dnp_and_current_roster_ids(self):
        first = game("0022500001", "2025-10-21")
        second = game("0022500002", "2025-10-22")
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([first, second])
            store.ingest_boxscore(
                first,
                boxscore(first,
                         [player(10, "Key", "Player", position="G", minutes="30:00", stats={"points": 22}),
                          player(11, "Backup", "Guard", minutes="10:00", stats={"points": 5})],
                         [player(20, "Home", "Player", position="G")]),
                collection_time_utc="2026-08-31T16:40:00Z",
            )
            store.ingest_boxscore(
                second,
                boxscore(second,
                         [player(10, "Key", "Player", comment="DNP - Injury/Illness", minutes="",
                                 stats={"minutes": "", "points": 0}),
                          player(11, "Backup", "Guard", minutes="25:00", stats={"points": 15})],
                         [player(20, "Home", "Player", position="G")]),
                collection_time_utc="2026-08-31T16:41:00Z",
            )
            context = store.matchup_context(
                {"game_id": "future", "season_start": 2026, "tipoff": "2026-10-20T19:00Z",
                 "home": {"id": 2}, "away": {"id": 1}},
                "2026-10-19T00:00:00Z",
                roster_context(away_players=[(10, "Key Player"), (11, "Backup Guard")]),
            )
            key_player = next(
                row for row in context["teams"]["away"]["previous_season_players"]
                if row["player_id"] == "10"
            )
            candidate = key_player["replacement_observations"][0]
            self.assertEqual(candidate["player_id"], "11")
            self.assertEqual(candidate["games_with_key_player_dnp"], 1)
            self.assertEqual(candidate["minutes_difference"], 15.0)
            self.assertIn("descriptive historical association", candidate["interpretation"])
            self.assertEqual(key_player["last_season_confirmed_dnp_games"], 1)
            self.assertTrue(any(
                item["measure"] == "team_minutes_volume"
                for item in key_player["factual_role_evidence"]
            ))
            self.assertFalse(any("联盟顶级" in item.get("statement", "")
                                 for item in key_player["factual_role_evidence"]))
            self.assertEqual(context["teams"]["away"]["continuity"]["status"], "provisional_roster_membership")

    def test_complete_two_team_context_matches_roster_ids_and_keeps_provenance(self):
        target = game()
        with tempfile.TemporaryDirectory() as tmp:
            store = PlayerKnowledgeStore(Path(tmp) / "player_knowledge.sqlite")
            store.initialize([target])
            payload = boxscore(
                target,
                away_players=[player(10, "Away", "Player", position="G", stats={"points": 12})],
                home_players=[player(20, "Home", "Player", position="G", stats={"points": 5})],
            )
            store.ingest_boxscore(target, payload, collection_time_utc="2026-08-31T16:40:00Z")
            context = store.matchup_context(
                {"game_id": "espn:future", "season_start": 2026, "tipoff": "2026-10-20T19:00Z",
                 "home": {"id": 2}, "away": {"id": 1}},
                "2026-10-19T00:00:00Z",
                roster_context(),
            )

            self.assertEqual(context["status"], "available")
            home_player = context["teams"]["home"]["previous_season_players"][0]
            away_player = context["teams"]["away"]["previous_season_players"][0]
            self.assertEqual(home_player["player_id"], "20")
            self.assertEqual(away_player["player_id"], "10")
            self.assertEqual(home_player["current_team_last_season_stint"]["statistics"]["points"]["total"], 5)
            self.assertEqual(context["coverage"]["collected_games"], 1)

    def test_missing_database_read_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.sqlite"
            store = PlayerKnowledgeStore(path)
            self.assertEqual(store.coverage()["status"], "unavailable")
            self.assertFalse(path.exists())

    def test_player_knowledge_survives_analyst_roster_compaction(self):
        from v2.pregame_system.analyst import _compact_roster_context, build_analyst_context

        knowledge = {"status": "available", "season": "2025-26", "teams": {"home": {"players": []}}}
        compact = _compact_roster_context({"teams": {}, "player_knowledge": knowledge})
        self.assertEqual(compact["player_knowledge"], knowledge)
        context = build_analyst_context(
            {
                "pure_prediction": {"status": "unavailable"},
                "market_benchmark": None,
                "divergence": None,
                "residual_prediction": None,
                "official_numeric_prediction": {"status": "unavailable"},
            },
            [{"id": "roster-evidence", "kind": "Fact", "section": "roster_context",
              "value": {"teams": {}, "player_knowledge": knowledge}}],
            missing_data=[],
        )
        self.assertEqual(context.roster_context["player_knowledge"], knowledge)

    def test_default_analysis_context_attachment_does_not_change_numeric_payload(self):
        from v2.frontend.analysis_service import SavedGameAnalysisService

        knowledge = {"status": "partial", "reason": "season_coverage_incomplete", "teams": {}}

        class FakeStore:
            def matchup_context(self, game, cutoff, roster, *, availability_records):
                self.assert_cutoff = cutoff
                self.assert_roster = roster
                return knowledge

        numeric = {
            "basketball": {"status": "available", "prediction": {"home_win_probability": 0.6}},
            "final": {"win_probability": 0.6, "expected_margin": 2.0, "expected_total": 220.0},
        }
        game_row = {"relationship": numeric, "roster_context": {"teams": {}}}
        service = SavedGameAnalysisService({}, analyst=object(), player_knowledge_store=FakeStore())
        service._attach_player_knowledge(game_row, "2026-10-19T00:00:00Z")

        self.assertEqual(game_row["relationship"], numeric)
        self.assertEqual(game_row["roster_context"]["player_knowledge"], knowledge)

    def test_saved_analysis_service_sends_matchup_player_context_to_analyst(self):
        from v2.frontend.analysis_service import SavedGameAnalysisService

        knowledge = {
            "status": "available",
            "season": "2025-26",
            "teams": {"home": {"players": [{"player_id": "20", "games_played": 70}]},
                      "away": {"players": [{"player_id": "10", "games_played": 60}]}},
        }

        class FakeStore:
            def matchup_context(self, game, cutoff, roster, *, availability_records):
                return knowledge

        class FakeJudgment:
            def model_dump(self):
                return {}

        class FakeAnalyst:
            def __init__(self):
                self.context = None

            def judge(self, context):
                self.context = context
                return FakeJudgment(), {"path": None}

        game_row = {
            "id": "espn:test",
            "status": "scheduled",
            "tipoff": "2026-10-20T19:00:00Z",
            "home": {"id": 2, "name": "Home", "name_zh": "主队"},
            "away": {"id": 1, "name": "Away", "name_zh": "客队"},
            "relationship": {
                "basketball": {"status": "available", "prediction": {"win_probability": 0.6}},
                "basketball_provenance": {"market_independent": True},
                "market": None,
                "divergence": None,
                "residual": None,
                "final": {"win_probability": 0.6, "expected_margin": 2.0, "expected_total": 220.0},
            },
            "background": {"availability": {"status": "unavailable"}, "schedule": {}},
            "roster_context": {"teams": {}},
            "matchup_history": {"status": "unavailable"},
            "season_form": {"status": "unavailable"},
            "season_phase": {"phase": "opening_night", "season_start": 2026},
            "missing": [],
        }
        analyst = FakeAnalyst()
        service = SavedGameAnalysisService(
            {"games": [game_row]}, analyst=analyst, player_knowledge_store=FakeStore()
        )
        result = service.analyze("espn:test", allow_lookup=False, cutoff="2026-10-02T00:00:00Z")

        self.assertEqual(result["official_numeric_prediction"], game_row["relationship"]["final"])
        roster_evidence = next(
            item for item in analyst.context.objective_evidence if item["section"] == "roster_context"
        )
        self.assertEqual(roster_evidence["value"]["player_knowledge"], knowledge)
        self.assertNotIn("player_knowledge", game_row["roster_context"])


if __name__ == "__main__":
    unittest.main()
