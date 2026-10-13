from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from v2.pregame_system.analyst import AnalystJudgment
from v2.pregame_system.editorial_test_helpers import seal_fixture_payload
from v2.pregame_system.prediction_versions import PredictionVersionStore, payload_sha256

from .analysis_service import (
    SavedGameAnalysisService,
    analysis_base_fingerprint,
    historical_game_context,
    _evidence,
    cached_public_market_context,
    research_local_context,
    search_categories,
    game_identity,
)
from v2.pregame_system.web_search import select_public_market_reference
from .export_data import (
    STATIC_ROOT,
    build_app_data,
    export_app_data,
    normalize_game,
    normalize_market,
    select_key_factors,
)
from .server import (
    build_ai_response,
    _with_display_projection,
    build_market_response,
    build_prediction_versions_response,
    load_local_env,
)
from .prediction_version_service import PredictionVersionService


class VersionClock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, 19, 42, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def publish_test_version(store, clock, game, request_id):
    request = store.begin_request(game, request_id=request_id)
    identity = request["game_identity"]
    clock.value += timedelta(seconds=3)
    final = {
        "winner_side": "home",
        "win_probability": 0.57,
        "expected_margin": 1.5,
        "expected_total": 228.5,
        "evidence_ids": ["prediction"],
    }
    payload = {
        "game_id": game["id"],
        "game_identity": identity,
        "request_id": request_id,
        "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"],
        "input_frozen_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=1)).isoformat(),
        "generated_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=2)).isoformat(),
        "input_snapshot": copy.deepcopy(game),
        "analyst_context": {"context_hash": "fixture-context"},
        "research": {"records": [{"id": "prediction", "snippet": "source"}]},
        "model_layers": {"official_numeric_prediction": game["relationship"]["final"]},
        "judgment": {
            "analysis_article": "Version article",
            "final_prediction": final,
            "evidence_index": {"prediction": {"section": "prediction"}},
        },
        "evidence_index": {"prediction": {"section": "prediction"}},
        "sources": [],
        "cited_sources": [],
        "provenance": {"model": "fixture"},
    }
    payload["prediction"] = payload["judgment"]["final_prediction"]
    return store.publish(game, request, seal_fixture_payload(payload))


def test_real_saved_predictions_are_normalized_without_demo_market_fixture():
    data = build_app_data()
    assert data["coverage"]["schedule_games"] == 56
    assert data["coverage"]["games_with_saved_prediction"] >= 8
    assert data["source"]["synthetic_market_included"] is False
    assert data["games"]
    for game in data["games"]:
        assert game["id"].startswith("espn:")
        assert game["prediction"]["available"]
        assert 0.5 <= game["prediction"]["winner_probability"] <= 1
        assert 0 <= len(game["key_factors"]) <= 3
        assert game["technical"]["source_label"] == "开发期赛前预测快照"
        assert game["source"]["report_path"].startswith("v2/pregame_system/outputs/agent_runs/")
        assert len(game["source"]["report_sha256"]) == 64
        assert not ((game["relationship"].get("market") or {}).get("demo_fixture"))


def test_cutoff_display_projection_keeps_frozen_cutoffs_separate_and_hash_stable():
    version = {
        "version_id": "version-cutoffs",
        "input_cutoff_at": "2026-10-05T17:50:51.900669+00:00",
        "input_snapshot": {
            "technical": {
                "cutoff": "2026-10-05T15:00:00+00:00",
                "fact_context_cutoff": "2026-10-05T16:30:00+00:00",
                "roster_context_cutoff": "2026-10-05T16:40:00+00:00",
            },
            "fact_context": {"feature_cutoff": "2026-10-05T16:31:57.990639+00:00"},
            "roster_context": {"context_cutoff": "2026-10-05T16:41:12.000000+00:00"},
        },
    }
    before_hash = payload_sha256(version)

    response = _with_display_projection({"status": "success", "version": version,
                                         "version_id": version["version_id"]})

    assert response["display_projection"] == {
        "schema_version": "prediction-display-cutoffs-v1",
        "version_id": "version-cutoffs",
        "cutoffs": {
            "input_cutoff_at": "2026-10-05T17:50:51.900669+00:00",
            "feature_cutoff_at": "2026-10-05T16:31:57.990639+00:00",
            "roster_context_cutoff_at": "2026-10-05T16:41:12.000000+00:00",
        },
    }
    assert payload_sha256(response["version"]) == before_hash
    assert "display_projection" not in version


def test_cutoff_display_projection_does_not_backfill_missing_legacy_fields():
    version = {
        "version_id": "legacy-version",
        "input_cutoff_at": "2026-10-05T17:00:00+00:00",
        "input_snapshot": {"technical": {"cutoff": "2026-10-05T16:00:00+00:00"}},
    }

    projection = _with_display_projection({"version": version})["display_projection"]

    assert projection["cutoffs"] == {
        "input_cutoff_at": "2026-10-05T17:00:00+00:00",
        "feature_cutoff_at": None,
        "roster_context_cutoff_at": None,
    }


def test_missing_backend_fields_have_explicit_states():
    data = build_app_data()
    featured = next(game for game in data["games"] if game["id"] == "espn:401909088")
    assert featured["relationship"]["market"] is None
    assert "没有" in featured["relationship"]["market_message"]
    assert featured["background"]["availability"]["status"] == "unavailable"
    assert featured["missing"]


def test_versioned_demo_contains_matchup_history_and_opening_phase():
    data = build_app_data()
    philly = next(game for game in data["games"] if game["id"] == "espn:401909089")
    assert philly["season_phase"]["phase"] == "opening_night"
    assert philly["matchup_history"]["matchup_relevance"] == "high"
    series = philly["matchup_history"]["series_summary"]
    assert series["games"] == 4
    assert series["wins"] == {"1610612752": 4, "1610612755": 0}
    assert series["team_summaries"]["1610612752"]["average_margin"] == 22.25
    assert series["team_stats_status"] == "complete"
    assert philly["season_form"]["status"] == "available"
    assert philly["season_form"]["teams"]["home"]["current_last10"]["games"] == 0
    assert philly["season_form"]["teams"]["home"]["previous_last10"]["games"] == 10
    ordinary = next(game for game in data["games"] if game["id"] == "espn:401909088")
    assert ordinary["matchup_history"]["matchup_relevance"] == "medium"


def test_research_planner_sees_local_series_and_current_roster_without_all_old_games():
    game = next(item for item in build_app_data()["games"] if item["id"] == "espn:401909089")
    context = research_local_context(game)
    assert context["matchup_history"]["series_summary"]["wins"] == {
        "1610612752": 4, "1610612755": 0
    }
    assert "meetings" not in context["matchup_history"]
    assert "LeBron James" in context["official_roster_names"]["away"]
    assert context["season_phase"]["phase"] == "opening_night"


def test_detail_markup_has_matchup_history_region():
    markup = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="matchup-history-section"' in markup
    assert 'id="matchup-history"' in markup
    assert "比赛开始前" not in markup
    assert "直接交锋" not in markup
    assert '<h2 id="matchup-history-title">历史交锋</h2>' in markup
    assert 'id="prediction-relationship"' not in markup
    assert 'id="roster-context"' not in markup
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "renderRelationship" not in script
    assert "renderRoster" not in script


def test_export_is_valid_json(tmp_path):
    path = export_app_data(tmp_path / "app_data.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2.0"
    assert payload["games"][0]["home"]["logo_url"].startswith("https://a.espncdn.com/")


def test_local_env_loader_does_not_override_existing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TAVILY_API_KEY=from-file\nDEEPSEEK_API_KEY=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")
    load_local_env(env_file)
    assert __import__("os").environ["TAVILY_API_KEY"] == "from-file"
    assert __import__("os").environ["DEEPSEEK_API_KEY"] == "from-shell"


def test_current_demo_team_logos_are_local_runtime_assets():
    data = build_app_data()
    abbreviations = {
        team["abbreviation"].lower()
        for game in data["games"]
        for team in (game["home"], game["away"])
    }
    assert len(abbreviations) == 16
    assert all((STATIC_ROOT / "logos" / f"{abbr}.png").is_file() for abbr in abbreviations)
    app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "LOCAL_LOGO_TEAMS" in app
    assert "logos/${team.abbreviation.toLowerCase()}.png" in app


def test_player_display_aliases_cover_common_current_roster_names():
    from .display_data import display_name

    assert display_name({"player_id": "203484", "name": "Kentavious Caldwell-Pope"}) == "肯塔维奥斯·考德威尔-波普"
    assert display_name({"player_id": "1629014", "name": "Anfernee Simons"}) == "安芬尼·西蒙斯"
    assert display_name({"player_id": "1642845", "name": "VJ Edgecombe"}) == "VJ·埃奇库姆"
    app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "Kentavious Caldwell-Pope" in app
    assert "Caldwell-Pope" in app


def test_versioned_display_snapshot_is_compact_and_keeps_real_roster_context():
    data = build_app_data()
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) < 150_000
    assert "raw_identity" not in encoded
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    players = game["roster_context"]["teams"]["away"]["official_team_roster"]["players"]
    names = {item["display_name"] for item in players}
    assert {"勒布朗·詹姆斯", "泰瑞斯·马克西", "乔尔·恩比德"} <= names
    cold = game["roster_context"]["teams"]["away"]["cold_start"]
    assert cold["cold_start"] is True
    assert cold["rolling_5_current_season_games"] == 0
    assert cold["rolling_5_previous_season_games"] == 5
    assert cold["rolling_15_current_season_games"] == 0
    assert cold["rolling_15_previous_season_games"] == 15


def test_absent_schedule_features_are_not_reported_as_false(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{}", encoding="utf-8")
    # normalize_game records paths relative to the project root.
    from . import export_data
    original_root = export_data.ROOT
    export_data.ROOT = tmp_path
    try:
        report = {
            "game": {"game_id": "espn:fixture", "home_team_id": 1610612765,
                     "away_team_id": 1610612738, "scheduled_tipoff": "2026-10-20T19:00:00Z"},
            "prediction": {"win_probability": .55, "expected_margin": 1, "expected_total": 220},
            "team_form": {"values": {"home_rest_days": 7, "away_rest_days": 1,
                                       "home_back_to_back": 0, "away_back_to_back": 0}},
        }
        game = normalize_game({}, path, report)
        assert game["background"]["schedule"]["home_back_to_back"] is None
        assert game["background"]["schedule"]["away_rest_days"] is None
        assert game["key_factors"] == []
    finally:
        export_data.ROOT = original_root


def test_normalize_game_separates_model_rest_features_from_frozen_schedule_facts(tmp_path):
    from . import export_data

    path = tmp_path / "report.json"
    path.write_text("{}", encoding="utf-8")
    original_root = export_data.ROOT
    export_data.ROOT = tmp_path
    try:
        game_id = "espn:fixture-fact-context"
        report = {
            "game": {"game_id": game_id, "home_team_id": 1610612765,
                     "away_team_id": 1610612738, "scheduled_tipoff": "2026-10-03T23:00:00+00:00",
                     "status": "scheduled"},
            "snapshot": {"cutoff": "2026-10-03T19:30:00+00:00"},
            "prediction": {"win_probability": .55, "expected_margin": 1, "expected_total": 220},
            "team_form": {"values": {"home_rest_days": 7, "away_rest_days": 1}},
            "fact_context": {
                "schema_version": "pregame-fact-context-v1", "game_id": game_id,
                "feature_cutoff": "2026-10-03T19:00:00+00:00", "team_form": {},
                "schedule": {
                    "home": {"status": "available", "reason": None,
                        "basis": {"type": "planned_schedule", "game_ids": ["espn:prior"]},
                        "back_to_back": True, "source_refs": [{"game_id": "espn:prior",
                            "source_ref": "https://league.example/schedule", "captured_at": "2026-10-03T18:00:00+00:00"}]},
                    "away": {"status": "unavailable", "reason": "no_cutoff_eligible_schedule_evidence",
                        "basis": None, "back_to_back": None, "source_refs": []},
                },
            },
        }
        normalized = normalize_game({"game_id": game_id}, path, report)
        assert normalized["fact_context"]["game_id"] == game_id
        assert normalized["background"]["schedule"]["home_rest_days"] is None
        assert normalized["background"]["schedule"]["away_rest_days"] is None
        assert normalized["background"]["schedule"]["home_back_to_back"] is True
        assert normalized["background"]["schedule"]["away_back_to_back"] is None

        report["fact_context"]["schedule"]["home"]["source_refs"][0]["captured_at"] = "2026-10-03T19:15:00+00:00"
        late = normalize_game({"game_id": game_id}, path, report)
        assert late["background"]["schedule"]["home_back_to_back"] is None
    finally:
        export_data.ROOT = original_root


def test_generation_input_preserves_the_baseline_fact_context_without_recomputing_it():
    from .generation_input import prepare_generation_data

    data = build_app_data()
    game = data["games"][0]
    frozen = {
        "schema_version": "pregame-fact-context-v1", "game_id": game["id"],
        "feature_cutoff": "2026-09-19T00:00:00+00:00", "team_form": {"home": {"game_ids": ["g1"]}},
        "schedule": {"home": {"status": "unavailable", "back_to_back": None,
                                "basis": None, "reason": "calendar_unknown", "source_refs": []}},
    }
    game["fact_context"] = json.loads(json.dumps(frozen))
    prepared = prepare_generation_data(
        data, game["id"], cutoff="2026-09-20T12:00:00+00:00",
        baseline_builder=lambda *_: (_ for _ in ()).throw(AssertionError("existing baseline must be reused")),
    )
    selected = next(item for item in prepared["games"] if item["id"] == game["id"])
    assert selected["fact_context"] == frozen
    assert game["fact_context"] == frozen


def test_factor_thresholds_and_fixed_semantic_priority():
    game = {"home_team_id": 1610612765, "away_team_id": 1610612738}
    values = {"home_net_rating_roll15": 4, "away_net_rating_roll15": 1,
              "home_win_rate_roll15": 9 / 15, "away_win_rate_roll15": 7 / 15,
              "home_rest_days": 7, "away_rest_days": 1}
    report = {"team_form": {"values": values}}
    factors = select_key_factors(report, game)
    assert [factor["label"] for factor in factors] == ["近期攻防表现差异", "近期战绩差异"]
    assert "领先 3.0" in factors[0]["detail"]
    assert "13.3 个百分点" in factors[1]["detail"]
    # Model rest values no longer enter user-facing key-factor selection.
    values["away_rest_days"] = 7
    assert len(select_key_factors(report, game)) == 2
    values["home_win_rate_roll15"] = 8 / 15
    assert len(select_key_factors(report, game)) == 1
    values["home_net_rating_roll15"] = 3.9
    assert select_key_factors(report, game) == []


def test_factors_skip_missing_equal_and_nonfinite_values():
    game = {"home_team_id": 1610612765, "away_team_id": 1610612738}
    assert select_key_factors({}, game) == []
    report = {"team_form": {"values": {"home_rest_days": 7, "away_rest_days": 7,
                                       "home_net_rating_roll15": float("nan"), "away_net_rating_roll15": 1}}}
    assert select_key_factors(report, game) == []


def test_ai_endpoint_requires_click_time_credentials_without_touching_agent():
    data = build_app_data()

    class VersionService:
        generated = False

        def selected(self, game):
            return {
                "status": "unpublished", "game_id": game["id"], "version": None,
                "version_id": None, "state_revision": 0, "locked_at": None,
            }

        def generate(self, *args, **kwargs):
            self.generated = True
            raise AssertionError("writer must not run without credentials")

    service = VersionService()
    status, payload = build_ai_response(
        data["games"][0]["id"], api_key="", data=data, version_service=service
    )
    assert status == 200
    assert payload["update_status"] == "read_only"
    assert service.generated is False


def test_ai_endpoint_rejects_unknown_game_before_agent():
    data = build_app_data()
    class ForbiddenVersionService:
        def selected(self, game):
            raise AssertionError("unknown game reached version store")
    status, payload = build_ai_response(
        "espn:unknown", api_key="fixture", data=data, version_service=ForbiddenVersionService()
    )
    assert status == 404
    assert "没有找到" in payload["message"]


def test_prediction_version_endpoint_reads_the_sqlite_selected_version(tmp_path):
    data = build_app_data()
    game = data["games"][0]
    legacy = tmp_path / "current_analyses"
    legacy.mkdir()
    (legacy / f"{game['id'].replace(':', '_')}.json").write_text(
        json.dumps({"game_id": game["id"], "judgment": {"final_prediction": {"winner_side": "away"}}}),
        encoding="utf-8",
    )
    clock = VersionClock()
    store = PredictionVersionStore(tmp_path / "prediction_versions.sqlite", clock=clock)
    version = publish_test_version(
        store, clock, game, "00000000-0000-0000-0000-000000000111"
    )
    status, result = build_prediction_versions_response(
        data=data, version_service=PredictionVersionService(store=store, clock=clock)
    )
    assert status == 200
    selected = result["predictions"][game["id"]]
    assert selected["version_id"] == version["version_id"]
    assert selected["state_revision"] == 1
    assert selected["version"]["judgment"]["final_prediction"]["expected_margin"] == 1.5
    assert selected["version"]["input_snapshot"]["relationship"]["final"]["expected_margin"] == game["relationship"]["final"]["expected_margin"]
    assert selected["fact_view"]["origin"] == "legacy_frozen"
    assert selected["fact_view"]["version_id"] == version["version_id"]
    assert selected["fact_view"]["source_payload_sha256"] == version["payload_sha256"]
    assert selected["fact_view"]["context"]["schedule"]["home"]["back_to_back"] is None


def test_changed_current_inputs_do_not_remove_a_historically_valid_version(tmp_path):
    data = build_app_data()
    game = data["games"][0]
    clock = VersionClock()
    store = PredictionVersionStore(tmp_path / "prediction_versions.sqlite", clock=clock)
    version = publish_test_version(
        store, clock, game, "00000000-0000-0000-0000-000000000112"
    )
    game["relationship"]["final"]["expected_margin"] += 1
    status, result = build_prediction_versions_response(
        data=data, version_service=PredictionVersionService(store=store, clock=clock)
    )
    assert status == 200
    selected = result["predictions"][game["id"]]
    assert selected["version_id"] == version["version_id"]
    assert selected["version"]["input_snapshot"]["relationship"]["final"]["expected_margin"] == version["input_snapshot"]["relationship"]["final"]["expected_margin"]


def test_analysis_input_fingerprint_is_cutoff_specific():
    game = build_app_data()["games"][0]
    cutoff = game["technical"]["cutoff"]
    later_cutoff = "2026-09-20T19:42:00+00:00"
    assert analysis_base_fingerprint(game, cutoff) != analysis_base_fingerprint(game, later_cutoff)


def test_historical_analysis_drops_context_captured_after_forecast_cutoff():
    game = copy.deepcopy(build_app_data()["games"][0])
    forecast = copy.deepcopy(game["relationship"]["final"])
    game["status"] = "completed"
    cutoff = game["technical"]["cutoff"]

    safe = historical_game_context(game, cutoff)

    assert safe["roster_context"] is None
    assert safe["matchup_history"] is None
    assert safe["season_form"] is None
    assert safe["season_phase"] is None
    assert safe["background"]["recent_form"] is None
    assert safe["background"]["schedule"] is None
    assert safe["relationship"]["final"] == forecast
    assert {item["section"] for item in safe["missing"]} >= {
        "roster_context", "matchup_history", "season_form", "recent_form", "schedule"
    }


def test_ai_endpoint_returns_the_server_selected_version():
    data = build_app_data()
    game_id = data["games"][0]["id"]
    data_game = data["games"][0]
    class FakeVersionService:
        def selected(self, selected_game):
            return {"status": "unpublished", "game_id": selected_game["id"], "version": None,
                    "version_id": None, "state_revision": 0, "locked_at": None}

        def generate(self, payload, selected_id, *, request_id):
            assert selected_id == game_id
            assert request_id == "00000000-0000-0000-0000-000000000113"
            identity = game_identity(data_game)
            version = {
                "version_id": "version-fixture-1",
                "request_seq": 1,
                "published_at": "2026-09-20T19:42:03+00:00",
                "judgment": {"final_prediction": {"winner_side": "home", "win_probability": 0.57,
                                                    "expected_margin": 1.5, "expected_total": 228.5}},
            }
            return {"status": "success", "update_status": "published", "selection_status": "current",
                    "game_id": selected_id, "game_identity": identity, "version_id": version["version_id"],
                    "state_revision": 1, "locked_at": None, "version": version}

    status, payload = build_ai_response(
        game_id, api_key="fixture", data=data, request_id="00000000-0000-0000-0000-000000000113",
        version_service=FakeVersionService(), allow_generate=True,
    )
    assert status == 200
    assert payload["version_id"] == "version-fixture-1"
    assert payload["version"]["judgment"]["final_prediction"]["expected_margin"] == 1.5


def test_ai_endpoint_update_failure_returns_the_previous_selected_version():
    data = build_app_data()
    game = data["games"][0]
    old_version = {"version_id": "old-v1", "judgment": {"final_prediction": {"winner_side": "home"}}}
    class FailingVersionService:
        def selected(self, selected_game):
            return {
                "status": "current", "game_id": selected_game["id"], "version": old_version,
                "version_id": "old-v1", "state_revision": 4, "locked_at": None,
            }

        def generate(self, *args, **kwargs):
            raise RuntimeError("mock writer failure")

    status, payload = build_ai_response(
        game["id"], api_key="fixture", data=data, version_service=FailingVersionService(), allow_generate=True
    )
    assert status == 200
    assert payload["update_status"] == "failed"
    assert payload["version_id"] == "old-v1"
    assert payload["version"] == old_version


def test_ai_endpoint_returns_locked_version_without_starting_generation():
    data = build_app_data()
    game = data["games"][0]
    locked_version = {"version_id": "locked-v1", "judgment": {"final_prediction": {"winner_side": "away"}}}
    class LockedVersionService:
        generated = False

        def selected(self, selected_game):
            return {
                "status": "locked", "game_id": selected_game["id"], "version": locked_version,
                "version_id": "locked-v1", "state_revision": 5, "locked_at": "2026-10-20T22:55:00+00:00",
                "lock_reason": "scheduled_tipoff_reached",
            }

        def generate(self, *args, **kwargs):
            self.generated = True
            raise AssertionError("locked game reached writer")

    service = LockedVersionService()
    status, payload = build_ai_response(
        game["id"], api_key="fixture", data=data, version_service=service
    )
    assert status == 200
    assert payload["update_status"] == "locked"
    assert payload["version_id"] == "locked-v1"
    assert service.generated is False


def test_saved_game_analysis_adds_traceable_web_evidence_without_changing_official():
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    official = json.dumps(game["relationship"]["final"], sort_keys=True)
    seen = {}

    class Searcher:
        def search(self, game, cutoff, **kwargs):
            assert kwargs["categories"] == {
                "availability", "lineup", "market", "roster_news", "game_story"
            }
            return {
                "status": "available",
                "records": [
                    {
                        "id": "web-1",
                        "category": "availability",
                        "title": "Official report",
                        "url": "https://www.nba.com/news/report",
                        "published_at": "2026-09-17T12:00:00+00:00",
                        "snippet": "Availability report.",
                        "eligible_for_fact": True,
                    },
                    {
                        "id": "web-market-1",
                        "category": "market",
                        "title": "76ers at Knicks odds",
                        "url": "https://sports.betmgm.com/en/blog/team/philadelphia-76ers/",
                        "published_at": None,
                        "captured_at": "2026-09-18T00:00:00+00:00",
                        "score": 0.9,
                        "eligible_for_fact": True,
                        "market_reference": {
                            "status": "available",
                            "usage": "display_only_public_market_reference",
                            "eligible_prediction_input": False,
                            "perspective": "home",
                            "home_spread": -5.5,
                            "away_spread": 5.5,
                            "expected_home_margin": 5.5,
                            "total": 231.5,
                            "source_title": "76ers at Knicks odds",
                            "source_url": "https://sports.betmgm.com/en/blog/team/philadelphia-76ers/",
                            "source_domain": "sports.betmgm.com",
                            "source_tier": 1,
                            "published_at": None,
                            "captured_at": "2026-09-18T00:00:00+00:00",
                        },
                    }
                ],
            }

    class Analyst:
        def judge(self, context):
            seen["context"] = context
            prediction_id = next(
                item["id"] for item in context.objective_evidence if item["section"] == "prediction"
            )
            return AnalystJudgment(
                winner_lean="home",
                side_lean="no_lean",
                total_lean="no_lean",
                confidence="low",
                supporting_evidence_ids=[prediction_id, "web-1"],
                conflicting_evidence_ids=[],
                baseline_agreement={"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
                subjective_rationale="正式预测提供方向，当前阵容与旧赛季窗口使优势幅度仍有不确定性。",
                core_question="当前阵容能否改变上赛季数据所描述的球队面貌？",
                game_script="比赛路径取决于双方如何处理核心持球与内线空间。",
                model_risk="旧赛季窗口和缺少球员级数值特征可能让球队均值失真。",
            ), {"status": "success"}

    result = SavedGameAnalysisService(
        data,
        analyst=Analyst(),
        searcher=Searcher(),
    ).analyze(game["id"])
    assert result["sources"][0]["id"] == "web-1"
    assert any(item["id"] == "web-1" for item in seen["context"].objective_evidence)
    assert json.dumps(game["relationship"]["final"], sort_keys=True) == official
    assert result["official_numeric_prediction"] == game["relationship"]["final"]
    assert result["public_market_reference"]["home_spread"] == -5.5
    assert result["public_market_reference"]["expected_home_margin"] == 5.5
    assert result["public_market_reference"]["eligible_prediction_input"] is False
    assert seen["context"].market_benchmark is None
    assert result["analysis_contract_version"] == "matchup-history-v1"


def test_saved_game_analysis_uses_deepseek_tool_research_when_supplied():
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    seen = {}

    class Searcher:
        def search(self, *args, **kwargs):
            raise AssertionError("deterministic fallback should not run")

    class Researcher:
        def investigate(self, identity, cutoff, **kwargs):
            assert "game_story" in kwargs["categories"]
            return ({
                "status": "available",
                "records": [{
                    "id": "story-1",
                    "category": "game_story",
                    "title": "2026 NBA Finals",
                    "url": "https://www.nba.com/playoffs/2026/nba-finals",
                    "published_at": "2026-06-14T00:00:00+00:00",
                    "captured_at": "2026-09-20T00:00:00+00:00",
                    "snippet": "The Knicks won the championship.",
                    "eligible_for_fact": True,
                }],
            }, {"status": "success", "calls": [{"tool": "search_pregame_web"}]})

    class Analyst:
        def judge(self, context):
            seen["context"] = context
            prediction_id = next(
                item["id"] for item in context.objective_evidence if item["section"] == "prediction"
            )
            return AnalystJudgment(
                winner_lean="home",
                side_lean="no_lean",
                total_lean="no_lean",
                confidence="low",
                supporting_evidence_ids=[prediction_id],
                conflicting_evidence_ids=[],
                baseline_agreement={"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
                subjective_rationale="结构化验证。",
            ), {"status": "success"}

    result = SavedGameAnalysisService(
        data,
        analyst=Analyst(),
        searcher=Searcher(),
        researcher=Researcher(),
    ).analyze(game["id"])
    story = [
        item for item in seen["context"].objective_evidence
        if (item.get("value") or {}).get("category") == "game_story"
    ]
    assert story and story[0]["id"] == "story-1"
    assert result["search"]["mode"] == "deepseek_tool_loop"
    assert result["search"]["tool_trace"]["calls"][0]["tool"] == "search_pregame_web"


def test_structured_market_prevents_public_market_lookup():
    data = build_app_data()
    game = json.loads(json.dumps(data["games"][0]))
    game["background"]["market"] = {"home_spread": -2.5, "total": 224.5}
    game["relationship"]["market"] = {"market_expected_margin": 2.5}
    assert "market" not in search_categories(game)
    assert "game_story" in search_categories(game)


def test_public_market_selection_never_merges_sources():
    context = {
        "records": [
            {
                "id": "media",
                "category": "market",
                "score": 0.99,
                "market_reference": {
                    "status": "available", "source_tier": 2, "source_url": "https://foxsports.com/a",
                    "home_spread": -4.5, "total": 228.5,
                },
            },
            {
                "id": "book",
                "category": "market",
                "score": 0.8,
                "market_reference": {
                    "status": "available", "source_tier": 1, "source_url": "https://betmgm.com/b",
                    "home_spread": -5.5, "total": 231.5,
                },
            },
        ]
    }
    selected = select_public_market_reference(context)
    assert selected["evidence_id"] == "book"
    assert selected["home_spread"] == -5.5
    assert selected["total"] == 231.5


def test_market_quote_preserves_home_spread_semantics():
    market = normalize_market(
        {
            "market": {
                "as_of_cutoff": {
                    "home_spread": -6.5,
                    "expected_home_margin": 6.5,
                    "total": 224.5,
                    "provider": "DraftKings",
                }
            }
        }
    )
    assert market["perspective"] == "home"
    assert market["home_spread"] == -6.5
    assert market["expected_home_margin"] == 6.5


def test_verified_public_market_cache_is_identity_checked_and_display_only(tmp_path):
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    context = cached_public_market_context(game)
    reference = select_public_market_reference(context)
    assert reference["market_stage"] == "early"
    assert reference["home_spread"] == -5.5
    assert reference["away_spread"] == 5.5
    assert reference["expected_home_margin"] == 5.5
    assert reference["total"] == 233.5
    assert reference["eligible_prediction_input"] is False
    assert reference["source_domain"] == "fanduel.com"

    payload = json.loads(
        (STATIC_ROOT.parent / "data" / "public_market_references.json").read_text(encoding="utf-8")
    )
    target = next(item for item in payload["records"] if item["game_id"] == game["id"])
    target["game"]["home_team_id"] = 1610612755
    bad = tmp_path / "bad-market-cache.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    assert cached_public_market_context(game, bad) is None


def test_historical_market_cache_after_cutoff_is_rejected(tmp_path):
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    payload = json.loads(
        (STATIC_ROOT.parent / "data" / "public_market_references.json").read_text(encoding="utf-8")
    )
    target = next(item for item in payload["records"] if item["game_id"] == game["id"])
    target["market_reference"]["captured_at"] = "2026-09-20T00:00:00+00:00"
    cache = tmp_path / "market-cache.json"
    cache.write_text(json.dumps(payload), encoding="utf-8")

    assert cached_public_market_context(
        game,
        cache,
        cutoff="2026-09-19T23:59:59+00:00",
    ) is None


@pytest.mark.parametrize(
    ("game_id", "home_spread", "total"),
    [
        ("espn:401909088", -1.5, 221.5),
        ("espn:401909089", -5.5, 233.5),
        ("espn:401909090", -1.5, 231.5),
        ("espn:401909835", -6.5, 238.5),
        ("espn:401909091", -1.5, 243.5),
        ("espn:401909836", 4.5, 226.5),
    ],
)
def test_verified_public_market_cache_covers_six_demo_games(game_id, home_spread, total):
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == game_id)
    reference = select_public_market_reference(cached_public_market_context(game))
    assert reference["home_spread"] == home_spread
    assert reference["away_spread"] == -home_spread
    assert reference["total"] == total
    assert reference["eligible_prediction_input"] is False


def test_public_market_endpoint_is_independent_from_ai_and_uses_verified_cache():
    data = build_app_data()

    class Searcher:
        def search(self, *args, **kwargs):
            raise AssertionError("verified cache should resolve before web search")

    status, payload = build_market_response(
        "espn:401909089",
        data=data,
        searcher=Searcher(),
    )
    assert status == 200
    assert payload["status"] == "available"
    assert payload["lookup"]["source"] == "verified_cache"
    reference = payload["public_market_reference"]
    assert reference["home_spread"] == -5.5
    assert reference["total"] == 233.5
    assert reference["eligible_prediction_input"] is False


def test_analyst_context_keeps_cached_public_market_when_web_search_is_unavailable():
    data = build_app_data()

    class Searcher:
        def search(self, *args, **kwargs):
            raise ValueError("web_search_api_key_missing")

    class Analyst:
        def judge(self, context):
            assert context.market_benchmark is None
            public_market = [
                item
                for item in context.objective_evidence
                if (item.get("value") or {}).get("category") == "market"
            ]
            assert public_market
            prediction_id = next(
                item["id"] for item in context.objective_evidence if item["section"] == "prediction"
            )
            return AnalystJudgment(
                winner_lean="home",
                side_lean="no_lean",
                total_lean="no_lean",
                confidence="low",
                supporting_evidence_ids=[prediction_id],
                conflicting_evidence_ids=[],
                baseline_agreement={"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
                subjective_rationale="正式预测提供方向，公开早盘只作背景参考。",
                core_question="当前阵容能否改变历史基线？",
                game_script="比赛路径仍取决于核心持球和空间。",
                model_risk="旧赛季窗口可能让球队均值失真。",
            ), {"status": "success"}

    result = SavedGameAnalysisService(
        data,
        analyst=Analyst(),
        searcher=Searcher(),
    ).analyze("espn:401909089")
    assert result["public_market_reference"]["home_spread"] == -5.5
    assert result["search"]["status"] == "unavailable"
    assert result["search"]["public_market_reference"] == "available"


def test_zero_local_season_rows_are_not_an_opener_or_capped_rest_fact():
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    original_prediction = json.dumps(game["relationship"], sort_keys=True)
    evidence, _, _ = _evidence(game, "2026-09-27T00:00:00Z", None)
    sections = {item["section"]: item["value"] for item in evidence}
    assert "team_form" in sections
    assert sections["season_form"]["teams"]["home"]["current_season_games"] == 0
    assert sections["season_phase"]["global_opening_night_confirmed"] is False
    assert sections["season_phase"]["phase"] == "very_early"
    assert sections["matchup"]["schedule"]["home"]["back_to_back"] is None
    assert sections["matchup"]["schedule"]["away"]["back_to_back"] is None
    assert "fact_context" not in sections
    assert json.dumps(game["relationship"], sort_keys=True) == original_prediction


def test_evidence_uses_frozen_fact_context_schedule_and_keeps_opening_window_form():
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    game["fact_context"] = {
        "schema_version": "pregame-fact-context-v1", "game_id": game["id"],
        "feature_cutoff": "2026-09-26T00:00:00+00:00",
        "team_form": {"home": {"requested_games": 15, "observed_games": 15,
            "season_counts": {"2025": 15}, "competition_counts": {"playoffs": 15},
            "metrics": {"net_rating_15": {"value": 2.5, "valid_games": 13, "missing_games": 2,
                "unit": "points_per_100_possessions", "aggregation": "mean_of_per_game_values",
                "method": "v1_per_game_mean_shift_1_rolling_15"}}}},
        "schedule": {"home": {"status": "available", "reason": None,
            "basis": {"type": "planned_schedule", "game_ids": ["espn:prior"]},
            "back_to_back": True, "source_refs": [{"game_id": "espn:prior",
                "source_ref": "https://league.example/schedule", "captured_at": "2026-09-25T00:00:00+00:00"}]},
            "away": {"status": "unavailable", "reason": "calendar_coverage_unknown",
                "basis": None, "back_to_back": None, "source_refs": []}},
    }
    game["background"]["schedule"] = {"home_rest_days": 7, "away_rest_days": 7,
                                        "home_back_to_back": False, "away_back_to_back": False}
    evidence, _, _ = _evidence(game, "2026-09-27T00:00:00+00:00", None)
    sections = {item["section"]: item["value"] for item in evidence}
    assert sections["fact_context"]["team_form"]["home"]["metrics"]["net_rating_15"]["valid_games"] == 13
    assert sections["matchup"]["schedule"]["home"]["back_to_back"] is True
    assert sections["matchup"]["schedule"]["away"]["back_to_back"] is None
    assert "home_rest_days" not in sections["matchup"]["schedule"]
    assert "team_form" in sections


def test_structured_market_suppresses_public_market_endpoint():
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    game["background"]["market"] = {"home_spread": -4.5, "total": 231.5}
    status, payload = build_market_response(game["id"], data=data)
    assert status == 200
    assert payload["status"] == "structured_available"
    assert payload["public_market_reference"] is None


def test_ai_sheet_has_loading_success_sections_and_recoverable_error_copy():
    from .export_data import STATIC_ROOT

    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "正在读取赛前分析" in script
    for label in ("球队与阵容", "关键球员与对位", "为什么这么看", "冲突信息", "仍未解决的不确定性"):
        assert label in script
    assert "公开市场参考" in script
    assert 'fetch(`/api/market-reference?game_id=' in script
    assert 'fetch("/api/ai-analysis"' not in script
    assert "公开早盘参考" in script
    assert "第25秒分析暂不可用" in script
    assert "renderProductAnalysis" in script
    for removed in (
        "未进入正式数值预测",
        "正式市场基准暂无",
        "不代表模型预测的因果贡献",
        "当前离线提要",
    ):
        assert removed not in script
    for internal in ("cold_start", "previous_season_is_majority 为 true", "def_rating_15"):
        assert internal in script
