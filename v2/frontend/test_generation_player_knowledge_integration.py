"""Offline acceptance of registration -> real player context -> frozen report -> UI."""
from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from v2.pregame_system.analyst import EvidenceBoundAnalyst
from v2.pregame_system.editorial_test_helpers import protocol_reviewer
from v2.pregame_system.player_knowledge import DEFAULT_DATABASE as PLAYER_DB, PlayerKnowledgeStore
from v2.pregame_system.prediction_versions import PredictionVersionStore
from v2.pregame_system.test_player_knowledge import boxscore, player

from . import analysis_service, server
from .analysis_service import SavedGameAnalysisService, prediction_layers
from .export_data import DEFAULT_OUTPUT, STATIC_ROOT, normalize_availability
from .generation_input import prepare_generation_data
from .prediction_version_service import PredictionVersionService
from .pregame_scheduler import PregameScheduler
from .test_prediction_version_restore import NODE_FRONTEND_PROBE
from .test_pregame_scheduler import no_external_services  # autouse: only loopback HTTP is allowed

NY, PHI, BOS = 1610612752, 1610612755, 1610612738
JB, MB, TM, JE = "1628973", "1628969", "1630178", "203954"
NEW_NY, NEW_PHI, FOREIGN = "90000001", "90000002", "90000003"
TIP = datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
ARTICLE = (
    "本页是离线集成验收样例，预测数字和文章由测试替身提供，不作为真实比赛报告。"
    "验收首先关注数据从哪里来，以及当前比赛双方能否被稳定识别。球队历史表现、名单和球员摘要各自保留来源，"
    "进入分析上下文后仍然能找到对应的证据。当前名单与上一赛季统计属于不同时间维度，不能互相替代。"
    "球员有没有历史记录，与他是否在当前名单中出现，应当分别保存；有合格来源的出战状态也应独立保留。"
    "在这个样例里，第25秒选择主队方向，但这个方向只是用于检验输出协议的固定测试判断。"
    "报告发布时，数字和正文共同归入一个版本，之后读取页面只能使用这一份版本中的内容。"
    "如果后续数据库加入更多资料，旧报告仍然应当保留当时读取的球员知识，而不能在展示时偷偷换成新资料。"
    "下一阶段正在运行时，页面继续读取上一份成功结果；下一阶段成功后，才整体切换到新报告。"
    "失败、名单缺失或球员历史查询异常，都必须留下能够核对的状态，不能靠补写信息让报告看起来完整。"
    "这次验收只检验链路和证据边界，所有外部研究和生成调用均由离线替身接管。"
    "页面恢复使用本机服务和固定数据，桌面与手机视图都核对同一版本的内容。"
    "测试记录中的审核通过仅表示注入协议与资料绑定有效，不证明真实语义判断或预测效果。"
)

PLAYWRIGHT_RESTORE = r"""
const fs = require('node:fs');
const {chromium} = require(process.argv[1]);
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const results = [];
  try {
    for (const viewport of [{width:1366,height:768},{width:390,height:844}]) {
      const context = await browser.newContext({viewport, reducedMotion:'reduce'});
      let generationPosts = 0, blockedExternal = 0;
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (!['127.0.0.1','localhost'].includes(url.hostname)) { blockedExternal++; return route.abort(); }
        if (url.pathname === '/api/ai-analysis') {
          generationPosts++;
          return route.fulfill({status:503,contentType:'application/json',body:'{"status":"unexpected_generation"}'});
        }
        if (url.pathname === '/api/market-reference') {
          return route.fulfill({status:200,contentType:'application/json',body:'{"status":"unavailable"}'});
        }
        return route.continue();
      });
      const page = await context.newPage(), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(config.url + '/#game/' + encodeURIComponent(config.gameId));
      await page.waitForFunction(id => state.currentAnalyses.has(id) && state.activeGame?.id === id, config.gameId);
      await page.locator('#ai-analysis').click();
      await page.waitForFunction(() => document.querySelector('#sheet-content').textContent.includes('离线集成验收样例'));
      const before = await page.evaluate(id => ({
        version:state.currentAnalyses.get(id).version_id, prediction:activePrediction(state.activeGame),
        article:state.currentAnalyses.get(id).judgment.analysis_article,
        renderedArticle:document.querySelector('#sheet-content').textContent,
        probability:document.querySelector('#predicted-probability').textContent,
        margin:document.querySelector('#expected-margin').textContent,
        overflow:document.documentElement.scrollWidth > window.innerWidth + 1,
      }), config.gameId);
      await page.reload();
      await page.waitForFunction(id => state.currentAnalyses.has(id) && state.activeGame?.id === id, config.gameId);
      await page.locator('#ai-analysis').click();
      await page.waitForFunction(() => document.querySelector('#sheet-content').textContent.includes('离线集成验收样例'));
      const after = await page.evaluate(id => ({
        version:state.currentAnalyses.get(id).version_id, prediction:activePrediction(state.activeGame),
        article:state.currentAnalyses.get(id).judgment.analysis_article,
        renderedArticle:document.querySelector('#sheet-content').textContent,
        overflow:document.documentElement.scrollWidth > window.innerWidth + 1,
      }), config.gameId);
      if (config.screenshotDirectory) {
        await page.screenshot({path:config.screenshotDirectory + '/analysis-' + viewport.width + '.png',fullPage:true});
      }
      results.push({viewport,before,after,generationPosts,blockedExternal,errors});
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify(results));
})().catch(error => {console.error(error);process.exitCode=1;});
"""


class Clock:
    def __init__(self):
        self.at = TIP - timedelta(days=4)

    def __call__(self):
        return self.at


def seed_player_database(path):
    store = PlayerKnowledgeStore(path)
    rows = [
        {"game_id": "0022509001", "game_date": "2026-01-01", "home_team_id": NY, "away_team_id": PHI,
         "season": "2025-26", "season_type": "Regular Season"},
        {"game_id": "0022509002", "game_date": "2026-01-03", "home_team_id": BOS, "away_team_id": NY,
         "season": "2025-26", "season_type": "Regular Season"},
    ]
    store.initialize(rows)
    store.ingest_boxscore(rows[0], boxscore(rows[0],
        [player(int(TM), "Tyrese", "Maxey", stats={"points": 20}), player(int(JE), "Joel", "Embiid", stats={"points": 16})],
        [player(int(JB), "Jalen", "Brunson", stats={"points": 18}), player(int(MB), "Mikal", "Bridges", stats={"points": 12})]),
        collection_time_utc="2026-09-01T12:00:00Z", source_url="https://example.test/fixture/boxscore-one")
    store.ingest_boxscore(rows[1], boxscore(rows[1],
        [player(int(JB), "Jalen", "Brunson", stats={"points": 22}), player(int(MB), "Mikal", "Bridges", stats={"points": 14})],
        [player(int(FOREIGN), "UNRELATED", "TEAM PLAYER", stats={"points": 11})]),
        collection_time_utc="2026-09-01T12:00:00Z", source_url="https://example.test/fixture/boxscore-two")
    return store, rows


def roster():
    def team(team_id, players):
        return {"team_id": team_id, "roster_basis": "official_team_roster",
            "official_team_roster": {"status": "available", "team_id": team_id,
                "season_start": 2026, "id_namespace": "nba",
                "players": [{"player_id": pid, "name": name} for pid, name in players],
                "source": {"captured_at": "2026-09-15T12:00:00Z", "source_type": "fixture_official_team_roster",
                           "url": "https://example.test/fixture/roster", "sha256": f"roster-{team_id}"},
                "coverage": {"source_roster_completeness": "source_listed_not_independently_verified"}},
            "cold_start": {"season_start": 2026, "cold_start": True,
                "rolling_5_current_season_games": 0, "rolling_5_previous_season_games": 5,
                "rolling_15_current_season_games": 0, "rolling_15_previous_season_games": 15}}
    return {"teams": {
        "home": team(NY, [(JB, "Jalen Brunson"), (MB, "Mikal Bridges"), (NEW_NY, "Fixture New Guard")]),
        "away": team(PHI, [(TM, "Tyrese Maxey"), (JE, "Joel Embiid"), (NEW_PHI, "Fixture New Center")]),
    }, "numeric_model_usage": False}


def availability(cutoff):
    older = (cutoff - timedelta(hours=1)).isoformat()
    newer = (cutoff + timedelta(seconds=1)).isoformat()
    return {"status": "available", "records": [
        {"player_id": NEW_NY, "team_id": NY, "player_name": "Fixture New Guard", "status": "Questionable",
         "source_url": "https://example.test/fixture/injury", "source_published_at": older, "captured_at": older},
        {"player_id": JB, "team_id": NY, "player_name": "Jalen Brunson", "status": "Available",
         "source_url": "https://example.test/fixture/injury", "source_published_at": older, "captured_at": older},
        {"player_id": TM, "team_id": PHI, "player_name": "Tyrese Maxey", "status": "Out",
         "reason": "AFTER_CUTOFF_SENTINEL", "source_published_at": newer, "captured_at": newer},
        {"player_id": JE, "team_id": PHI, "player_name": "Joel Embiid", "status": "Out", "reason": "MISSING_TIME_SENTINEL"},
        {"player_id": FOREIGN, "team_id": BOS, "player_name": "UNRELATED TEAM PLAYER", "status": "Out",
         "source_published_at": older, "captured_at": older, "source_url": "https://example.test/fixture/injury"},
    ]}


class Harness:
    def __init__(self, root, monkeypatch, *, kind="preseason", knowledge=None):
        self.root, self.clock = root, Clock()
        self.knowledge, self.history_rows = seed_player_database(root / "players.sqlite")
        if knowledge is not None:
            self.knowledge = knowledge
        self.registration = {"game_id": "espn:499990101" if kind == "preseason" else "espn:499990102",
            "home_team_id": NY, "home_team": "New York Knicks", "away_team_id": PHI,
            "away_team": "Philadelphia 76ers", "scheduled_tipoff": TIP.isoformat(),
            "timezone": "UTC", "game_type": kind, "fixture": True}
        # Reuse actual saved team history/roster rendering fields, never production predictions for this fixture.
        self.template = copy.deepcopy(next(game for game in json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))["games"]
                                           if game["id"] == "espn:401909089"))
        self.template["roster_context"] = roster()
        self.template["background"]["availability"] = availability(TIP - timedelta(days=3))
        self.template["background"]["market"] = None
        self.template["relationship"]["market"] = None
        self.template["relationship"]["residual"] = None
        self.template["relationship"]["divergence"] = None
        self.numeric_before = copy.deepcopy(self.template["relationship"])
        self.input_events, self.research_inputs, self.writer_contexts, self.forecast_inputs = [], [], [], []
        self.writer_projections = []
        self.fail_generation, self.cross_tipoff = False, False
        self.entered, self.release = threading.Event(), None
        monkeypatch.setattr(analysis_service, "now", lambda: self.clock.at.isoformat())
        monkeypatch.setattr("v2.pregame_system.analyst.now", lambda: self.clock.at.isoformat())
        monkeypatch.setattr("v2.pregame_system.forecasting_workflow.now", lambda: self.clock.at.isoformat())

        harness = self
        class CapturingAnalyst(EvidenceBoundAnalyst):
            def judge(self, context, **kwargs):
                harness.writer_contexts.append(copy.deepcopy(context.model_dump()))
                return super().judge(context, **kwargs)

        class Researcher:
            def investigate(self, identity, cutoff, **kwargs):
                harness.research_inputs.append(copy.deepcopy(kwargs["local_context"]))
                return {"status": "available", "records": [{"id": "fixture-web-source", "category": "game_story",
                    "title": "Offline integration fixture", "url": "https://example.test/fixture/research",
                    "published_at": cutoff, "captured_at": cutoff, "eligible_for_fact": True,
                    "snippet": "This is synthetic test evidence, not real NBA news."}]}, {"status": "injected_fixture"}
        def analysis_factory(data, *, analyst):
            return SavedGameAnalysisService(data, analyst=analyst, researcher=Researcher(), player_knowledge_store=harness.knowledge)
        self.service = PredictionVersionService(store=PredictionVersionStore(root / "versions.sqlite", clock=self.clock),
            service_factory=analysis_factory, input_preparer=self.prepare,
            pre_generation_preparer=self.prepare_current_roster_preflight,
            analyst_factory=lambda request: CapturingAnalyst(root=root / "writer" / request["request_id"],
                include_forecasting_skill=True, client=self.client, forecast_client=self.client,
                reviewer_client=protocol_reviewer))
        registered = self.service.tasks.register_game(self.registration)
        from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
        self.game = SchedulerPolicyPlanner(self.service.tasks).configure(registered["game"]["id"],
            {"preheat_enabled": True, "preheat_reason": "offline integration fixture", "preheat_lead_hours": 72})
        self.scheduler = PregameScheduler(self.service)

    def prepare_current_roster_preflight(self, data, game_id):
        from v2.pregame_system.prediction_versions import VersionStoreError
        result = copy.deepcopy(data)
        game = next(item for item in result["games"] if item["id"] == game_id)
        summary = {"schema_version": "current-roster-preflight-v1", "status": "ready",
            "game_id": game_id, "teams": {},
            "scope": "verified current team-roster membership only; not game availability or confirmed starters"}
        snapshots = {}
        observed = (self.clock.at - timedelta(seconds=1)).isoformat()
        for side in ("away", "home"):
            expected = int(game[side]["id"])
            team = (self.template.get("roster_context") or {}).get("teams", {}).get(side) or {}
            official = team.get("official_team_roster") or team.get("roster") or {}
            source = official.get("source") or {}
            players = official.get("players") or []
            try:
                source_capture = datetime.fromisoformat(str(source.get("captured_at")).replace("Z", "+00:00"))
                if source_capture.tzinfo is None or source_capture >= self.clock.at:
                    raise ValueError("snapshot_time")
            except (TypeError, ValueError):
                raise VersionStoreError("current_roster_identity_preflight_failed:" + side) from None
            if (official.get("status") != "available" or int(team.get("team_id") or 0) != expected
                    or int(official.get("team_id") or 0) != expected or not players
                    or source.get("source_type") != "fixture_official_team_roster"):
                raise VersionStoreError("current_roster_identity_preflight_failed:" + side)
            source_hash = f"current-roster-fixture-{expected}-{observed}"
            source["captured_at"] = observed
            source["sha256"] = source_hash
            official["observed_at"] = observed
            summary["teams"][side] = {"status": "verified", "team_id": expected,
                "roster_basis": team.get("roster_basis"), "player_count": len(players),
                "observed_at": observed, "source_type": source["source_type"],
                "source_url": source["url"], "raw_response_sha256": source_hash,
                "parser_version": "fixture-roster-v1"}
            snapshots[side] = {"team_id": expected, "roster_basis": team.get("roster_basis"),
                "official_team_roster": copy.deepcopy(official),
                "current_team_roster": copy.deepcopy(team.get("current_team_roster")),
                "league_team_assignment": copy.deepcopy(team.get("league_team_assignment")),
                "minutes": None}
        game["current_roster_preflight"] = summary
        game["current_roster_snapshots"] = snapshots
        return result

    def prepare(self, data, game_id, *, cutoff):
        self.input_events.append("generation_input")
        def baseline(raw, cutoff):
            prepared = copy.deepcopy(self.template)
            prepared.update(id=raw["id"], tipoff=raw["tipoff"], game_type=raw["game_type"], fixture=True)
            return prepared
        prepared = prepare_generation_data(data, game_id, cutoff=cutoff, baseline_builder=baseline)
        self.numeric_pre_knowledge = prediction_layers(next(g for g in prepared["games"] if g["id"] == game_id))
        return prepared

    def client(self, messages, schema):
        incoming = json.loads(messages[1]["content"])
        if schema.__name__ in {"IndependentForecast", "VersionedIndependentForecast"}:
            self.forecast_inputs.append(incoming)
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(timeout=10)
            if self.fail_generation:
                raise RuntimeError("fixture_generation_failed")
            refs = [item["id"] for item in incoming["evidence"] if item["section"] in {"pure_prediction", "roster_context"}]
            value = {"prediction": {"winner_side": "home", "win_probability": 0.62,
                        "expected_home_margin": 3.5, "expected_total": 222.5},
                    "factors": [{"classification": "inference", "evidence_ids": refs,
                                 "effect": "home", "summary": "离线预测替身，验证上下文和数值绑定。"}],
                    "uncertainties": []}, {"fixture": True}
            if schema.__name__ == "VersionedIndependentForecast":
                value[0]["analysis_core"] = {
                    "central_question": "How can the primary half-court creator attack the opponent's coverage?",
                    "central_thesis": {"statement": "The half-court matchup is the clearest path to the home-side lean.",
                        "classification": "inference", "evidence_ids": refs[:1], "confidence": "medium"},
                    "matchup_status": "identified",
                    "key_matchups": [{"subject": "主队半场持球创造", "opponent_or_scheme": "客队换防与协防",
                        "why_it_matters": "这会影响第一拍能否形成二次进攻机会。",
                        "expected_effect": "若防守能够延误第一拍，比赛会更接近主队预期节奏。",
                        "classification": "inference", "evidence_ids": refs[:1], "confidence": "medium",
                        "uncertainty": "实际防守选择仍待比赛验证。"}],
                    "expected_game_script": "主队争取在半场组织中持续制造错位，客队则希望压缩处理时间并转向转换进攻。",
                    "flip_conditions": [{"condition": "主队持球点无法持续制造优势。",
                        "judgment_effect": "主队方向的把握需要下调。", "evidence_ids": refs[:1]}],
                }
            return value
        assert schema.__name__ == "EditorialDraft"
        self.writer_projections.append(incoming)
        result = {"title": "离线集成验收样例", "lead": "这是任务、球员知识和版本绑定的测试文章。",
            "article": ARTICLE, "confidence": "low",
            "watch_themes": [{"title": "阵容证据进入报告", "statement": "核对历史球员资料与当前名单各自的来源。"},
                             {"title": "数字与正文同版读取", "statement": "核对刷新前后的版本、数字和正文一致。"}]}
        self.clock.at = TIP if self.cross_tipoff else self.clock.at + timedelta(seconds=2)
        return result, {"fixture": True}

    def run(self, stage="EARLY_PREVIEW"):
        leads = {"EARLY_PREVIEW": timedelta(days=3), "UPDATED_PREVIEW": timedelta(hours=24), "FINAL_PREGAME": timedelta(minutes=5)}
        self.clock.at = TIP - leads[stage]
        if stage == "UPDATED_PREVIEW":
            from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
            SchedulerPolicyPlanner(self.service.tasks).material_update({"game_id": self.game["id"],
                "schedule_revision": self.game["schedule_revision"], "event_type": "availability_change",
                "affected_object": {"player_id": "fixture-player"}, "old_state": "unknown", "new_state": "confirmed",
                "evidence_batch_id": "integration-fixture-update", "evidence_ids": ["fixture-confirmed-source"],
                "observed_at": self.clock.at.isoformat(), "reason": "offline fixture material update"}, admin_confirmed=True)
        result = self.scheduler.run_due_tasks({"games": []})
        return result, self.service.selected(self.game)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def packet(version):
    return version["analyst_context"]["roster_context"]["player_knowledge"]


@pytest.mark.parametrize("kind", ["preseason", "regular_season"])
def test_registered_scheduler_real_player_context_to_version(tmp_path, monkeypatch, kind):
    h = Harness(tmp_path, monkeypatch, kind=kind)
    from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
    h.clock.at = TIP - timedelta(days=3)
    tasks = SchedulerPolicyPlanner(h.service.tasks).plan_game(h.game)
    assert {task["stage"] for task in tasks} == {"EARLY_PREVIEW", "FINAL_PREGAME"}
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "published", result
    version = selected["version"]
    knowledge = packet(version)
    assert h.input_events == ["generation_input"]
    assert len(h.research_inputs) == len(h.forecast_inputs) == len(h.writer_contexts) == 1
    assert len(h.writer_projections) == 1
    assert h.writer_contexts[0] == version["analyst_context"]
    assert h.writer_projections[0]["protocol"] == "editorial-decision-v2"
    assert "objective_evidence" not in h.writer_projections[0]
    assert version["publication_receipt"]["status"] == "eligible"
    assert version["publication_receipt"]["quality_review"]["status"] == "warning"
    assert h.research_inputs[0]["season_phase"]["game_type"] == kind
    identity = h.writer_contexts[0]["roster_context"]["current_roster_identity_coverage"]
    assert identity["status"] == "verified"
    assert {row["status"] for row in identity["teams"].values()} == {"verified"}
    history_coverage = h.writer_contexts[0]["roster_context"]["player_knowledge_history_coverage"]
    assert {row["scope"] for row in history_coverage.values()} == {"historical_player_record_match_only"}
    assert set(knowledge["teams"]) == {"home", "away"}
    assert {knowledge["teams"][side]["team_id"] for side in ("home", "away")} == {NY, PHI}
    assert set(knowledge["coverage"]["team_coverage"]) == {str(NY), str(PHI)}
    assert knowledge["teams"]["home"]["coverage"]["collected_games"] == 2
    assert knowledge["teams"]["away"]["coverage"]["collected_games"] == 1
    assert all(len(knowledge["teams"][side]["previous_season_players"]) == 3 for side in ("home", "away"))
    for writer_input in (h.writer_contexts[0], h.writer_projections[0]):
        assert "UNRELATED TEAM PLAYER" not in json.dumps(writer_input)
        assert "AFTER_CUTOFF_SENTINEL" not in json.dumps(writer_input)
        assert "MISSING_TIME_SENTINEL" not in json.dumps(writer_input)
    rookie = next(p for p in knowledge["teams"]["home"]["previous_season_players"] if p["player_id"] == NEW_NY)
    assert rookie["previous_season_history_status"] == "unavailable"
    assert rookie["current_availability"]["status"] == "Questionable"
    assert all(knowledge["teams"][side]["continuity"]["status"] == "provisional_roster_membership" for side in ("home", "away"))
    assert version["model_layers"] == h.numeric_pre_knowledge
    assert version["input_snapshot"]["relationship"] == h.numeric_before
    assert version["prediction"] == version["judgment"]["final_prediction"]
    assert version["task_id"] == result["results"][0]["task"]["task_id"]
    assert version["stage"] == "EARLY_PREVIEW" and version["trigger"]["type"] == "scheduled"
    assert version["research"]["records"][0]["id"] == "fixture-web-source"
    assert version["provenance"]["forecast_artifact"]["status"] == "available"
    assert version["provenance"]["forecast_artifact"]["files"][0]["sha256"]
    assert version["provenance"]["analyst_artifact"]["files"][0]["sha256"]


def test_compact_player_rows_preserve_values_samples_and_full_frozen_source(harness):
    h = harness
    _, selected = h.run()
    version = selected["version"]
    compact = packet(version)
    full = version["input_snapshot"]["roster_context"]["player_knowledge"]
    definitions = compact["column_definitions"]
    for side in ("home", "away"):
        original = {p["player_id"]: p for p in full["teams"][side]["previous_season_players"]}
        for player_row in compact["teams"][side]["previous_season_players"]:
            source = original[player_row["player_id"]]
            assert player_row["current_availability"] == source["current_availability"]
            for key in ("current_team_last_season_stint", "previous_season_total"):
                if not isinstance(player_row.get(key), list):
                    continue
                projected = dict(zip(definitions["history"], player_row[key]))
                raw = source[key]
                for metric, average, samples, missing in zip(definitions["per_observed_game"],
                        projected["per_observed_game"], projected["observed_games_by_statistic"],
                        projected["missing_games_by_statistic"]):
                    statistic = raw["statistics"][metric]
                    assert average == statistic.get("per_observed_game", statistic.get("per_appearance"))
                    assert samples == statistic["observed_games"]
                    assert missing == statistic["missing_games"]
    assert full["teams"]["home"]["previous_season_players"][0].get("factual_role_evidence")
    assert "column_definitions" not in full


def test_normalized_availability_keeps_identity_and_time_provenance():
    cut = TIP - timedelta(days=3)
    valid = availability(cut)["records"][0]
    report = {"availability": {"status": "available", "records": [valid]}}
    normalized = normalize_availability(report)
    assert normalized["records"][0]["player_id"] == valid["player_id"]
    assert normalized["records"][0]["captured_at"] == valid["captured_at"]
    assert normalized["records"][0]["source_published_at"] == valid["source_published_at"]


def test_future_missing_time_and_foreign_availability_do_not_enter_any_model_message(harness):
    h = harness
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "published", result
    writer = h.writer_contexts[0]
    for writer_input in (writer, h.writer_projections[0]):
        assert "AFTER_CUTOFF_SENTINEL" not in json.dumps(writer_input)
        assert "MISSING_TIME_SENTINEL" not in json.dumps(writer_input)
        assert "UNRELATED TEAM PLAYER" not in json.dumps(writer_input)
    records = next(e["value"]["records"] for e in writer["objective_evidence"] if e["section"] == "availability")
    assert {record["player_id"] for record in records} == {JB, NEW_NY}


def test_foreign_roster_team_blocks_before_generation(harness):
    h = harness
    h.template["roster_context"]["teams"]["home"] = {"team_id": BOS,
        "official_team_roster": {"status": "available", "team_id": BOS,
            "players": [{"player_id": FOREIGN, "name": "UNRELATED TEAM PLAYER"}],
            "source": {"captured_at": "2026-09-01T00:00:00Z"}}}
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "failed", result
    assert "current_roster_identity_preflight_failed:home" in result["results"][0]["task"]["last_error"]
    assert selected["version"] is None
    assert h.research_inputs == h.forecast_inputs == h.writer_contexts == []


def test_missing_current_roster_blocks_before_generation(harness):
    h = harness
    h.template["roster_context"]["teams"].pop("away")
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "failed", result
    assert "current_roster_identity_preflight_failed:away" in result["results"][0]["task"]["last_error"]
    assert selected["version"] is None
    assert h.research_inputs == h.forecast_inputs == h.writer_contexts == []


@pytest.mark.parametrize("case", ["after_cutoff", "missing_capture", "unavailable"])
def test_unverified_current_roster_blocks_before_research_or_writer(harness, case):
    h = harness
    official = h.template["roster_context"]["teams"]["home"]["official_team_roster"]
    official["players"] = [{"player_id": "90000099", "name": "UNVERIFIED_ROSTER_SENTINEL"}]
    if case == "after_cutoff":
        official["source"]["captured_at"] = (TIP - timedelta(days=3) + timedelta(seconds=1)).isoformat()
    elif case == "missing_capture":
        official["source"].pop("captured_at")
    else:
        official["status"] = "unavailable"
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "failed", result
    assert "current_roster_identity_preflight_failed:home" in result["results"][0]["task"]["last_error"]
    assert selected["version"] is None
    assert "UNVERIFIED_ROSTER_SENTINEL" not in json.dumps(h.research_inputs)
    assert "UNVERIFIED_ROSTER_SENTINEL" not in json.dumps(h.writer_contexts)
    assert h.research_inputs == h.forecast_inputs == h.writer_contexts == []


def test_no_availability_does_not_create_available_or_out(harness):
    h = harness
    h.template["background"]["availability"] = {"status": "unavailable", "records": []}
    _, selected = h.run()
    context = selected["version"]["analyst_context"]
    assert not any(item["section"] == "availability" for item in context["objective_evidence"])
    assert any(item["section"] == "availability" for item in context["missing_data"])
    assert all(p["current_availability"] is None for team in packet(selected["version"])["teams"].values()
               for p in team.get("previous_season_players", []))


def test_knowledge_query_failure_is_explicit_and_numeric_inputs_unchanged(harness):
    h = harness
    class Broken:
        def matchup_context(self, *args, **kwargs):
            raise RuntimeError("fixture_database_query_failure")
    h.knowledge = Broken()
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "published", result
    knowledge = packet(selected["version"])
    assert knowledge["status"] == "unavailable" and knowledge["teams"] == {}
    assert knowledge["reason"] == "player_knowledge_load_failed"
    assert selected["version"]["model_layers"] == h.numeric_pre_knowledge


def test_database_update_never_changes_old_published_player_packet(harness):
    h = harness
    _, first = h.run()
    frozen = copy.deepcopy(first["version"])
    row = h.history_rows[0]
    h.knowledge.ingest_boxscore(row, boxscore(row,
        [player(int(TM), "Tyrese", "Maxey", stats={"points": 20})],
        [player(int(JB), "Jalen", "Brunson", stats={"points": 77})]),
        collection_time_utc="2026-10-18T00:00:00Z", source_url="https://example.test/fixture/revised-boxscore")
    current = h.service.selected(h.game)["version"]
    assert current == frozen
    _, second = h.run("UPDATED_PREVIEW")
    assert second["version_id"] != first["version_id"]
    assert packet(second["version"]) != packet(frozen)
    with h.service.store.connect() as db:
        old = h.service.store._decode_version(db.execute("SELECT * FROM prediction_versions WHERE version_id=?", (first["version_id"],)).fetchone())
    assert old == frozen


def test_later_player_receipt_cannot_replace_history_before_cutoff(harness):
    h = harness
    row = h.history_rows[0]
    h.knowledge.ingest_boxscore(row, boxscore(row,
        [player(int(TM), "Tyrese", "Maxey", stats={"points": 99})],
        [player(int(JB), "Jalen", "Brunson", stats={"points": 77})]),
        collection_time_utc="2026-10-18T00:00:00Z", source_url="https://example.test/fixture/late-receipt")
    _, selected = h.run()
    profiles = packet(selected["version"])["teams"]["home"]["previous_season_players"]
    guard = next(p for p in profiles if p["player_id"] == JB)
    history = dict(zip(packet(selected["version"])["column_definitions"]["history"], guard["current_team_last_season_stint"]))
    assert history["points_total"] == 40
    assert packet(selected["version"])["coverage"]["source"]["collection_time_max"] < selected["version"]["input_cutoff_at"]


@pytest.mark.parametrize("field", ["source_published_at", "captured_at"])
def test_naive_availability_timestamp_is_not_time_provenance(harness, field):
    h = harness
    h.template["background"]["availability"]["records"][0][field] = "2026-10-16T12:00:00"
    _, selected = h.run()
    context = selected["version"]["analyst_context"]
    records = next(item["value"]["records"] for item in context["objective_evidence"] if item["section"] == "availability")
    assert {row["player_id"] for row in records} == {JB}


def test_current_demo_only_runtime_blocks_new_game_before_any_external_stage(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("COURTVISION_RUNTIME_ASSET_ROOT", str(tmp_path / "missing-production-assets"))
    monkeypatch.setenv("COURTVISION_PRODUCTION_MODEL", "")
    h = harness
    h.service.input_preparer = prepare_generation_data
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "failed"
    assert "registered_game_requires_production_runtime_assets" in result["results"][0]["task"]["last_error"]
    assert selected["version"] is None
    assert h.research_inputs == h.forecast_inputs == h.writer_contexts == []


def test_next_stage_running_and_failed_keep_previous_success(harness):
    h = harness
    _, first = h.run()
    h.entered.clear()
    h.release = threading.Event()
    answers = []
    worker = threading.Thread(target=lambda: answers.append(h.run("UPDATED_PREVIEW")))
    worker.start()
    assert h.entered.wait(timeout=5)
    status, payload = server.build_prediction_versions_response(data={"games": []}, version_service=h.service)
    assert status == 200
    selected = payload["predictions"][h.game["id"]]
    assert selected["version_id"] == first["version_id"]
    assert selected["generation_task"]["status"] == "running"
    h.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert answers[0][1]["version_id"] != first["version_id"]
    second = answers[0][1]
    h.release = None
    h.fail_generation = True
    result, selected = h.run("FINAL_PREGAME")
    assert result["results"][0]["update_status"] == "failed"
    assert selected["version_id"] == second["version_id"]


def test_fake_generation_crossing_tipoff_cannot_publish(harness):
    h = harness
    _, first = h.run()
    h.cross_tipoff = True
    result, selected = h.run("FINAL_PREGAME")
    assert result["results"][0]["update_status"] == "failed"
    assert selected["version_id"] == first["version_id"] and selected["status"] == "locked"


def test_actual_server_and_frontend_read_same_player_bound_version(harness, monkeypatch):
    h = harness
    _, selected = h.run()
    assert selected["version"], "Fixture pipeline did not publish"
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", h.service)
    monkeypatch.setattr(server, "read_json", lambda path: {"games": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT)))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        game_id = selected["version"]["game_id"]
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/game?game_id={game_id}", timeout=5) as reply:
            detail = json.load(reply)
        data = {"games": [detail["game"]]}
        current = {"predictions": {game_id: detail["prediction"]}}
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    if not shutil.which("node"):
        pytest.skip("Node.js is required for frontend restore")
    probe = subprocess.run(["node", "-e", NODE_FRONTEND_PROBE, str(STATIC_ROOT / "app.js")],
        input=json.dumps({"game": data["games"][0], "response": current}), text=True, encoding="utf-8",
        capture_output=True, timeout=20, check=True)
    restored = json.loads(probe.stdout)
    assert restored["restoredVersionId"] == restored["displayedVersionId"] == selected["version_id"]
    assert restored["restoredArticle"] == selected["version"]["judgment"]["analysis_article"]
    assert restored["displayedMargin"] == selected["version"]["prediction"]["expected_margin"]
    assert len(h.writer_contexts) == len(h.forecast_inputs) == 1


def test_real_local_player_database_in_current_chain(tmp_path, monkeypatch):
    if not PLAYER_DB.is_file():
        pytest.skip("Full local Player Knowledge database is not included in fresh-clone runtime assets")
    h = Harness(tmp_path, monkeypatch, knowledge=PlayerKnowledgeStore(PLAYER_DB))
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "published", result
    knowledge = packet(selected["version"])
    assert knowledge["coverage"]["collected_games"] == 1230
    assert knowledge["coverage"]["player_game_rows"] == 32191
    assert set(knowledge["coverage"]["team_coverage"]) == {str(NY), str(PHI)}
    for side in ("home", "away"):
        assert knowledge["teams"][side]["coverage"]["collected_games"] == 82
        assert len(knowledge["teams"][side]["previous_season_players"]) == 3
        assert knowledge["teams"][side]["current_roster_match_count"] == 2
    output = os.environ.get("COURTVISION_INTEGRATION_ARTIFACT_DIR")
    if output:
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "fixture_version.json").write_text(json.dumps(selected["version"], ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "analyst_context.json").write_text(json.dumps(h.writer_contexts[0], ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "writer_projection.json").write_text(json.dumps(h.writer_projections[0], ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "forecast_input.json").write_text(json.dumps(h.forecast_inputs[0], ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "summary.json").write_text(json.dumps({"fixture": True, "real_player_database": str(PLAYER_DB),
            "game": h.registration, "version_id": selected["version_id"], "stage": selected["version"]["stage"],
            "teams": {side: {"team_id": team["team_id"], "coverage": team["coverage"],
                "profiles": len(team["previous_season_players"]), "matched": team["current_roster_match_count"],
                "unmatched": team["players_without_previous_season_record"], "continuity": team["continuity"]["status"]}
                for side, team in knowledge["teams"].items()}, "numeric_inputs_unchanged": selected["version"]["model_layers"] == h.numeric_pre_knowledge,
            "research_calls": len(h.research_inputs), "forecast_calls": len(h.forecast_inputs), "editorial_calls": len(h.writer_contexts)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copytree(tmp_path / "writer", folder / "writer_artifacts", dirs_exist_ok=True)


def test_real_browser_refresh_reads_same_frozen_report(harness, monkeypatch):
    module = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
    if not shutil.which("node") or not module.is_dir() or not Path('<local-configured-path> Files (x86)/Microsoft/Edge/Application/msedge.exe').is_file():
        pytest.skip("Local Playwright package and installed Edge are required; no browser download is performed")
    h = harness
    if PLAYER_DB.is_file():
        h.knowledge = PlayerKnowledgeStore(PLAYER_DB)
    _, selected = h.run()
    base_data = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    base_data["games"] = []
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", h.service)
    monkeypatch.setattr(server, "read_json", lambda path: base_data)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT)))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    screenshot_directory = os.environ.get("COURTVISION_INTEGRATION_BROWSER_DIR")
    if screenshot_directory:
        Path(screenshot_directory).mkdir(parents=True, exist_ok=True)
    try:
        probe = subprocess.run(["node", "-e", PLAYWRIGHT_RESTORE, str(module)],
            input=json.dumps({"url": f"http://127.0.0.1:{httpd.server_port}", "gameId": h.game["id"],
                              "screenshotDirectory": screenshot_directory}),
            text=True, encoding="utf-8", capture_output=True, timeout=60, check=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    results = json.loads(probe.stdout)
    for viewport in results:
        for moment in ("before", "after"):
            read = viewport[moment]
            assert read["version"] == read["prediction"]["version_id"] == selected["version_id"]
            assert read["article"] == selected["version"]["judgment"]["analysis_article"]
            assert read["article"] in read["renderedArticle"]
            assert read["prediction"]["winner_probability"] == selected["version"]["prediction"]["win_probability"]
            assert read["overflow"] is False
        assert viewport["generationPosts"] == 0
        assert viewport["errors"] == []
        assert "62.0%" in viewport["before"]["probability"]
        assert "3.5" in viewport["before"]["margin"]
    assert len(h.writer_contexts) == len(h.forecast_inputs) == 1
    if screenshot_directory:
        (Path(screenshot_directory) / "browser_restore.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        (Path(screenshot_directory) / "browser_fixture_version.json").write_text(json.dumps(selected["version"], ensure_ascii=False, indent=2), encoding="utf-8")
