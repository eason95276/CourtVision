"""Verified schedule intake and read-only no-report pages; all tests use temporary SQLite."""
import copy
import functools
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError
from . import server
from .export_data import DEFAULT_OUTPUT, STATIC_ROOT
from .generation_input import merge_registered_games
from .prediction_version_service import PredictionVersionService
from .pregame_scheduler import PregameScheduler
from .register_schedule import register_verified_schedule, verified_preseason_games
from .test_pregame_scheduler import no_external_services  # autouse: only localhost requests


@pytest.fixture
def intake():
    # Sanitized schedule fixture only: never fake prediction/roster/AI content.
    entries = [
        ("2026-10-03", "401902644", "0012600009", "TOR", 28, 1610612761, "MIA", 14, 1610612748),
        ("2026-10-04", "401914127", "0012600067", "DEN", 7, 1610612743, "UTAH", 26, 1610612762),
        ("2026-10-04", "401918010", "0012600066", "LAC", 12, 1610612746, "GS", 9, 1610612744),
    ]
    boards = {day: {"board": {"events": []}, "receipt": {"status": "success", "sha256": "fixture-receipt",
        "captured_at": "2026-10-02T10:00:00Z", "url": "https://site.api.espn.com/fixture-schedule"}}
        for day in ("2026-10-03", "2026-10-04")}
    verification = {"season_end_year": 2027, "official_game_days": list(boards),
                    "captured_at": "2026-10-02T10:01:00Z", "games": []}
    for day, eid, nbaid, home, hid, hnba, away, aid, away_nba in entries:
        tip = day + "T23:00:00Z"
        competitors = [{"homeAway": side, "team": {"id": str(pid), "abbreviation": abbr, "displayName": abbr}}
            for side, pid, abbr in (("home", hid, home), ("away", aid, away))]
        boards[day]["board"]["events"].append({"id": eid, "date": tip, "season": {"year": 2027, "type": 1},
            "competitions": [{"competitors": competitors, "timeValid": True,
                "status": {"type": {"state": "pre", "detail": "7 PM EDT"}}, "neutralSite": eid == "401918010"}]})
        verification["games"].append({"provider_game_id": "espn:" + eid, "nba_game_id": nbaid,
            "home_team_id": hnba, "away_team_id": away_nba, "scheduled_tipoff": tip,
            "official_game_day": day, "source_url": "https://www.nba.com/game/" + nbaid})
    return boards, verification


@pytest.fixture
def service(tmp_path, intake):
    instance = PredictionVersionService(store=PredictionVersionStore(tmp_path / "versions.sqlite",
        clock=lambda: datetime(2026, 10, 2, 12, tzinfo=timezone.utc)))
    register_verified_schedule(*intake, instance.tasks)
    return instance


def test_verified_registration_time_identity_and_no_reports(service, intake):
    games = service.tasks.registered_games()
    assert len(games) == 3
    assert all(game["game_type"] == "preseason" and game["timezone"] == "UTC" for game in games)
    assert games[0]["scheduled_tipoff_beijing"] == "2026-10-04T07:00:00+08:00"
    assert all(game["generation_hold_reason"] == "scheduler_policy_pending" for game in games)
    register_verified_schedule(*intake, service.tasks)
    assert len(service.tasks.registered_games()) == 3
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["scheduled_tipoff", "away_team_id", "source_url", "official_game_day"])
def test_conflicting_official_verification_rejected_before_registration(tmp_path, intake, field):
    boards, verification = copy.deepcopy(intake)
    verification["games"][0][field] = {"scheduled_tipoff": "2026-10-03T22:00:00Z", "away_team_id": 1610612738,
        "source_url": "https://example.test/unverified", "official_game_day": "2026-10-04"}[field]
    instance = PredictionVersionService(store=PredictionVersionStore(tmp_path / "p.sqlite"))
    with pytest.raises(ValueError):
        register_verified_schedule(boards, verification, instance.tasks)
    assert instance.tasks.registered_games() == []


def test_schedule_change_is_explicitly_rejected_without_duplicate_or_rewrite(service):
    game = service.tasks.registered_games()[0]
    changed = copy.deepcopy(game)
    changed["tipoff"] = "2026-10-04T00:00:00Z"
    changed["scheduled_tipoff"] = changed["tipoff"]
    with pytest.raises(VersionStoreError, match="schedule_changed"):
        service.tasks.register_game(changed)
    assert len(service.tasks.registered_games()) == 3
    assert service.tasks.registered_game(game["id"])["tipoff"] == game["tipoff"]


def test_cancelled_tasks_survive_ensure_and_reject_manual_claims(service, monkeypatch):
    for game in service.tasks.registered_games():
        stripped = {key: value for key, value in game.items() if key != "generation_hold_reason"}
        result = service.tasks.ensure_schedule(stripped)
        assert all(task["status"] == "cancelled" and service.tasks.claim(task["task_id"]) is None for task in result["tasks"])
        with pytest.raises(VersionStoreError, match="scheduler_policy_pending"):
            service.tasks.request_manual(stripped)
    monkeypatch.setattr(service, "execute_task", lambda *a: pytest.fail("Must not execute held generation"))
    assert PregameScheduler(service).run_due_tasks({"games": []})["due_count"] == 0


def test_no_report_api_is_fact_only_and_ai_generation_is_paused(service, monkeypatch):
    facts = merge_registered_games({"games": []}, service.tasks, ready_only=True)["games"]
    assert len(facts) == 3
    for game in facts:
        assert game["prediction"] is None and game["roster_context"] is None
        assert game["background"]["availability"]["status"] == "unavailable"
        assert game["background"]["recent_form"] == {"home": {}, "away": {}}
    monkeypatch.setattr(service, "generate", lambda *a, **kw: pytest.fail("No generation permitted"))
    status, result = server.build_ai_response(facts[0]["id"], api_key="not-used", data={"games": []}, version_service=service)
    assert status == 200 and result["update_status"] == "read_only"
    assert result["version"] is None
    assert server.build_prediction_versions_response(data={"games": []}, version_service=service)[0] == 200


FACT_BROWSER = r"""
const fs = require('node:fs'), {chromium} = require(process.argv[1]);
const config = JSON.parse(fs.readFileSync(0,'utf8'));
(async () => {
 const browser = await chromium.launch({channel:'msedge',headless:true});
 const results=[];
 try {
  for (const viewport of [{width:1366,height:768},{width:390,height:844}]) {
   const context=await browser.newContext({viewport,reducedMotion:'reduce'});
   let aiPosts=0,marketReads=0,marketPosts=0,allPosts=0,readerGets=0;
   await context.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(route.request().method()==='POST')allPosts++;
    if(url.pathname==='/api/current-analyses')readerGets++;
    if(!['127.0.0.1','localhost'].includes(url.hostname))return route.abort();
    if(url.pathname==='/api/ai-analysis') {aiPosts++;return route.fulfill({status:503,body:'{}'});}
    if(url.pathname==='/api/market-reference') {
     if(route.request().method()==='POST')marketPosts++;else marketReads++;
     return route.fulfill({status:503,body:'{}'});
    }
    return route.continue();
   });
   const page=await context.newPage(),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   for(const game of config.games) {
    await page.goto(config.url+'/#game/'+encodeURIComponent(game.id));
    await page.waitForFunction(id=>state.activeGame?.id===id,game.id);
    for(let pass=0;pass<2;pass++) {
     if(pass){await page.reload();await page.waitForFunction(id=>state.activeGame?.id===id,game.id);}
     const read=await page.evaluate(()=>({
      id:state.activeGame.id, home:state.activeGame.home.id,away:state.activeGame.away.id,
      status:document.querySelector('#detail-status').textContent,
      time:document.querySelector('#detail-time').textContent,
      prediction:activePrediction(state.activeGame),winner:document.querySelector('#predicted-winner').textContent,
      probability:document.querySelector('#predicted-probability').textContent,
      disabled:document.querySelector('#ai-analysis').disabled,
      injury:document.querySelector('#injury-context').textContent,
      roster:state.activeGame.roster_context,
      overflow:document.documentElement.scrollWidth>innerWidth,
     }));
     results.push({viewport,pass,...read});
     if(config.exerciseReader){
      await page.evaluate(()=>requestAiAnalysis(true));
      await page.waitForFunction(()=>!state.aiPending);
      await page.evaluate(()=>closeSheet());
     }
    }
    if(config.screenshots)await page.screenshot({path:config.screenshots+'/'+game.id.replace(':','_')+'-'+viewport.width+'.png',fullPage:true});
   }
   results.push({viewport,aiPosts,marketReads,marketPosts,allPosts,readerGets,errors});
   await context.close();
  }
 } finally {await browser.close();}
 console.log(JSON.stringify(results));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def test_browser_facts_and_refresh_are_zero_generation(service, monkeypatch):
    module = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
    if not shutil.which("node") or not module.is_dir():
        pytest.skip("Local Node and Playwright installation required; no downloads")
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", service)
    base = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    base["games"] = []
    monkeypatch.setattr(server, "read_json", lambda path: base)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT)))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    screenshots = os.environ.get("COURTVISION_PRESEASON_SCREENSHOTS")
    if screenshots:
        Path(screenshots).mkdir(parents=True, exist_ok=True)
    games = merge_registered_games(base, service.tasks, ready_only=True)["games"]
    try:
        probe = subprocess.run(["node", "-e", FACT_BROWSER, str(module)],
            input=json.dumps({"url": f"http://127.0.0.1:{httpd.server_port}", "games": games,
                              "screenshots": screenshots, "exerciseReader": True}), text=True, encoding="utf-8", capture_output=True,
            timeout=60, check=True)
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)
    results = json.loads(probe.stdout)
    for row in results:
        if "aiPosts" in row:
            assert row["aiPosts"] == row["marketPosts"] == row["allPosts"] == 0
            assert row["readerGets"] >= 6
            assert row["errors"] == []
        else:
            game = next(g for g in games if g["id"] == row["id"])
            assert row["prediction"] is None and row["probability"] == "—"
            assert row["home"] == game["home"]["id"] and row["away"] == game["away"]["id"]
            assert "季前赛" in row["status"]
            assert "未开始" in row["status"] or "赛况待更新" in row["status"]
            assert "已结束" not in row["status"]
            assert "07:00" in row["time"]
            assert row["roster"] is None and row["disabled"] and not row["overflow"]
    if screenshots:
        (Path(screenshots) / "fact_browser_receipt.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
