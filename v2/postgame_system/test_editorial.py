from __future__ import annotations

import json
import sqlite3

import pytest

from v2.pregame_system.prediction_versions import payload_sha256
from v2.pregame_system.storage import content_hash
from .editorial import (
    _validated_market_reference,
    build_postgame_context,
    build_public_postgame_entry,
    build_public_postgame_payload,
    generate_postgame_editorial,
    load_postgame_editorial,
    load_postgame_skill,
)


GAME_ID = "espn:401898390"
POSTGAME_ID = "postgame-frozen-fixture"
BASE_ID = "pregame-frozen-fixture"
TIPOFF = "2026-10-07T10:00:00+08:00"


class ReadStore:
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db


def _insert_payload(db, table, columns, values):
    placeholders = ",".join("?" for _ in columns)
    db.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
        values,
    )


def fixture_store(tmp_path):
    path = tmp_path / "postgame.sqlite"
    store = ReadStore(path)
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE prediction_versions(
              version_id TEXT, game_id TEXT, payload_json TEXT, payload_sha256 TEXT
            );
            CREATE TABLE game_result_snapshots(
              result_id TEXT, game_id TEXT, payload_json TEXT, payload_sha256 TEXT
            );
            CREATE TABLE postgame_versions(
              version_id TEXT, game_id TEXT, base_prediction_version_id TEXT,
              result_id TEXT, payload_json TEXT, payload_sha256 TEXT, published_at TEXT
            );
        """)
        quote = {
            "status": "available",
            "game_identity": {
                "game_id": GAME_ID,
                "home_team_id": 1610612744,
                "away_team_id": 1610612747,
                "registered_game": True,
            },
            "home_spread": -4.5,
            "away_spread": 4.5,
            "expected_home_margin": 4.5,
            "total": 220.5,
            "source_title": "Draft Kings current odds",
            "sportsbook": "Draft Kings",
            "source_url": "https://example.test/odds",
            "captured_at": "2026-10-07T02:28:50+08:00",
            "observed_at": "2026-10-07T02:28:50+08:00",
            "published_at": None,
        }
        base = {
            "schema_version": "prediction-version-v1",
            "version_id": BASE_ID,
            "game_id": GAME_ID,
            "stage": "EARLY_PREVIEW",
            "published_at": "2026-10-07T02:29:06+08:00",
            "input_cutoff_at": "2026-10-07T02:28:00+08:00",
            "game_identity": {"home_team_id": 1610612744, "away_team_id": 1610612747},
            "judgment": {
                "final_prediction": {
                    "winner_side": "home", "win_probability": 0.55,
                    "expected_margin": 3.0, "expected_total": 223.5,
                },
                "analysis_article": "赛前原始文章。",
                "watch_themes": [],
                "player_analysis": [],
            },
            "public_market_reference": quote,
            "sources": [{"title": "赛前背景", "url": "https://example.test/pregame"}],
        }
        base_hash = payload_sha256(base)
        _insert_payload(
            db, "prediction_versions",
            ("version_id", "game_id", "payload_json", "payload_sha256"),
            (BASE_ID, GAME_ID, json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")), base_hash),
        )
        game = {
            "game_id": GAME_ID,
            "canonical_game_id": GAME_ID,
            "scheduled_tipoff": TIPOFF,
            "game_date": "2026-10-06",
            "season_type": "Preseason",
            "home_team_id": 1610612744,
            "away_team_id": 1610612747,
            "home_team": "Golden State Warriors",
            "away_team": "Los Angeles Lakers",
            "home_team_tricode": "GS",
            "away_team_tricode": "LAL",
            "home_points": 124,
            "away_points": 98,
            "actual_home_margin": 26,
            "actual_total": 222,
        }
        record = {
            "game": game,
            "game_flow": {"period_scores": {"away": [31, 22, 22, 23], "home": [45, 36, 15, 28]}},
            "team_boxscores": [
                {"side": "away", "team_id": 1610612747, "team_tricode": "LAL",
                 "points": 98, "fieldGoalsMade": 33, "fieldGoalsAttempted": 83,
                 "threePointersMade": 13, "threePointersAttempted": 37,
                 "freeThrowsMade": 19, "freeThrowsAttempted": 23, "reboundsTotal": 35,
                 "assists": 20, "turnovers": 22},
                {"side": "home", "team_id": 1610612744, "team_tricode": "GS",
                 "points": 124, "fieldGoalsMade": 41, "fieldGoalsAttempted": 80,
                 "threePointersMade": 15, "threePointersAttempted": 35,
                 "freeThrowsMade": 27, "freeThrowsAttempted": 32, "reboundsTotal": 44,
                 "assists": 32, "turnovers": 23},
            ],
            "key_factors": [],
            "evidence": {"receipts": {"summary": {
                "status": "success", "url": "https://example.test/summary",
                "captured_at": "2026-10-07T12:35:38+08:00",
            }}},
            "prediction_review": {
                "public_prediction": {
                    "prediction": {"expected_margin": 3.0, "expected_total": 223.5, "win_probability": 0.55},
                    "errors": {
                        "predicted_home_win": True, "win_correct": True,
                        "signed_margin_error": -23.0, "absolute_margin_error": 23.0,
                        "signed_total_error": 1.5, "absolute_total_error": 1.5,
                    },
                },
            },
        }
        record["record_hash"] = content_hash(record)
        result = {
            "identity": {
                "canonical_game_id": GAME_ID,
                "home_team_id": 1610612744,
                "away_team_id": 1610612747,
            },
            "game": game,
            "team_boxscores": record["team_boxscores"],
            "player_boxscores": [
                {"team_id": 1610612744, "side": "home", "full_name": "Stephen Curry",
                 "points": 16, "reboundsTotal": 2, "assists": 5, "minutes": 14,
                 "participation_status": "played", "starter": 1},
                {"team_id": 1610612747, "side": "away", "full_name": "Luka Doncic",
                 "points": None, "reboundsTotal": None, "assists": None, "minutes": None,
                 "participation_status": "did_not_play_confirmed", "starter": None},
            ],
            "period_scores": {"away": [31, 22, 22, 23], "home": [45, 36, 15, 28]},
            "receipts": {"summary": {"status": "success"}},
        }
        result["bundle_hash"] = content_hash({
            key: value for key, value in result.items() if key != "receipts"
        })
        result_hash = payload_sha256(result)
        _insert_payload(
            db, "game_result_snapshots",
            ("result_id", "game_id", "payload_json", "payload_sha256"),
            ("result-v1", GAME_ID, json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), result_hash),
        )
        postgame = {
            "schema_version": "postgame-version-v1",
            "game_id": GAME_ID,
            "version_id": POSTGAME_ID,
            "base_prediction_version_id": BASE_ID,
            "base_prediction_payload_sha256": base_hash,
            "result_id": "result-v1",
            "base_stage": "EARLY_PREVIEW",
            "record": record,
            "pregame_plan": {
                "key_matchup": "赛前关键对位判断。",
                "player_usage": [{"player_name": "Stephen Curry", "side": "home",
                                  "minutes_min": 14, "minutes_max": 22}],
            },
            "minutes_review": [{
                "player_name": "Stephen Curry", "side": "home",
                "estimated_minutes": [14, 22], "actual_minutes": 14,
                "participation_status": "played",
            }],
            "scoring_decomposition": [
                {"side": "away", "field_goal_points": 79, "free_throw_points": 19, "bench_points": 51},
                {"side": "home", "field_goal_points": 97, "free_throw_points": 27, "bench_points": 65},
            ],
            "unknown_before_game": [
                {"section": "research.availability", "status": "UNKNOWN"},
                {"section": "research.confirmed_starter", "status": "UNKNOWN"},
                {"section": "research.minutes_restriction", "status": "UNKNOWN"},
            ],
            "display_names": [{
                "canonical_name_en": "Stephen Curry",
                "display_name_zh": "斯蒂芬·库里",
            }],
            "published_at": "2026-10-07T12:35:38+08:00",
        }
        post_hash = payload_sha256(postgame)
        _insert_payload(
            db, "postgame_versions",
            ("version_id", "game_id", "base_prediction_version_id", "result_id",
             "payload_json", "payload_sha256", "published_at"),
            (POSTGAME_ID, GAME_ID, BASE_ID, "result-v1",
             json.dumps(postgame, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             post_hash, postgame["published_at"]),
        )
    return store


def test_context_uses_only_verified_same_game_frozen_inputs(tmp_path):
    store = fixture_store(tmp_path)
    context = build_postgame_context(store, GAME_ID, postgame_version_id=POSTGAME_ID)
    assert context["game"]["home_score"] == 124 and context["game"]["away_score"] == 98
    assert context["scoring_decomposition"][1]["bench_points"] == 65
    assert len(context["pregame"]["unverified_before_tipoff"]) == 3
    assert context["prediction"]["expected_home_margin"] == 3.0
    assert context["prediction"]["expected_total"] == 223.5
    assert context["public_market_reference_review"]["spread"]["lean_side"] == "away"
    assert context["public_market_reference_review"]["spread"]["settlement"] == "loss"
    assert context["public_market_reference_review"]["total"]["lean"] == "over"
    assert context["public_market_reference_review"]["total"]["settlement"] == "win"
    assert any(row["name"] == "斯蒂芬·库里" for row in context["key_players"])
    assert context["sources"][0]["label"] == "比赛结果与球员数据"
    assert "record" not in context and "evaluation" not in context


def test_late_or_wrong_identity_market_quote_is_not_used():
    from .editorial import _validated_market_reference
    base = {
        "game_id": GAME_ID,
        "public_market_reference": {
            "status": "available",
            "game_identity": {"game_id": GAME_ID, "home_team_id": 1610612744,
                              "away_team_id": 1610612747, "registered_game": True},
            "home_spread": -4.5,
            "total": 220.5,
            "source_url": "https://example.test/odds",
            "captured_at": "2026-10-08T00:00:00+08:00",
        },
    }
    game = {"home_team_id": 1610612744, "away_team_id": 1610612747,
            "scheduled_tipoff": TIPOFF}
    assert _validated_market_reference(base, game) is None
    base["public_market_reference"]["captured_at"] = "2026-10-07T01:00:00+08:00"
    base["public_market_reference"]["game_identity"]["away_team_id"] = 1610612748
    assert _validated_market_reference(base, game) is None


def test_one_writer_call_creates_immutable_version_bound_article_and_public_projection(tmp_path):
    store = fixture_store(tmp_path)
    output_root = tmp_path / "editorials"
    skill_path = tmp_path / "postgame-skill.md"
    skill_path.write_text("skill version one", encoding="utf-8")
    calls = []

    def client(messages, key, model, timeout):
        calls.append(messages)
        assert "skill version one" in messages[0]["content"]
        assert "skill version one" not in messages[1]["content"]
        assert "2026-10-07" in messages[1]["content"]
        assert "market_benchmark" not in messages[1]["content"]
        return json.dumps({
            "title": "双星缺阵添变数\n勇士主场定乾坤",
            "short_review": "胜负方向看对了，但湖人两名预计核心最终缺阵，勇士把分差拉到了26分。",
            "article": "第一节和第二节比分分别是45比31、36比22，勇士半场领先28分。"
                       "\n\n赛前预计库里赢3分的判断命中方向，但实际差距来到26分。东契奇和里夫斯赛后记录为DNP，"
                       "这意味着赛前设想的湖人后场组合没有出现在比赛里；赛前并未确认他们会打满或一定出场。"
                       "\n\n冻结早盘参考是勇士让4.5分、总分220.5。赛前让分方向偏湖人+4.5，最终未命中；"
                       "总分偏大，实际222分，高于220.5。预测总分223.5与实际只差1.5分，但比分分配完全不同。",
        }, ensure_ascii=False)

    before_prediction, before_postgame = None, None
    with store.connect() as db:
        before_prediction = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (BASE_ID,)
        ).fetchone())
        before_postgame = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?", (POSTGAME_ID,)
        ).fetchone())
    result = generate_postgame_editorial(
        store, GAME_ID, postgame_version_id=POSTGAME_ID, api_client=client,
        api_key="fixture-key", output_root=output_root, skill_path=skill_path,
    )
    assert result.status == "success"
    assert len(calls) == 1
    article = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert article["schema_version"] == "postgame-editorial-v1"
    assert article["base_prediction_version_id"] == BASE_ID
    assert article["base_prediction_payload_sha256"] == before_prediction[1]
    skill_path.write_text("skill version two", encoding="utf-8")
    assert load_postgame_skill(skill_path) == "skill version two"
    again = generate_postgame_editorial(
        store, GAME_ID, postgame_version_id=POSTGAME_ID, api_client=client,
        api_key="fixture-key", output_root=output_root, skill_path=skill_path,
    )
    assert again.status == "already_exists"
    assert len(calls) == 1

    db = store.connect()
    try:
        saved_postgame = json.loads(db.execute(
            "SELECT payload_json FROM postgame_versions"
        ).fetchone()[0])
    finally:
        db.close()
    entry = build_public_postgame_entry(store, saved_postgame, output_root=output_root)
    assert entry["status"] == "available"
    assert entry["score_text"] == "湖人 98 — 124 勇士"
    assert entry["summary"] == article["editorial"]["short_review"]
    public = build_public_postgame_payload(store, saved_postgame, output_root=output_root)
    assert public["title"] == article["editorial"]["title"]
    assert public["market_reference"]["home_spread"] == -4.5
    assert public["market_reference"]["spread_review"]["settlement"] == "loss"
    assert public["market_reference"]["total_review"]["settlement"] == "win"
    assert not ({"record", "evaluation", "base_stage", "payload_sha256", "postgame_record_hash"} & set(public))
    with store.connect() as db:
        after_prediction = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (BASE_ID,)
        ).fetchone())
        after_postgame = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?", (POSTGAME_ID,)
        ).fetchone())
    assert after_prediction == before_prediction
    assert after_postgame == before_postgame


def test_writer_rejects_tampered_companion_artifact(tmp_path):
    store = fixture_store(tmp_path)
    out = tmp_path / "editorials"
    skill = tmp_path / "skill.md"
    skill.write_text("skill", encoding="utf-8")
    response = json.dumps({"title":"双星缺阵添变数\n勇士主场定乾坤","short_review":"短评","article":"正文一段。"}, ensure_ascii=False)
    generated = generate_postgame_editorial(
        store, GAME_ID, api_client=lambda *args: response, api_key="fixture-key",
        output_root=out, skill_path=skill,
    )
    value = json.loads(generated.artifact_path.read_text(encoding="utf-8"))
    value["editorial"]["article"] = "被改写"
    generated.artifact_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_binding_or_hash"):
        build_public_postgame_payload(
            store, json.loads(store.connect().execute(
                "SELECT payload_json FROM postgame_versions"
            ).fetchone()[0]), output_root=out,
        )


def test_manual_postgame_publication_sidecar_switches_reader_without_mutating_frozen_rows(tmp_path):
    store = fixture_store(tmp_path)
    out = tmp_path / "editorials"
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("fixture skill", encoding="utf-8")
    response = json.dumps({
        "title": "双星缺阵添变数\n勇士主场定乾坤",
        "short_review": "Writer 原稿短评",
        "article": "Writer 原稿正文。",
    }, ensure_ascii=False)
    generated = generate_postgame_editorial(
        store, GAME_ID, api_client=lambda *args: response, api_key="fixture-key",
        output_root=out, skill_path=skill_path,
    )
    assert generated.status == "success"
    original_bytes = generated.artifact_path.read_bytes()
    with store.connect() as db:
        saved_postgame = json.loads(db.execute(
            "SELECT payload_json FROM postgame_versions WHERE version_id=?", (POSTGAME_ID,)
        ).fetchone()[0])
        before_prediction = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (BASE_ID,)
        ).fetchone())
        before_postgame = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?", (POSTGAME_ID,)
        ).fetchone())

    from v2.postgame_system.editorial import MANUAL_PUBLICATION_SCHEMA

    manual = {
        "schema_version": MANUAL_PUBLICATION_SCHEMA,
        "game_id": GAME_ID,
        "postgame_version_id": POSTGAME_ID,
        "postgame_record_hash": saved_postgame["record"]["record_hash"],
        "base_prediction_version_id": BASE_ID,
        "base_prediction_payload_sha256": saved_postgame["base_prediction_payload_sha256"],
        "source_shadow_artifact_sha256": "a" * 64,
        "editorial": {
            "title": "人工正式标题",
            "short_review": "人工正式短评。",
            "article": "人工正式正文。",
        },
        "sources": [],
        "published_at": "2026-10-09T12:00:00+08:00",
    }
    manual["artifact_sha256"] = content_hash(manual)
    manual_path = out / "manual_publications" / f"{POSTGAME_ID}.json"
    manual_path.parent.mkdir(parents=True)
    manual_path.write_text(json.dumps(manual, ensure_ascii=False, indent=2), encoding="utf-8")

    selected = load_postgame_editorial(saved_postgame, output_root=out)
    assert selected["editorial"]["title"] == "人工正式标题"
    public = build_public_postgame_payload(store, saved_postgame, output_root=out)
    entry = build_public_postgame_entry(store, saved_postgame, output_root=out)
    assert public["title"] == "人工正式标题"
    assert public["short_review"] == "人工正式短评。"
    assert public["article"] == "人工正式正文。"
    assert entry["summary"] == "人工正式短评。"
    assert generated.artifact_path.read_bytes() == original_bytes
    with store.connect() as db:
        after_prediction = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (BASE_ID,)
        ).fetchone())
        after_postgame = tuple(db.execute(
            "SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?", (POSTGAME_ID,)
        ).fetchone())
    assert after_prediction == before_prediction
    assert after_postgame == before_postgame

    manual["editorial"]["article"] = "被篡改的正文。"
    manual_path.write_text(json.dumps(manual, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="manual_publication_binding_or_hash"):
        load_postgame_editorial(saved_postgame, output_root=out)
