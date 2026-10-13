from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest
import v2.pregame_system.forecasting_pilot as pilot_module

from v2.pregame_system.forecasting_pilot import (
    build_forecast_messages,
    build_forecast_packet,
    build_packets_from_rows,
    CsvOutcomeProvider,
    evaluate_forecasts,
    forecast_one,
    run_forecast_batch,
    packet_content_sha256,
    select_target_games,
    validate_forecast,
    write_evaluation_artifacts,
)


TARGETS = {
    1: ("hou", "okc", "home", 6.5, 225.5),
    21: ("okc", "por", "away", 11.5, 234.0),
    41: ("sa", "okc", "home", 8.5, 230.5),
    61: ("den", "okc", "home", 6.5, 235.5),
    82: ("phx", "okc", "home", 7.5, 213.0),
}


def make_rows(count: int = 82) -> list[dict[str, str]]:
    first_day = date(2025, 10, 21)
    opponents = ["hou", "por", "sa", "den", "phx"]
    rows: list[dict[str, str]] = []
    for index in range(count):
        number = index + 1
        away, home = ("okc", opponents[index % len(opponents)]) if index % 2 else (opponents[index % len(opponents)], "okc")
        favorite_side = "home" if number % 3 else "away"
        spread = 5.5
        total = 221.5
        if number in TARGETS:
            away, home, favorite_side, spread, total = TARGETS[number]
        rows.append(
            {
                "season": "2026",
                "date": (first_day + timedelta(days=index)).isoformat(),
                "regular": "True",
                "playoffs": "False",
                "away": away,
                "home": home,
                "score_away": str(102 + (index % 13)),
                "score_home": str(98 + (index % 11)),
                "q1_away": "25",
                "q1_home": "24",
                "whos_favored": favorite_side,
                "spread": str(spread),
                "total": str(total),
                "moneyline_away": "",
                "moneyline_home": "",
            }
        )
    return rows


def selected_packet(rows: list[dict[str, str]], game_number: int) -> dict:
    target = next(
        row for row in select_target_games(rows) if row["game_number"] == game_number
    )
    return build_forecast_packet(rows, target, game_number, "a" * 64)


def packet_ids(packets: list[dict]) -> list[str]:
    return [packet["target"]["game_id"] for packet in packets]


def standard_predictions(packets: list[dict]) -> list[dict]:
    output = []
    for packet in packets:
        target = packet["target"]
        output.append(
            {
                "game_id": target["game_id"],
                "packet_sha256": packet["packet_sha256"],
                "prediction": {
                    "winner": target["home"]["abbr"],
                    "win_probability": 0.62,
                    "expected_margin": 8.0,
                    "expected_total": 230.0,
                },
                "independent_prediction": {
                    "winner": target["home"]["abbr"],
                    "win_probability": 0.62,
                    "expected_margin": 8.0,
                    "expected_total": 230.0,
                },
            }
        )
    return output


def collect_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).casefold())
            keys.update(collect_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(collect_keys(child))
    return keys


def test_okc_schedule_is_exactly_82_and_fixed_ordinals_are_stable():
    rows = make_rows()
    selected = select_target_games(rows)
    assert len(selected) == 5
    assert [row["game_number"] for row in selected] == [1, 21, 41, 61, 82]
    assert [row["date"] for row in selected] == [
        "2025-10-21",
        "2025-11-10",
        "2025-11-30",
        "2025-12-20",
        "2026-01-10",
    ]
    assert [row["game_id"] for row in selected] == [
        "OKC-2025-26-REG-001",
        "OKC-2025-26-REG-021",
        "OKC-2025-26-REG-041",
        "OKC-2025-26-REG-061",
        "OKC-2025-26-REG-082",
    ]
    reverse_selected = select_target_games(list(reversed(rows)))
    assert [(row["game_number"], row["date"], row["home"], row["away"])
            for row in reverse_selected] == [
        (row["game_number"], row["date"], row["home"], row["away"])
        for row in selected
    ]


def test_schedule_count_must_be_82_before_ordinal_selection():
    with pytest.raises(ValueError, match="schedule_count"):
        select_target_games(make_rows(81))


def test_target_and_future_results_do_not_change_packet_recent_form_or_h2h():
    rows = make_rows()
    before = selected_packet(rows, 21)
    mutated = copy.deepcopy(rows)
    for row in mutated[20:]:
        row["score_home"] = "0"
        row["score_away"] = "200"
        row["q1_home"] = "0"
        row["q1_away"] = "50"
    after = selected_packet(mutated, 21)
    assert before == after
    assert before["team_state_before"]["okc"]["record"]["games_played"] == 20


def test_packets_exclude_target_outcome_and_postgame_fields():
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64)
    forbidden = {
        "score_home",
        "score_away",
        "home_score",
        "away_score",
        "q1_home",
        "q1_away",
        "ot_home",
        "ot_away",
        "actual_winner",
        "actual_margin",
        "actual_total",
        "ats_result",
        "ou_result",
        "postgame_stats",
        "postgame_news",
    }
    assert len(packets) == 5
    for packet in packets:
        assert not (collect_keys(packet) & forbidden)
        baseline = packet["quantitative_baselines"]
        assert baseline["pure"]["status"] == "unavailable"
        assert baseline["residual"]["status"] == "unavailable"
        assert baseline["official_numeric"]["status"] == "unavailable"
        assert packet["pregame_context"]["injury"]["status"] == "unavailable"
        assert packet["pregame_context"]["roster"]["status"] == "unavailable"
        assert packet["pregame_context"]["starters"]["status"] == "unavailable"
        assert packet["pregame_context"]["news"]["status"] == "unavailable"


def test_target_and_future_score_sentinels_never_appear_in_packet_json():
    rows = make_rows()
    rows[20]["score_home"] = "9876"
    rows[20]["score_away"] = "8765"
    rows[20]["q1_home"] = "7654"
    packet_text = json.dumps(selected_packet(rows, 21), ensure_ascii=False)
    assert "9876" not in packet_text
    assert "8765" not in packet_text
    assert "7654" not in packet_text


def test_historical_dataset_team_codes_resolve_to_team_names():
    rows = make_rows()
    rows[18]["away"], rows[18]["home"] = "okc", "utah"
    rows[19]["away"], rows[19]["home"] = "gs", "okc"
    packet = selected_packet(rows, 21)
    names = [
        game["opponent"]["name"]
        for game in packet["team_state_before"]["okc"]["recent_form"]["games"]
    ]
    assert "Utah Jazz" in names
    assert "Golden State Warriors" in names


def test_market_favorite_maps_to_signed_home_line_and_missing_moneyline_stays_unavailable():
    rows = make_rows()
    home_favorite = selected_packet(rows, 1)["market_reference"]
    away_favorite = selected_packet(rows, 21)["market_reference"]
    assert home_favorite["favorite_side"] == "home"
    assert home_favorite["market_expected_home_margin"] == pytest.approx(6.5)
    assert home_favorite["home_handicap_points"] == pytest.approx(-6.5)
    assert away_favorite["favorite_side"] == "away"
    assert away_favorite["market_expected_home_margin"] == pytest.approx(-11.5)
    assert away_favorite["home_handicap_points"] == pytest.approx(11.5)
    assert home_favorite["moneyline"]["status"] == "unavailable"
    assert home_favorite["moneyline"]["home"] is None
    assert home_favorite["moneyline"]["away"] is None
    assert home_favorite["quote_timestamp"] is None


def test_forecast_messages_use_only_forecasting_skill_and_one_packet():
    packet = selected_packet(make_rows(), 1)
    messages = build_forecast_messages(packet, "FORECASTING ONLY")
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": "FORECASTING ONLY"}
    assert messages[1]["role"] == "user"
    user_content = json.loads(messages[1]["content"])
    assert user_content == packet
    assert "Editorial Skill" not in json.dumps(messages, ensure_ascii=False)


def test_forecast_runner_uses_only_skill_and_packet_with_fake_client(tmp_path):
    packet = selected_packet(make_rows(), 1)
    captured = []

    def fake_request(messages, api_key, model, max_tokens, *, timeout_seconds):
        captured.append((messages, api_key, model, max_tokens, timeout_seconds))
        value = {"prediction": {"winner_side": "home", "win_probability": 0.61,
            "expected_home_margin": 4.5, "expected_total": 229.5},
            "factors": [{"classification": "inference", "evidence_ids": ["model.pure"],
                "effect": "home", "summary": "Limited prior basketball evidence."}], "uncertainties": []}
        if len(captured) == 2:
            value['factors'][0]['evidence_ids'].append('market.reference')
            value.update(decision="keep", market_assessment="unclear", rationale="Keep the saved basketball judgment.")
        return json.dumps(value)

    result = forecast_one(
        packet,
        fake_request,
        "test-key",
        skill_loader=lambda: "FORECASTING ONLY",
        audit_dir=tmp_path / "stages",
    )
    assert result["winner"] == "Oklahoma City Thunder"
    assert len(captured) == 2
    messages = captured[0][0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"].startswith("FORECASTING ONLY")
    assert "market_reference" not in messages[1]["content"]
    assert "independent" in json.loads(captured[1][0][1]["content"])
    assert "Editorial Skill" not in json.dumps(messages, ensure_ascii=False)


def test_forecast_output_requires_exact_schema_and_target_team():
    packet = selected_packet(make_rows(), 1)
    valid = {
        "winner": "okc",
        "win_probability": 0.61,
        "expected_margin": 4.5,
        "expected_total": 229.5,
    }
    assert validate_forecast(packet, valid) == valid
    with pytest.raises(ValueError, match="winner"):
        validate_forecast(packet, {**valid, "winner": "lal"})
    with pytest.raises(ValueError, match="probability"):
        validate_forecast(packet, {**valid, "win_probability": 1.01})
    with pytest.raises(ValueError, match="schema"):
        validate_forecast(packet, {**valid, "article": "not allowed"})


def test_evaluator_waits_for_all_five_valid_forecasts_before_reading_any_result():
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64)
    forecasts = standard_predictions(packets)
    forecasts[-1]["prediction"]["winner"] = "unknown"
    calls: list[str] = []
    with pytest.raises(ValueError):
        evaluate_forecasts(
            packets,
            forecasts,
            lambda game_id: calls.append(game_id) or pytest.fail("outcome read before validation"),
        )
    assert calls == []


def test_evaluator_scores_winner_margin_total_ats_and_over_under_after_predictions():
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64)
    forecasts = standard_predictions(packets)
    forecasts[0]["prediction"]["expected_total"] = 235.0
    forecasts[1]["prediction"].update(
        winner="okc",
        expected_margin=-14.0,
    )
    events: list[str] = []
    actuals = {
        packets[0]["target"]["game_id"]: {"home_score": 120, "away_score": 110},
        packets[1]["target"]["game_id"]: {"home_score": 100, "away_score": 112},
        packets[2]["target"]["game_id"]: {"home_score": 120, "away_score": 100},
        packets[3]["target"]["game_id"]: {"home_score": 220, "away_score": 150},
    }
    by_id = {packet["packet_id"]: packet for packet in packets}

    def outcome_provider(game_id):
        events.append(game_id)
        target = by_id[game_id]["target"]
        return {
            "game_id": game_id,
            "home": target["home"]["abbr"],
            "away": target["away"]["abbr"],
            **actuals.get(game_id, {"home_score": 100, "away_score": 99}),
        }

    report = evaluate_forecasts(packets, forecasts, outcome_provider)
    assert events == packet_ids(packets)
    assert report["evaluation_order"] == "all_forecasts_validated_before_first_outcome_read"
    game1 = report["games"][0]
    assert game1["metrics"]["winner_correct"] is True
    assert game1["metrics"]["margin_absolute_error"] == pytest.approx(2.0)
    assert game1["metrics"]["total_absolute_error"] == pytest.approx(5.0)
    assert game1["metrics"]["ats"]["pick"] == "home"
    assert game1["metrics"]["ats"]["result"] == "win"
    assert game1["metrics"]["over_under"]["pick"] == "over"
    assert game1["metrics"]["over_under"]["result"] == "win"
    assert game1["independent_metrics"]["winner_correct"] is True
    assert game1["independent_metrics"]["margin_absolute_error"] == pytest.approx(2.0)
    assert game1["market_reference_metrics"]["margin_absolute_error"] == pytest.approx(3.5)
    assert game1["market_reference_metrics"]["total_absolute_error"] == pytest.approx(4.5)
    game2 = report["games"][1]
    assert game2["metrics"]["winner_correct"] is True
    assert game2["metrics"]["ats"]["pick"] == "away"
    assert game2["metrics"]["ats"]["result"] == "win"
    assert game2["metrics"]["over_under"]["pick"] == "under"
    assert game2["metrics"]["over_under"]["result"] == "win"
    game3 = report["games"][2]
    assert game3["metrics"]["ats"]["pick"] == "away"
    assert game3["metrics"]["ats"]["result"] == "loss"
    game4 = report["games"][3]
    assert game4["metrics"]["over_under"]["pick"] == "under"
    assert game4["metrics"]["over_under"]["result"] == "loss"


def test_ats_and_total_pushes_are_preserved_and_saved_separately(tmp_path):
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64)
    packets = copy.deepcopy(packets)
    packets[0]["market_reference"]["spread_points"] = 6.0
    packets[0]["market_reference"]["market_expected_home_margin"] = 6.0
    packets[0]["market_reference"]["home_handicap_points"] = -6.0
    packets[0]["market_reference"]["total_line_points"] = 220.0
    packets[0]["packet_sha256"] = packet_content_sha256(packets[0])
    forecasts = standard_predictions(packets)
    forecasts[0]["prediction"]["expected_margin"] = 8.0
    forecasts[0]["prediction"]["expected_total"] = 230.0
    by_id = {packet["packet_id"]: packet for packet in packets}

    def push_outcome(game_id):
        target = by_id[game_id]["target"]
        scores = (113, 107) if game_id == packets[0]["target"]["game_id"] else (100, 99)
        return {
            "game_id": game_id,
            "home": target["home"]["abbr"],
            "away": target["away"]["abbr"],
            "home_score": scores[0],
            "away_score": scores[1],
        }

    report = evaluate_forecasts(packets, forecasts, push_outcome)
    first = report["games"][0]
    assert first["metrics"]["ats"]["result"] == "push"
    assert first["metrics"]["ats"]["actual_settlement"] == "push"
    assert first["metrics"]["over_under"]["result"] == "push"
    assert first["metrics"]["over_under"]["actual_settlement"] == "push"

    result_paths = write_evaluation_artifacts(report, tmp_path)
    actuals = json.loads(result_paths["actual_results"].read_text(encoding="utf-8"))
    evaluation = json.loads(result_paths["evaluation"].read_text(encoding="utf-8"))
    assert len(actuals["games"]) == 5
    assert "home_score" in actuals["games"][0]
    assert "home_score" not in json.dumps(evaluation)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_forecast_values_are_rejected(bad):
    packet = selected_packet(make_rows(), 1)
    valid = {
        "winner": "okc",
        "win_probability": 0.61,
        "expected_margin": 4.5,
        "expected_total": 229.5,
    }
    with pytest.raises(ValueError):
        validate_forecast(packet, {**valid, "expected_margin": bad})


def _fake_two_stage_response(schema):
    value = {
        "prediction": {"winner_side": "home", "win_probability": 0.61,
                       "expected_home_margin": 3.5, "expected_total": 220.5},
        "factors": [{"classification": "inference", "evidence_ids": ["model.pure"],
                     "effect": "home", "summary": "Limited prior basketball evidence."}],
        "uncertainties": [],
    }
    if schema.__name__ == "MarketReviewForecast":
        value["factors"][0]["evidence_ids"].append("market.reference")
        value.update(decision="keep", market_assessment="supports",
                     rationale="Market reference is consistent with the independent estimate.")
    return json.dumps(value)


def _three_packets():
    rows = make_rows()
    return build_packets_from_rows(rows, "test.csv", "a" * 64, (1, 2, 3))


def test_all_82_schedule_ordinals_are_available_and_stable():
    rows = make_rows()
    forward = select_target_games(rows, range(1, 83))
    reverse = select_target_games(list(reversed(rows)), range(1, 83))
    assert len(forward) == 82
    assert [row["game_number"] for row in forward] == list(range(1, 83))
    assert [(row["game_id"], row["date"], row["home"], row["away"]) for row in forward] == [
        (row["game_id"], row["date"], row["home"], row["away"]) for row in reverse
    ]


def test_82_packets_build_with_no_target_or_future_result_fields():
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64, range(1, 83))
    assert len(packets) == 82
    forbidden = {"score_home", "score_away", "home_score", "away_score", "q1_home", "q1_away",
                 "actual_winner", "actual_margin", "actual_total", "ats_result", "ou_result"}
    assert all(not (collect_keys(packet) & forbidden) for packet in packets)


def test_evaluator_validates_all_82_forecasts_before_reading_any_outcome(tmp_path):
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64, range(1, 83))
    forecasts = standard_predictions(packets)
    forecasts[-1]["prediction"]["winner"] = "not-a-team"
    calls = []
    with pytest.raises(ValueError):
        evaluate_forecasts(packets, forecasts,
                           lambda game_id: calls.append(game_id) or pytest.fail("outcome read early"))
    assert calls == []


def test_evaluator_and_artifact_writer_accept_82_games(tmp_path):
    packets = build_packets_from_rows(make_rows(), "test.csv", "a" * 64, range(1, 83))
    forecasts = standard_predictions(packets)
    by_id = {packet["packet_id"]: packet for packet in packets}

    def actual(game_id):
        target = by_id[game_id]["target"]
        return {"game_id": game_id, "date": target["date"],
                "home": target["home"]["abbr"], "away": target["away"]["abbr"],
                "home_score": 110, "away_score": 100}

    report = evaluate_forecasts(packets, forecasts, actual)
    assert len(report["games"]) == 82
    paths = write_evaluation_artifacts(report, tmp_path / "evaluation-82")
    actuals = json.loads(paths["actual_results"].read_text(encoding="utf-8"))
    evaluation = json.loads(paths["evaluation"].read_text(encoding="utf-8"))
    assert len(actuals["games"]) == len(evaluation["games"]) == 82
    assert "home_score" not in json.dumps(evaluation)


def test_csv_outcome_provider_is_lazy_and_can_resolve_any_season_ordinal(monkeypatch):
    rows = make_rows()
    reads = []
    monkeypatch.setattr(pilot_module, "_read_verified_csv",
                        lambda path: reads.append(path) or (rows, "a" * 64))
    provider = CsvOutcomeProvider("test.csv")
    assert reads == []
    target = select_target_games(rows, (82,))[0]
    actual = provider(target["game_id"])
    assert len(reads) == 1
    assert actual["game_id"] == target["game_id"]
    assert actual["date"] == target["date"]
    assert actual["home"] == target["home"]
    assert actual["away"] == target["away"]
    assert "home_score" in actual and "away_score" in actual


def test_batch_checkpoint_resumes_only_incomplete_games_in_new_output_dir(tmp_path):
    packets = _three_packets()
    checkpoint_dir = tmp_path / "checkpoints"
    first_calls = []
    resumed_calls = []
    state = {"fail": True}

    def request(messages, api_key, model, max_tokens, *, timeout_seconds):
        if state["fail"]:
            first_calls.append(len(first_calls) + 1)
        else:
            resumed_calls.append((model, max_tokens, timeout_seconds))
        if state["fail"] and len(first_calls) == 5:
            raise RuntimeError("simulated interruption")
        return _fake_two_stage_response_for_message(messages)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_forecast_batch(packets, tmp_path / "first" / "predictions.json",
                           request, "test-key", lambda: "FORECASTING ONLY",
                           checkpoint_dir=checkpoint_dir)
    assert len(first_calls) == 5
    assert len(list((checkpoint_dir / "game_001").glob("*/success.json"))) == 1
    assert len(list((checkpoint_dir / "game_002").glob("*/success.json"))) == 1
    assert not list((checkpoint_dir / "game_003").glob("*/success.json"))

    state["fail"] = False
    result = run_forecast_batch(packets, tmp_path / "second" / "predictions.json",
                                request, "test-key", lambda: "FORECASTING ONLY",
                                checkpoint_dir=checkpoint_dir)
    assert len(result["predictions"]) == 3
    assert len(resumed_calls) == 2

    # The same output path can be reopened only when its exact input/config set matches.
    reused = run_forecast_batch(packets, tmp_path / "second" / "predictions.json",
                                request,
                                "test-key", lambda: "FORECASTING ONLY", checkpoint_dir=checkpoint_dir)
    assert reused["run_fingerprint"] == result["run_fingerprint"]


def _fake_two_stage_response_for_message(messages):
    request = json.loads(messages[1]["content"])
    schema = (type("MarketReviewForecast", (), {})
              if "independent" in request and "market_context" in request
              else type("IndependentForecast", (), {}))
    return _fake_two_stage_response(schema)


def test_checkpoint_invalidates_on_packet_skill_and_model_setting_changes(tmp_path, monkeypatch):
    packet = selected_packet(make_rows(), 1)
    checkpoint_dir = tmp_path / "checkpoints"
    calls = []

    def success(messages, api_key, model, max_tokens, *, timeout_seconds):
        calls.append((model, max_tokens))
        return _fake_two_stage_response_for_message(messages)

    def execute(packet_value, name, skill):
        return run_forecast_batch([packet_value], tmp_path / name / "predictions.json",
                                  success, "test-key", lambda: skill,
                                  checkpoint_dir=checkpoint_dir)

    execute(packet, "base", "FORECASTING A")
    assert len(calls) == 2

    changed_packet = json.loads(json.dumps(packet))
    changed_packet["market_reference"]["total_line_points"] += 0.5
    changed_packet["packet_sha256"] = packet_content_sha256(changed_packet)
    execute(changed_packet, "changed-input", "FORECASTING A")
    assert len(calls) == 4

    execute(changed_packet, "changed-skill", "FORECASTING B")
    assert len(calls) == 6

    monkeypatch.setattr(pilot_module, "DEEPSEEK_MAX_TOKENS", 2399)
    execute(changed_packet, "changed-settings", "FORECASTING B")
    assert len(calls) == 8
    execute(changed_packet, "changed-settings-reuse", "FORECASTING B")
    assert len(calls) == 8


def test_corrupt_success_checkpoint_fails_closed_without_model_call(tmp_path):
    packet = selected_packet(make_rows(), 1)
    checkpoint_dir = tmp_path / "checkpoints"
    calls = []

    def success(messages, api_key, model, max_tokens, *, timeout_seconds):
        calls.append(1)
        return _fake_two_stage_response_for_message(messages)

    run_forecast_batch([packet], tmp_path / "first.json", success, "test-key",
                       lambda: "FORECASTING ONLY", checkpoint_dir=checkpoint_dir)
    receipt_path = next((checkpoint_dir / "game_001").glob("*/success.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["receipt_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = len(calls)
    with pytest.raises(ValueError, match="receipt_hash_mismatch"):
        run_forecast_batch([packet], tmp_path / "second.json", success, "test-key",
                           lambda: "FORECASTING ONLY", checkpoint_dir=checkpoint_dir)
    assert len(calls) == before
