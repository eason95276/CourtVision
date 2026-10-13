from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import json
import threading
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from v2.pregame_system.prediction_versions import PredictionVersionStore
from v2.pregame_system.storage import content_hash

from .schedule_catalog import ScheduleCatalog, normalize_espn_event
from .schedule_sync import NBA_TEAMS_URL, ScheduleSyncRuntime, import_current_schedule, refresh_scoreboard_window
from . import server as frontend_server
from .server import (
    _current_schedule_season_form,
    _with_portfolio_editorial,
    build_game_detail_response,
    build_prediction_versions_response,
    build_schedule_window_response,
)


def _event(
    event_id="401999001",
    *,
    date="2026-10-20T19:00:00Z",
    season_type="2",
    state="pre",
    status_name="STATUS_SCHEDULED",
    completed=False,
    home_id="8",
    home_abbr="DET",
    away_id="2",
    away_abbr="BOS",
    home_score="0",
    away_score="0",
    neutral=False,
    notes=None,
):
    season_type_number = int(season_type)
    season_type_name = {1: "Preseason", 2: "Regular Season", 3: "Postseason"}.get(
        season_type_number, "Unknown"
    )
    return {
        "id": event_id,
        "date": date,
        "season": {"year": 2027, "displayName": "2026-27"},
        "seasonType": {
            "id": season_type,
            "type": season_type_number,
            "name": season_type_name,
        },
        "competitions": [
            {
                "id": event_id,
                "date": date,
                "neutralSite": neutral,
                "venue": {"fullName": "Test Arena", "address": {"city": "Detroit"}},
                "notes": notes or [],
                "competitors": [
                    {
                        "id": home_id,
                        "homeAway": "home",
                        "team": {"id": home_id, "abbreviation": home_abbr, "displayName": home_abbr},
                        "score": home_score,
                    },
                    {
                        "id": away_id,
                        "homeAway": "away",
                        "team": {"id": away_id, "abbreviation": away_abbr, "displayName": away_abbr},
                        "score": away_score,
                    },
                ],
                "status": {
                    "type": {
                        "name": status_name,
                        "state": state,
                        "completed": completed,
                        "description": status_name,
                    }
                },
            }
        ],
    }


def _response(payload, *, url="https://site.api.espn.com/test", captured_at="2026-10-05T02:00:00+00:00", status="success"):
    raw = json.dumps(payload, sort_keys=True).encode()
    return {
        "url": url,
        "captured_at": captured_at,
        "status": status,
        "http_status": 200 if status == "success" else None,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "payload": payload if status == "success" else None,
        "raw_text": raw.decode(),
    }


@pytest.mark.parametrize("status,body,error", [
    (200, b'{"events":[]}', None), (200, b'<html>blocked</html>', "invalid_json"),
    (200, b'[]', "json_root_not_object"), (302, b'{}', "http_status_not_success"),
    (503, b'{}', "http_status_not_success"),
])
def test_native_schedule_transport_preserves_validation_and_response_hash(monkeypatch, status, body, error):
    from . import schedule_sync
    monkeypatch.setattr(schedule_sync, "_native_https_client", lambda: "native-client")
    monkeypatch.setattr(schedule_sync, "_native_https_response", lambda *args: (status, body))
    result = schedule_sync.fetch_json("https://site.api.espn.com/test")
    assert result["transport"] == "windows_native_https"
    assert result["raw_sha256"] == hashlib.sha256(body).hexdigest()
    assert result["http_status"] == status and result["error"] == error
    assert result["status"] == ("success" if error is None else "unavailable")
    if error: assert result["payload"] is None


def test_native_schedule_command_has_no_tls_bypass_or_redirect_and_bounds_response(monkeypatch):
    from . import schedule_sync
    from pathlib import Path
    seen = {}
    def execute(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        Path(args[args.index("--output") + 1]).write_bytes(b'{"events":[]}')
        return SimpleNamespace(returncode=0, stdout=b"200", stderr=b"")
    monkeypatch.setattr(schedule_sync.subprocess, "run", execute)
    status, raw = schedule_sync._native_https_response("native-client", "https://site.api.espn.com/test", 20)
    assert status == 200 and json.loads(raw) == {"events": []}
    assert "--insecure" not in seen["args"] and "--location" not in seen["args"]
    assert seen["args"][seen["args"].index("--proto") + 1] == "=https"
    assert seen["args"][seen["args"].index("--max-filesize") + 1] == str(schedule_sync.MAX_RESPONSE_BYTES)
    assert seen["kwargs"]["timeout"] == 22


@pytest.mark.parametrize("url", ["http://site.api.espn.com/test", "https://example.com/test",
                                 "https://user:secret@site.api.espn.com/test", "https://site.api.espn.com:444/test"])
def test_schedule_transport_rejects_nonallowlisted_destinations_before_request(monkeypatch, url):
    from . import schedule_sync
    monkeypatch.setattr(schedule_sync, "_native_https_client", lambda: pytest.fail("must reject before transport"))
    with pytest.raises(ValueError, match="not_allowlisted"):
        schedule_sync.fetch_json(url)


def _board(*events):
    return {"events": list(events)}


def test_per_game_season_type_overrides_top_level_season_label():
    event = _event(season_type="2")
    row = normalize_espn_event(
        event,
        source_url="https://site.api.espn.com/teams/2/schedule?season=2027&seasontype=2",
        captured_at="2026-10-05T02:00:00Z",
        response_sha256="a" * 64,
        requested_season={"year": 2027, "type": 2},
        expected_game_type="regular_season",
    )
    assert row["season"] == 2027
    assert row["game_type"] == "regular_season"
    assert row["game_id"] == "espn:401999001"
    assert row["nba_game_id"] is None
    assert row["status"] == "scheduled"
    assert row["source_type"] == "espn_team_schedule"
    assert row["home_score"] is None and row["away_score"] is None
    assert row["timezone"] == "UTC"


@pytest.mark.parametrize(
    ("state", "status_name", "completed", "expected"),
    [
        ("pre", "STATUS_SCHEDULED", False, "scheduled"),
        ("in", "STATUS_IN_PROGRESS", False, "in_progress"),
        ("post", "STATUS_FINAL", True, "final"),
        ("post", "STATUS_CANCELED", True, "cancelled"),
        ("post", "STATUS_POSTPONED", False, "postponed"),
        ("mystery", "STATUS_UNKNOWN", False, "unknown"),
    ],
)
def test_status_and_score_normalization_never_turns_scheduled_zeroes_into_scores(
    state, status_name, completed, expected
):
    event = _event(state=state, status_name=status_name, completed=completed)
    row = normalize_espn_event(
        event,
        source_url="https://site.api.espn.com/scoreboard",
        captured_at="2026-10-05T02:00:00Z",
        response_sha256="b" * 64,
    )
    assert row["status"] == expected
    if expected == "scheduled":
        assert row["home_score"] is None and row["away_score"] is None
    elif expected == "in_progress":
        assert row["home_score"] == 0 and row["away_score"] == 0


def test_catalog_import_is_idempotent_and_keeps_game_identity_separate_from_generation(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "catalog.sqlite", clock=lambda: datetime(2026, 10, 5, tzinfo=timezone.utc))
    game = _event()
    response = _response(_board(game))
    first = catalog.ingest("trial", [response])
    second = catalog.ingest("trial", [response])
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["unchanged"] == 1
    assert catalog.get_game("espn:401999001")["nba_game_id"] is None
    assert catalog.count_games() == 1
    assert catalog.count_generation_tasks() == 0


def test_recent_form_uses_same_season_completed_schedule_before_frozen_cutoff_only(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "recent-form.sqlite")
    prior_warriors_loss = _event(
        event_id="401888001", date="2026-10-04T23:00:00Z", season_type="1",
        state="post", status_name="STATUS_FINAL", completed=True,
        home_id="12", home_abbr="LAC", away_id="9", away_abbr="GSW",
        home_score="104", away_score="101",
    )
    prior_lakers_win = _event(
        event_id="401888002", date="2026-10-06T02:00:00Z", season_type="1",
        state="post", status_name="STATUS_FINAL", completed=True,
        home_id="23", home_abbr="SAC", away_id="13", away_abbr="LAL",
        home_score="103", away_score="127",
    )
    target_final = _event(
        event_id="401898390", date="2026-10-07T02:00:00Z", season_type="1",
        state="post", status_name="STATUS_FINAL", completed=True,
        home_id="9", home_abbr="GSW", away_id="13", away_abbr="LAL",
        home_score="124", away_score="98",
    )
    later_final = _event(
        event_id="401888003", date="2026-10-08T02:00:00Z", season_type="1",
        state="post", status_name="STATUS_FINAL", completed=True,
        home_id="9", home_abbr="GSW", away_id="23", away_abbr="SAC",
        home_score="123", away_score="118",
    )
    cancelled = _event(
        event_id="401888004", date="2026-10-05T22:00:00Z", season_type="1",
        state="post", status_name="STATUS_CANCELED", completed=True,
        home_id="9", home_abbr="GSW", away_id="23", away_abbr="SAC",
        home_score="0", away_score="0",
    )
    catalog.ingest("schedule-early", [_response(
        _board(prior_warriors_loss), captured_at="2026-10-06T14:44:22+00:00")])
    catalog.ingest("schedule-late", [_response(_board(
        prior_lakers_win, target_final, later_final, cancelled,
    ), captured_at="2026-10-08T12:00:00+00:00")])
    game = {
        "id": "espn:401898390", "tipoff": "2026-10-07T02:00:00+00:00",
        "season": 2027, "season_label": "2026-27", "game_type": "preseason",
        "home": {"id": 1610612744}, "away": {"id": 1610612747},
    }
    version = {"version_id": "frozen-v1", "input_cutoff_at": "2026-10-07T01:00:00+00:00",
               "analyst_context": {"objective_evidence": [{"id": "pregame-score", "section": "web_context",
                   "value": {"eligible_for_fact": True, "game_id": "espn:401898390",
                       "team_id": 1610612747, "claim_type": "game_news",
                       "published_at": "2026-10-06T15:00:00+00:00",
                       "quote": "The Lakers secured a 127-103 victory over the Kings in their preseason opener."}}]}}
    form = _current_schedule_season_form(game, version, catalog)

    assert form["status"] == "available"
    assert form["cutoff"] == "2026-10-07T01:00:00+00:00"
    assert form["competition_scope"] == "all_game_types_in_current_season"
    home = form["teams"]["home"]
    away = form["teams"]["away"]
    assert home["current_season_games"] == 1
    assert home["current_season_record"] == {"wins": 0, "losses": 1}
    assert home["current_last5"]["game_ids"] == ["espn:401888001"]
    assert home["last_result"]["team_score"] == 101
    assert home["last_result"]["opponent_score"] == 104
    assert home["last_result"]["opponent_name_zh"] == "快船"
    assert away["current_season_games"] == 1
    assert away["current_season_record"] == {"wins": 1, "losses": 0}
    assert away["last_result"]["team_score"] == 127
    assert away["last_result"]["opponent_score"] == 103
    assert away["last_result"]["opponent_name_zh"] == "国王"
    all_used = {game_id for team in form["teams"].values()
                for game_id in team["current_last5"]["game_ids"]}
    assert "espn:401898390" not in all_used
    assert "espn:401888003" not in all_used
    assert "espn:401888004" not in all_used
    assert "previous_last10" not in form["teams"]["away"]
    late_source_version = {
        **version,
        "analyst_context": {"objective_evidence": [{"id": "late-score", "section": "web_context",
            "value": {"eligible_for_fact": True, "game_id": "espn:401898390",
                "team_id": 1610612747, "claim_type": "game_news",
                "published_at": "2026-10-08T12:00:00+00:00",
                "quote": "The Lakers secured a 127-103 victory over the Kings."}}]},
    }
    late_source_form = _current_schedule_season_form(game, late_source_version, catalog)
    assert late_source_form["teams"]["away"]["current_season_games"] == 0


def test_portfolio_editorial_is_a_hash_bound_display_overlay_only(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend_server, "DEFAULT_PORTFOLIO_EDITORIAL_DIR", tmp_path)
    version = {
        "version_id": "frozen-v1", "payload_sha256": "a" * 64,
        "game_id": "espn:401999001", "input_cutoff_at": "2026-10-19T12:00:00+00:00",
        "input_snapshot": {},
    }
    editorial = {
        "pregame_story": {"title": "冻结资料支持的标题", "statement": "赛前短评"},
        "analysis_article": "新文章",
        "watch_themes": [{"title": "看点", "statement": "对位"}],
        "player_analysis": [{"player_name": "Player A", "statement": "球员分析", "evidence_ids": []}],
    }
    artifact = {
        "schema_version": "portfolio-editorial-v1", "artifact_id": "editorial-only-v1",
        "base_prediction_version_id": "frozen-v1", "base_prediction_payload_sha256": "a" * 64,
        "editorial": editorial, "editorial_sha256": content_hash(editorial),
        "generated_at": "2026-10-19T12:00:00+00:00",
    }
    (tmp_path / "frozen-v1.json").write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    selection = {"status": "locked", "game_id": version["game_id"], "version_id": version["version_id"],
                 "version": version, "state_revision": 3}

    result = _with_portfolio_editorial(selection)
    assert result["version"] is version
    assert result["version"]["payload_sha256"] == "a" * 64
    assert result["display_projection"]["portfolio_editorial"]["editorial"] == editorial

    mismatched = {**selection, "version": {**version, "payload_sha256": "b" * 64}}
    refused = _with_portfolio_editorial(mismatched)
    assert "portfolio_editorial" not in refused["display_projection"]


def test_conflicting_duplicate_provider_identity_is_recorded_and_not_silently_selected(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "catalog.sqlite")
    one = _event(date="2026-10-20T19:00:00Z")
    other = _event(date="2026-10-20T20:00:00Z")
    result = catalog.ingest("trial", [_response(_board(one), url="https://site.api.espn.com/a"), _response(_board(other), url="https://site.api.espn.com/b")])
    assert result["conflicts"] == 1
    assert catalog.get_game("espn:401999001") is None
    assert catalog.conflict_count() == 1


def test_registered_game_tipoff_change_is_held_for_controlled_revision(tmp_path):
    db_path = tmp_path / "versions.sqlite"
    catalog = ScheduleCatalog(db_path, clock=lambda: datetime(2026, 10, 5, tzinfo=timezone.utc))
    version_store = PredictionVersionStore(db_path, clock=lambda: datetime(2026, 10, 5, tzinfo=timezone.utc))
    original = _event(date="2026-10-20T19:00:00Z")
    catalog.ingest("initial", [_response(_board(original))])
    game = {"id": "espn:401999001", "tipoff": "2026-10-20T19:00:00Z", "status": "scheduled",
            "home": {"id": 1610612765}, "away": {"id": 1610612738}}
    version_store.begin_request(game, request_id="00000000-0000-4000-8000-000000000001")
    changed = _event(date="2026-10-20T20:00:00Z")
    result = catalog.ingest("update", [_response(_board(changed))])
    assert result["conflicts"] == 1
    assert catalog.get_game("espn:401999001")["scheduled_tipoff"] == "2026-10-20T19:00:00+00:00"
    assert catalog.last_conflict()["action"] == "held_registered_game"


def test_bjt_window_uses_aware_time_and_preserves_daylight_saving_instants(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "catalog.sqlite")
    earlier = _event(event_id="401999101", date="2026-11-01T05:30:00Z")
    later = _event(event_id="401999102", date="2026-11-01T06:30:00Z")
    catalog.ingest("window", [_response(_board(earlier, later))])
    window = catalog.homepage_window(datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc))
    assert window["today"] == "2026-11-01"
    assert len(window["games"]) == 2
    assert {game["beijing_date"] for game in window["games"]} == {"2026-11-01"}
    assert all(game["scheduled_tipoff"].endswith("+00:00") for game in window["games"])


def test_window_distinguishes_successful_empty_from_failed_sync_and_keeps_old_rows(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "catalog.sqlite")
    game = _event(date="2026-10-05T23:00:00Z")
    catalog.ingest("initial", [_response(_board(game))])
    empty = catalog.ingest("refresh", [_response(_board(), url="https://site.api.espn.com/scoreboard?empty")])
    assert empty["status"] == "success"
    window = catalog.homepage_window(datetime(2026, 10, 5, 16, tzinfo=timezone.utc))
    assert window["status"] == "stale"
    assert window["games"]
    failed = _response({}, url="https://site.api.espn.com/failure", status="unavailable")
    catalog.ingest("refresh", [failed])
    assert catalog.homepage_window(datetime(2026, 10, 5, 16, tzinfo=timezone.utc))["status"] == "stale"


def test_pending_event_is_retained_without_inventing_game_id_or_tipoff(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "catalog.sqlite")
    pending = {"id": "", "date": None, "season": {"year": 2027}, "seasonType": {"id": "2"}, "competitions": []}
    response = _response({"events": [pending]})
    result = catalog.ingest("pending", [response])
    assert result["pending"] == 1
    row = catalog.pending_items()[0]
    assert row["game_id"] is None
    assert row["scheduled_tipoff"] is None
    assert catalog.count_games() == 0


def test_trial_import_uses_requested_and_per_event_season_type_and_never_registers_tasks(tmp_path):
    from courtvision.data import TEAM_ID_TO_ABBR

    espn_ids = {
        "ATL": "1", "BOS": "2", "BKN": "17", "CHA": "30", "CHI": "4", "CLE": "5",
        "DAL": "6", "DEN": "7", "DET": "8", "GS": "9", "HOU": "10", "IND": "11",
        "LAC": "12", "LAL": "13", "MEM": "29", "MIA": "14", "MIL": "15", "MIN": "16",
        "NO": "3", "NY": "18", "OKC": "25", "ORL": "19", "PHI": "20", "PHX": "21",
        "POR": "22", "SAC": "23", "SA": "24", "TOR": "28", "UTAH": "26", "WSH": "27",
    }
    directory = {"sports": [{"leagues": [{"teams": [
        {"team": {"id": espn_ids[abbr.upper()], "abbreviation": abbr.upper(), "displayName": abbr.upper()}}
        for abbr in TEAM_ID_TO_ABBR.values()
    ]}]}]}
    regular = _event(event_id="401999201", season_type="2", date="2026-10-20T19:00:00Z")
    regular_payload = {
        "requestedSeason": {"year": 2027, "type": 2, "name": "Regular Season"},
        "season": {"year": 2027, "type": 1, "name": "Preseason"},
        "events": [regular],
    }
    preseason = _event(
        event_id="401899201", season_type="1", date="2026-10-05T23:00:00Z",
        home_id="1", home_abbr="ATL", away_id="29", away_abbr="MEM",
    )
    def fetcher(url):
        if url == NBA_TEAMS_URL:
            return _response(directory, url=url)
        if "/teams/2/schedule?" in url or "/teams/8/schedule?" in url:
            return _response(regular_payload, url=url)
        if "/scoreboard?dates=" in url:
            return _response(_board(preseason), url=url)
        raise AssertionError(f"unexpected schedule URL: {url}")

    catalog = ScheduleCatalog(tmp_path / "trial.sqlite")
    result = import_current_schedule(
        catalog, fetcher=fetcher, season_year=2027,
        now=datetime(2026, 10, 5, 2, tzinfo=timezone.utc), team_ids={"2", "8"},
    )
    assert result["status"] == "success"
    assert result["inserted"] == 2
    assert catalog.count_games(season=2027, game_type="regular_season") == 1
    assert catalog.count_games(season=2027, game_type="preseason") == 1
    assert catalog.count_generation_tasks() == 0
    assert result["requested_teams"] == 2 and result["teams_complete"] == 2
    assert {row["games_returned"] for row in result["team_coverage"]} == {1}
    regular_row = catalog.get_game("espn:401999201")
    assert regular_row["game_type"] == "regular_season"
    assert regular_row["season"] == 2027
    assert regular_row["source_type"] == "espn_team_schedule"


def test_home_api_is_fixed_to_beijing_today_tomorrow_and_never_promotes_snapshot_prediction(tmp_path):
    now = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=lambda: now)
    service = SimpleNamespace(store=store)
    catalog = ScheduleCatalog(store.path, clock=lambda: now)
    today_final = _event(event_id="401999301", date="2026-10-05T15:00:00Z", state="post",
                         status_name="STATUS_FINAL", completed=True, home_score="105", away_score="129")
    tomorrow_scheduled = _event(event_id="401999302", date="2026-10-06T12:00:00Z")
    historic_outside_window = _event(event_id="401999303", date="2026-10-03T15:00:00Z")
    catalog.ingest("home", [_response(_board(today_final, tomorrow_scheduled, historic_outside_window))])
    stale_static = {
        "id": "espn:401999301", "tipoff": "2026-10-05T15:00:00+00:00", "status": "scheduled",
        "home": {"id": 1610612765}, "away": {"id": 1610612738},
        "prediction": {"available": True, "winner_probability": .93},
        "relationship": {"final": {"win_probability": .93}},
    }
    result = build_schedule_window_response(
        data={"games": [stale_static], "teams": []}, version_service=service,
        catalog=catalog, now=now,
    )
    assert result["schedule_window"]["today"] == "2026-10-05"
    assert result["schedule_window"]["tomorrow"] == "2026-10-06"
    assert result["schedule_window"]["today_count"] == 1
    assert result["schedule_window"]["tomorrow_count"] == 1
    assert {row["beijing_date"] for row in result["games"]} == {"2026-10-05", "2026-10-06"}
    assert {row["id"] for row in result["games"]} == {"espn:401999301", "espn:401999302"}
    final = next(row for row in result["games"] if row["id"] == "espn:401999301")
    assert final["status"] == "final"
    assert final["home"]["score"] == 105 and final["away"]["score"] == 129
    assert final["prediction"]["available"] is False
    assert final["relationship"]["final"] is None
    assert final["nba_game_id"] is None
    assert final["generation_hold_reason"] == "schedule_catalog_only_not_enabled_for_generation"
    assert catalog.count_generation_tasks() == 0
    _, selections = build_prediction_versions_response(data=result, version_service=service)
    assert selections["predictions"] == {}


def test_detail_api_reads_old_game_by_id_without_returning_it_in_home_window(tmp_path):
    now = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=lambda: now)
    service = SimpleNamespace(store=store)
    catalog = ScheduleCatalog(store.path, clock=lambda: now)
    today_game = _event(event_id="401999401", date="2026-10-05T15:00:00Z")
    old_heat = _event(event_id="401999402", date="2026-10-03T15:00:00Z", home_id="28", home_abbr="TOR",
                      away_id="14", away_abbr="MIA")
    catalog.ingest("detail", [_response(_board(today_game, old_heat))])
    static_old = {"id": "espn:401999402", "tipoff": "2026-10-03T15:00:00+00:00", "status": "final",
                  "home": {"id": 1610612761}, "away": {"id": 1610612748},
                  "prediction": {"available": True, "winner_probability": .91},
                  "relationship": {"final": {"win_probability": .91}}}
    status, detail = build_game_detail_response(
        "espn:401999402", data={"games": [static_old], "teams": []},
        version_service=service, catalog=catalog,
    )
    home = build_schedule_window_response(data={"games": [static_old], "teams": []},
                                          version_service=service, catalog=catalog, now=now)
    assert status == 200
    assert detail["game"]["id"] == "espn:401999402"
    assert detail["game"]["home"]["id"] == 1610612761
    assert detail["game"]["prediction"]["available"] is False
    assert "espn:401999402" not in {row["id"] for row in home["games"]}


def test_detail_api_exposes_pending_then_rejects_incomplete_postgame_fixture(tmp_path):
    from v2.pregame_system.prediction_versions import _json, payload_sha256
    from v2.postgame_system.version_service import PostgameVersionService

    now=datetime(2026,10,6,1,tzinfo=timezone.utc)
    store=PredictionVersionStore(tmp_path/"postgame-status.sqlite",clock=lambda:now)
    catalog=ScheduleCatalog(store.path,clock=lambda:now)
    final=_event(event_id="401999451",date="2026-10-05T23:00:00Z",season_type="1",state="post",
        status_name="STATUS_FINAL",completed=True,home_id="20",home_abbr="PHI",away_id="18",away_abbr="NYK",
        home_score="120",away_score="97")
    catalog.ingest("postgame-status",[_response(_board(final))],include_game_types={"preseason"})
    game_id="espn:401999451"
    with store.connect() as db:
        db.execute("INSERT INTO prediction_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",(
            "pregame-version","fixture-v1",game_id,"request-1",1,"2026-10-05T22:00:00+00:00",
            "2026-10-05T22:00:00+00:00","2026-10-05T22:01:00+00:00","2026-10-05T22:02:00+00:00",
            "{}","a"*64))
    service=SimpleNamespace(store=store,selected=lambda game:{"status":"locked","version_id":"pregame-version","version":None})

    status,detail=build_game_detail_response(game_id,data={"games":[],"teams":[]},
        version_service=service,catalog=catalog)
    assert status==200 and detail["game"]["postgame"]=={"status":"pending"}

    PostgameVersionService(store)
    result={"result_id":"result-1","game_id":game_id,"payload_json":"{}",
        "payload_sha256":payload_sha256({}),"captured_at":"2026-10-06T00:01:00+00:00"}
    post={"version_id":"postgame-version","game_id":game_id,"base_prediction_version_id":"pregame-version",
        "base_stage":"EARLY_PREVIEW","result_id":"result-1","published_at":"2026-10-06T00:02:00+00:00"}
    with store.connect() as db:
        db.execute("INSERT INTO game_result_snapshots VALUES(?,?,?,?,?)",tuple(result.values()))
        db.execute("INSERT INTO postgame_versions VALUES(?,?,?,?,?,?,?)",(
            post["version_id"],post["game_id"],post["base_prediction_version_id"],post["result_id"],
            _json(post),payload_sha256(post),post["published_at"]))
    status,detail=build_game_detail_response(game_id,data={"games":[],"teams":[]},
        version_service=service,catalog=catalog)
    assert status==200
    # This fixture deliberately contains no verified pregame/result/editorial payload.
    # A record's existence is not a reader-ready article.
    assert detail['game']['postgame']['status']=='unavailable'
    assert '校验未通过' in detail['game']['postgame']['message']


def test_only_previously_registered_games_fallback_and_are_marked_stale_when_catalog_has_no_success(tmp_path):
    now=datetime(2026,10,5,8,tzinfo=timezone.utc)
    store=PredictionVersionStore(tmp_path/'fallback.sqlite',clock=lambda:now)
    catalog=ScheduleCatalog(store.path,clock=lambda:now)
    registered={"id":"espn:401999801","tipoff":"2026-10-05T15:00:00+00:00","status":"scheduled",
                "registered_game":True,"schedule_revision":1,"homepage_visible":True,
                "home":{"id":1610612765,"name":"Detroit Pistons","name_zh":"活塞","abbreviation":"DET","accent":"#C8102E","logo_url":"x"},
                "away":{"id":1610612738,"name":"Boston Celtics","name_zh":"凯尔特人","abbreviation":"BOS","accent":"#007A33","logo_url":"y"},
                "source":{"url":"official-source","captured_at":"2026-10-04T00:00:00+00:00"},
                "prediction":None,"relationship":{"final":None},"background":{"schedule":{},"recent_form":{"home":{},"away":{}},"availability":{"status":"unavailable","records":[]}},
                "technical":{"source_label":"真实赛程 · 尚无正式赛前分析"},"missing":[],"key_factors":[]}
    service=SimpleNamespace(store=store)
    result=build_schedule_window_response(data={"games":[registered],"teams":[]},version_service=service,catalog=catalog,now=now)
    assert result["schedule_window"]["status"]=="stale"
    assert result["schedule_window"]["fallback_source"]=="previously_registered_schedules"
    assert [row["id"] for row in result["games"]]==[registered["id"]]
    assert result["games"][0].get("generation_hold_reason") is None
    assert catalog.count_games()==0


def test_sync_runtime_refreshes_on_start_daily_and_every_five_minutes_only_for_today_games(tmp_path):
    class MutableClock:
        value = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
        def __call__(self): return self.value
    clock = MutableClock()
    catalog = ScheduleCatalog(tmp_path / "runtime.sqlite", clock=clock)
    calls=[]
    def fetcher(url):
        calls.append(url)
        game = _event(event_id="401999501", date="2026-10-05T15:00:00Z")
        return _response(_board(game), url=url, captured_at=clock().isoformat())
    runtime = ScheduleSyncRuntime(catalog, fetcher=fetcher, clock=clock)
    first = runtime.cycle()
    assert len(first) == 1
    assert {url.split("dates=")[1].split("&")[0] for url in calls} == {
        (date(2026, 10, 3) + timedelta(days=i)).strftime("%Y%m%d") for i in range(17)}
    assert catalog.homepage_window(clock())["today_count"] == 1
    clock.value += timedelta(minutes=4)
    resumed = runtime.cycle()
    assert len(resumed) == 1
    assert len(calls) == 34
    for _ in range(9):
        clock.value += timedelta(seconds=30)
        assert runtime.cycle() == []
    clock.value += timedelta(seconds=30)
    second = runtime.cycle()
    assert len(second) == 1
    assert {url.split("dates=")[1].split("&")[0] for url in calls[34:]} == {
        "20261003", "20261004", "20261005", "20261006"}
    clock.value = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)  # Beijing has crossed midnight.
    third = runtime.cycle()
    assert len(third) == 1
    assert {url.split("dates=")[1].split("&")[0] for url in calls[38:]} == {
        (date(2026, 10, 4) + timedelta(days=i)).strftime("%Y%m%d") for i in range(17)}
    assert catalog.count_generation_tasks() == 0


def test_failed_startup_sync_retries_after_five_minutes_without_removing_saved_facts(tmp_path):
    class MutableClock:
        value = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
        def __call__(self): return self.value
    clock=MutableClock()
    catalog=ScheduleCatalog(tmp_path / "retry.sqlite",clock=clock)
    old=_event(event_id="401999701",date="2026-10-05T15:00:00Z")
    catalog.ingest("old",[_response(_board(old),captured_at=clock().isoformat())])
    attempts=[]
    def fetcher(url):
        attempts.append(url)
        if clock.value == datetime(2026, 10, 5, 8, tzinfo=timezone.utc):
            return _response({},url=url,captured_at=clock().isoformat(),status="unavailable")
        return _response(_board(old),url=url,captured_at=clock().isoformat())
    runtime=ScheduleSyncRuntime(catalog,fetcher=fetcher,clock=clock)
    first=runtime.cycle()
    assert first[0]["status"]=="failed"
    assert catalog.get_game("espn:401999701") is not None
    for _ in range(9):
        clock.value+=timedelta(seconds=30)
        assert runtime.cycle()==[]
    clock.value+=timedelta(seconds=30)
    retried=runtime.cycle()
    assert len(retried)==1 and retried[0]["status"]=="success"
    assert len(attempts)==34
    assert catalog.get_game("espn:401999701") is not None
    assert catalog.count_generation_tasks()==0


def test_daily_scoreboards_preserve_partial_receipts_and_do_not_delete_saved_facts(tmp_path):
    catalog = ScheduleCatalog(tmp_path / "daily.sqlite")
    saved = _event(event_id="401999801", date="2026-10-05T15:00:00Z")
    catalog.ingest("old", [_response(_board(saved))])
    calls = []
    def fetcher(url):
        calls.append(url)
        if url.endswith("dates=20261004"):
            return _response({}, url=url, status="unavailable")
        return _response(_board(), url=url)
    result = refresh_scoreboard_window(catalog, fetcher=fetcher, today=date(2026, 10, 5), days_after=1)
    assert result["status"] == "partial"
    assert result["source_count"] == 4 and result["successful_sources"] == 3
    assert all("-" not in url.split("dates=")[1].split("&")[0] for url in calls)
    assert catalog.get_game("espn:401999801") is not None
    assert catalog.count_generation_tasks() == 0


def test_slow_sync_is_not_mistaken_for_sleep_but_real_idle_still_refreshes(tmp_path):
    class Clock:
        value = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
        def __call__(self): return self.value
    clock = Clock()
    catalog = ScheduleCatalog(tmp_path / "slow.sqlite", clock=clock)
    def fetcher(url):
        # A slow batch advances wall time while the runtime itself is active.
        return _response(_board(), url=url, captured_at=clock().isoformat())
    from . import schedule_sync
    original_refresh = schedule_sync.refresh_scoreboard_window
    def slow_refresh(*args, **kwargs):
        result = original_refresh(*args, **kwargs)
        clock.value += timedelta(seconds=120)
        return result
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(schedule_sync, "refresh_scoreboard_window", slow_refresh)
        runtime = ScheduleSyncRuntime(catalog, fetcher=fetcher, clock=clock)
        assert len(runtime.cycle()) == 1
        clock.value += timedelta(seconds=30)
        assert runtime.cycle() == []
        clock.value += timedelta(seconds=90)
        assert len(runtime.cycle()) == 1
    assert catalog.count_generation_tasks() == 0


def test_midnight_window_and_unregistered_reschedule_reclassify_without_ai_tasks(tmp_path):
    class Clock:
        value = datetime(2026, 10, 5, 15, 59, 59, tzinfo=timezone.utc)
        def __call__(self): return self.value
    clock = Clock()
    catalog = ScheduleCatalog(tmp_path / "midnight.sqlite", clock=clock)
    previous = _event(event_id="401999901", date="2026-10-05T15:00:00Z", state="post",
                      status_name="STATUS_FINAL", completed=True, home_score="105", away_score="100")
    next_day = _event(event_id="401999902", date="2026-10-05T16:00:00Z")
    catalog.ingest("initial", [_response(_board(previous, next_day), captured_at=clock().isoformat())])
    before = catalog.homepage_window(clock())
    assert (before["today_count"], before["tomorrow_count"]) == (1, 1)
    clock.value += timedelta(seconds=1)
    after = catalog.homepage_window(clock())
    assert (after["today"], after["tomorrow"]) == ("2026-10-06", "2026-10-07")
    assert [row["game_id"] for row in after["games"]] == ["espn:401999902"]
    moved = _event(event_id="401999902", date="2026-10-06T16:00:00Z")
    catalog.ingest("reschedule", [_response(_board(moved), captured_at=clock().isoformat())])
    revised = catalog.homepage_window(clock())
    assert (revised["today_count"], revised["tomorrow_count"]) == (0, 1)
    assert catalog.get_game("espn:401999902")["schedule_revision"] == 2
    assert catalog.get_game("espn:401999901")["status"] == "final"
    assert catalog.count_generation_tasks() == 0


def test_offline_home_browser_rolls_beijing_day_and_retains_facts_on_read_failure(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    from http.server import SimpleHTTPRequestHandler
    from .export_data import STATIC_ROOT
    node = shutil.which("node")
    playwright = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Local browser dependencies required; this test never downloads them.")
    before = datetime(2026, 10, 5, 15, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=1)
    catalog = ScheduleCatalog(tmp_path / "browser.sqlite", clock=lambda: before)
    service = SimpleNamespace(store=PredictionVersionStore(catalog.path, clock=lambda: before))
    games = [_event(event_id="401999911", date="2026-10-05T15:00:00Z"),
             _event(event_id="401999912", date="2026-10-05T16:00:00Z"),
             _event(event_id="401999913", date="2026-10-06T16:00:00Z")]
    catalog.ingest("fixture", [_response(_board(*games), captured_at=before.isoformat())])
    payloads = [build_schedule_window_response(data={"games": [], "teams": []}, version_service=service,
                                              catalog=catalog, now=at) for at in (before, after)]
    script = r"""
const fs = require('node:fs');
const {chromium} = require(process.argv[1]);
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const results = [];
  try {
    for (const width of [1366, 390]) {
      const ctx = await browser.newContext({viewport:{width,height:844},timezoneId:'America/Los_Angeles'});
      let phase = 0, fail = false, posts = 0;
      await ctx.route('**/*', async route => {
        const req = route.request(), u = new URL(req.url());
        if (req.method() !== 'GET') { posts++; return route.abort(); }
        if (u.hostname !== '127.0.0.1') return route.abort();
        if (['/api/schedule-window','/app_data.json'].includes(u.pathname)) {
          if (fail) return route.abort();
          return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(cfg.payloads[phase])});
        }
        if (u.pathname === '/api/current-analyses') return route.fulfill({status:200,contentType:'application/json',body:'{"status":"success","predictions":{}}'});
        if (u.pathname.startsWith('/api/')) return route.fulfill({status:200,contentType:'application/json',body:'{"status":"unavailable"}'});
        return route.continue();
      });
      const page = await ctx.newPage(), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(cfg.url, {waitUntil:'domcontentloaded'});
      await page.waitForFunction(() => state.data?.schedule_window?.today === '2026-10-05');
      await page.locator('.date-filter-button').nth(1).click();
      await page.waitForFunction(() => state.selectedDate === '2026-10-06');
      phase = 1;
      await page.evaluate(() => refreshScheduleWindow());
      const rolled = await page.evaluate(() => ({today:state.data.schedule_window.today,
        selected:state.selectedDate,pressed:document.querySelector('.date-filter-button[aria-pressed="true"]').dataset.date,
        cards:[...document.querySelectorAll('[data-game-id]')].map(n=>n.dataset.gameId)}));
      await page.locator('.date-filter-button').nth(1).click();
      await page.waitForFunction(() => state.selectedDate === '2026-10-07');
      fail = true;
      await page.evaluate(() => refreshScheduleWindow());
      const retained = await page.evaluate(() => ({selected:state.selectedDate,
        cards:[...document.querySelectorAll('[data-game-id]')].map(n=>n.dataset.gameId),
        overflow:document.documentElement.scrollWidth > innerWidth}));
      results.push({width,rolled,retained,posts,errors});
      await ctx.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify(results));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(STATIC_ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = subprocess.run([node, "-e", script, str(playwright)],
                               input=json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "payloads": payloads}),
                               text=True, encoding="utf-8", capture_output=True, timeout=60)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    assert probe.returncode == 0, probe.stderr
    results = json.loads(probe.stdout)
    assert len(results) == 2
    for result in results:
        assert result["rolled"] == {"today": "2026-10-06", "selected": "2026-10-06", "pressed": "2026-10-06",
                                    "cards": ["espn:401999912"]}
        assert result["retained"] == {"selected": "2026-10-07", "cards": ["espn:401999913"], "overflow": False}
        assert result["posts"] == 0 and result["errors"] == []
    assert catalog.count_generation_tasks() == 0


def test_http_schedule_and_id_routes_return_saved_facts_only(tmp_path, monkeypatch):
    from v2.frontend import server as frontend_server
    from .export_data import STATIC_ROOT

    now = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
    store = PredictionVersionStore(tmp_path / "http.sqlite", clock=lambda: now)
    service = SimpleNamespace(store=store)
    catalog = ScheduleCatalog(store.path, clock=lambda: now)
    event = _event(event_id="401999601", date="2026-10-05T15:00:00Z")
    catalog.ingest("http", [_response(_board(event))])
    monkeypatch.setattr(frontend_server, "_PREDICTION_VERSION_SERVICE", service)
    monkeypatch.setattr(frontend_server, "_SCHEDULE_CATALOG", catalog)
    monkeypatch.setattr(frontend_server, "read_json", lambda _path: {"games": [], "teams": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(frontend_server.Handler, directory=str(STATIC_ROOT)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(root + "/api/schedule-window", timeout=5) as response:
            window = json.loads(response.read())
        with urllib.request.urlopen(root + "/app_data.json", timeout=5) as response:
            compatibility = json.loads(response.read())
        with urllib.request.urlopen(root + "/api/game?game_id=espn:401999601", timeout=5) as response:
            detail = json.loads(response.read())
        with urllib.request.urlopen(root + "/api/current-analyses", timeout=5) as response:
            analyses = json.loads(response.read())
        assert window["schedule_window"]["today"] == "2026-10-05"
        assert [row["id"] for row in window["games"]] == ["espn:401999601"]
        assert compatibility["games"] == window["games"]
        assert detail["game"]["id"] == "espn:401999601"
        assert detail["prediction"]["version"] is None
        assert analyses["predictions"] == {}
        assert catalog.count_generation_tasks() == 0
        db=store.connect()
        try:
            assert db.execute("SELECT COUNT(*) FROM game_prediction_state").fetchone()[0] == 0
        finally: db.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
