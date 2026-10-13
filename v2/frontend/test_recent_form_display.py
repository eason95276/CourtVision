from __future__ import annotations

import json
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .export_data import STATIC_ROOT


PLAYWRIGHT = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
BROWSER_CHECK = r"""
const {chromium}=require(process.argv[1]);
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.goto(process.argv[2],{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.data!==null,{timeout:10000});
    const result=await page.evaluate(()=>{
      const game={id:'espn:target',tipoff:'2026-10-07T02:00:00Z',game_type:'preseason',
        season_form:{status:'available',game_id:'espn:target',cutoff:'2026-10-07T01:00:00Z',teams:{
          away:{current_season_games:0,current_last5:{status:'no_games',games:0,game_ids:[]},
            previous_last10:{status:'available',games:10,win_rate:0.4}},
          home:{current_season_games:0,current_last5:{status:'no_games',games:0,game_ids:[]},
            previous_last10:{status:'available',games:10,win_rate:0.5}}
        }}};
      const version={input_cutoff_at:'2026-10-07T01:00:00Z'};
      const noGames=recentStatusLines(game,version,'away');
      game.season_form.teams.away={team_id:1,current_season_games:1,
        current_season_game_ids:['espn:prior'],
        current_season_record:{wins:1,losses:0},
        current_last5:{status:'available',games:1,wins:1,losses:0,win_rate:1,game_ids:['espn:prior']},
        previous_last10:{status:'available',games:10,win_rate:0.4},
        last_result:{game_id:'espn:prior',played_at:'2026-10-06T02:00:00Z',team_score:127,
          opponent_score:103,opponent_name_zh:'国王'},streak:{result:'win',games:2}};
      const oneGame=recentStatusLines(game,version,'away');
      const safeForm={...game,season_form:{...game.season_form,source:'schedule_catalog_final_games'}};
      const accepted=hasSafeCurrentScheduleForm(safeForm,version);
      const lateForm={...safeForm,season_form:{...safeForm.season_form,cutoff:'2026-10-07T01:30:00Z'}};
      const lateAccepted=hasSafeCurrentScheduleForm(lateForm,version);
      const targetForm={...game,season_form:{...game.season_form,teams:{...game.season_form.teams,
        away:{...game.season_form.teams.away,current_season_game_ids:['espn:target'],
          current_season_record:{wins:1,losses:0},streak:{result:'win',games:2},
          current_last5:{status:'available',games:1,win_rate:1,game_ids:['espn:target']},
          last_result:{game_id:'espn:target',played_at:'2026-10-07T02:00:00Z',
          team_score:98,opponent_score:124,opponent_name_zh:'勇士'}}}}};
      const target=recentStatusLines(targetForm,version,'away');
      return {noGames,oneGame,accepted,lateAccepted,target};
    });
    console.log(JSON.stringify(result));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
"""


def test_recent_status_is_current_season_only_and_rejects_late_or_target_results():
    node = shutil.which("node")
    if not node or not PLAYWRIGHT.is_dir():
        import pytest
        pytest.skip("Local Node and Playwright bundle required; no downloads are performed.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(STATIC_ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = subprocess.run(
            [node, "-e", BROWSER_CHECK, str(PLAYWRIGHT), f"http://127.0.0.1:{server.server_port}"],
            text=True, encoding="utf-8", capture_output=True, timeout=60, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    assert probe.returncode == 0, probe.stderr
    result = json.loads(probe.stdout)
    assert result["noGames"] == ["本季暂无比赛记录"]
    assert result["oneGame"] == ["本季已赛1场：1胜0负", "上一场：127–103 国王", "当前连胜2场"]
    assert result["accepted"] is True
    assert result["lateAccepted"] is False
    assert "98" not in " ".join(result["target"])
    assert "124" not in " ".join(result["target"])
    assert "本季近期战绩暂不可用" in result["target"]
