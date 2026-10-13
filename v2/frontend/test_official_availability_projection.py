from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


GAME_ID = "espn:401898390"
PLAYWRIGHT = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"

BROWSER_CHECK = r"""
const {chromium}=require(process.argv[1]);
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1366,height:768},reducedMotion:'reduce'});
    const pageErrors=[],consoleErrors=[];
    page.on('pageerror',e=>pageErrors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text())});
    await page.goto('http://127.0.0.1:8625/#game/'+encodeURIComponent(process.argv[2]),{waitUntil:'domcontentloaded'});
    await page.waitForFunction(id=>state.activeGame?.id===id&&state.currentAnalyses.has(id),process.argv[2],{timeout:15000});
    const result=await page.evaluate(id=>{
      const game=state.activeGame,original=state.currentAnalyses.get(id),version=structuredClone(original);
      state.editorialOverlays.delete(id); // This test isolates injury projection from the separate portfolio editorial layer.
      const teamCandidates={};
      for(const side of ['away','home']){
        const team=version.input_snapshot.roster_context.teams[side];
        const existing=team.official_team_roster?.players||[];
        const players=existing.slice(0,5).map((p,index)=>({...p,name:p.name||p.player_name||`Synthetic ${side} ${index+1}`,team_id:game[side].id}));
        while(players.length<5)players.push({name:`Synthetic ${side} ${players.length+1}`,player_id:`synthetic-${side}-${players.length+1}`,team_id:game[side].id});
        team.team_id=game[side].id;
        team.official_team_roster={...(team.official_team_roster||{}),status:'available',team_id:game[side].id,players};
        team.confirmed_starters=[];team.projected_starters=[];team.current_rotation={projected_starters:[]};
        teamCandidates[side]=players.slice(0,4);
      }
      const newsOnlyPlayer=version.input_snapshot.roster_context.teams.home.official_team_roster.players[4];
      const tipoff=Date.parse(game.tipoff),earlier=new Date(tipoff-3*60*60*1000).toISOString(),
        latest=new Date(tipoff-60*60*1000).toISOString(),postTipoff=new Date(tipoff+60*60*1000).toISOString();
      const reportUrl=at=>{
        const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',
          day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:true}).formatToParts(new Date(at)).map(x=>[x.type,x.value]));
        return `https://ak-static.cms.nba.com/referee/injury/Injury-Report_${p.year}-${p.month}-${p.day}_${p.hour}_${p.minute}${p.dayPeriod}.pdf`;
      };
      const report=(at,rows)=>({game_id:id,source_url:reportUrl(at),source_published_at:at,captured_at:new Date().toISOString(),
        status:'available',records:rows.map(([side,index,status,reason=''])=>({game_id:id,team_id:game[side].id,
          player_id:(teamCandidates[side][index]||version.input_snapshot.roster_context.teams[side].official_team_roster.players[index]).player_id,
          player_name:(teamCandidates[side][index]||version.input_snapshot.roster_context.teams[side].official_team_roster.players[index]).name,status,reason,
          source_url:reportUrl(at),source_published_at:at,captured_at:new Date().toISOString()}))});
      const old=report(earlier,[['away',1,'Available'],['home',1,'Available']]);
      const final=report(latest,[['away',0,'Out','left knee strain'],['away',1,'Questionable','ankle soreness'],
        ['away',2,'Probable','wrist soreness'],['away',3,'Available'],['home',0,'Out','knee injury'],
        ['home',1,'Doubtful','back soreness'],['home',2,'Available'],['home',3,'Probable','ankle soreness']]);
      const wrongGame=report(latest,[['home',4,'Out','wrong target game']]);
      wrongGame.game_id='espn:wrong-game';wrongGame.records[0].game_id='espn:wrong-game';
      const after=report(postTipoff,[['away',1,'Available']]);
      version.input_snapshot.background={...(version.input_snapshot.background||{}),
        availability:{status:'available',game_id:id,source_url:'https://example.com/news',records:[{
          game_id:id,team_id:game.home.id,player_name:newsOnlyPlayer.name,status:'out',
          source_url:'https://example.com/news',source_published_at:latest}]},
        official_injury_report:{reports:[old,final,wrongGame,after]}};
      version.editorial_material=[{pregame_plan:{lineups:['away','home'].map(side=>({side,basis:'agent_expected',
        players:version.input_snapshot.roster_context.teams[side].official_team_roster.players.map(p=>p.name),evidence_ids:[]})),
        player_usage:[],key_matchup:'fixture',uncertainty:[]}}];
      version.judgment.player_analysis=['away','home'].flatMap(side=>teamCandidates[side].map(player=>({player_name:player.name,
        statement:`${player.name}的角色影响今天这组对位。`,classification:'subjective_analysis',evidence_ids:[]})));
      version.analyst_context.objective_evidence.push({id:'news-only-out',section:'web_context',value:{eligible_for_fact:true,
        game_id:id,team_id:game.home.id,player_name:newsOnlyPlayer.name,claim_type:'availability',status:'out',
        quote:`${newsOnlyPlayer.name} is out.`,url:'https://example.com/news'}});
      state.currentAnalyses.set(id,version);
      renderProductAnalysis(version);renderContext(game,version);
      const reportView=officialInjuryReportProjection(game,version);
      const outChecks={away:lineupPlayerConfirmedOut(game,version,'away',teamCandidates.away[0].name,[],reportView),
        home:lineupPlayerConfirmedOut(game,version,'home',teamCandidates.home[0].name,[],reportView),
        direct:officialStatusForPlayer(reportView,game.away.id,teamCandidates.away[0].name),candidate:teamCandidates.away[0].name,
        records:reportView.records.map(r=>({team:r.teamId,name:r.player?.name,status:r.status,playerId:r.player?.player_id}))};
      const injuryText=document.querySelector('#injury-context').textContent;
      const availabilityHtml=document.querySelector('#injury-context').innerHTML;
      const availabilityClasses=[...document.querySelectorAll('#injury-context .availability-entry')].map(e=>e.className);
      const probableLowPriority=document.querySelectorAll('#injury-context .availability-entry.is-low-priority').length;
      const lineups=[...document.querySelectorAll('#lineup-context .lineup-team')].map(e=>e.textContent);
      const playerRows=[...document.querySelectorAll('#key-players .player-side')].map(side=>({
        names:[...side.querySelectorAll('.player-entry h4')].map(h=>h.childNodes[0]?.textContent||''),
        statements:[...side.querySelectorAll('.player-entry p')].map(p=>p.textContent),
        statuses:[...side.querySelectorAll('.player-availability-status')].map(s=>s.textContent)}));
      state.currentAnalyses.set(id,original);renderProductAnalysis(original);renderContext(game,original);
      return {versionId:original.version_id,reportStatus:reportView.status,reportTimestamp:new Date(reportView.timestamp).toISOString(),
        injuryText,lineups,playerRows,outChecks,candidates:{away:teamCandidates.away.map(p=>playerName(p)),home:teamCandidates.home.map(p=>playerName(p))},
        newsOnly:playerName(newsOnlyPlayer),availabilityHtml,availabilityClasses,probableLowPriority};
    },process.argv[2]);
    result.pageErrors=pageErrors;result.consoleErrors=consoleErrors;
    console.log(JSON.stringify(result));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
"""


def test_official_report_controls_injury_lineup_and_key_player_projection():
    node = shutil.which("node")
    if not node or not PLAYWRIGHT.is_dir():
        pytest.skip("Local Node and Playwright bundle required; no downloads are performed.")
    try:
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:8625/api/game?game_id=espn%3A401898390", timeout=2):
            pass
    except Exception as exc:
        pytest.skip(f"The configured local site is not running: {type(exc).__name__}")
    result = subprocess.run(
        [node, "-e", BROWSER_CHECK, str(PLAYWRIGHT), GAME_ID],
        text=True, encoding="utf-8", capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["versionId"] == "ffc2947f-ae38-424c-894f-c2f05d71c3d6"
    assert output["reportStatus"] == "published"
    assert output["reportTimestamp"] < "2026-10-07T02:00:00+00:00"
    assert "确认缺阵" in output["injuryText"]
    assert "出战成疑" in output["injuryText"]
    assert "大概率缺阵" in output["injuryText"]
    assert "大概率出战" in output["injuryText"]
    assert output["newsOnly"] not in output["injuryText"]
    assert output["outChecks"]["away"] is True and output["outChecks"]["home"] is True
    assert len(output["lineups"]) == 2
    for side, lineup in enumerate(output["lineups"]):
        out_name = output["candidates"]["away" if side == 0 else "home"][0]
        assert out_name not in lineup, output["outChecks"]
        assert "已移除" in lineup
    assert output["newsOnly"] in output["lineups"][1]
    assert output["candidates"]["home"][2] not in output["injuryText"]
    assert output["probableLowPriority"] >= 1
    assert len(output["playerRows"]) == 2
    for side, card in enumerate(output["playerRows"]):
        candidates = output["candidates"]["away" if side == 0 else "home"]
        assert len(card["names"]) == 3
        assert candidates[0] not in card["names"]
        assert candidates[3] in card["names"]
        assert candidates[1] in card["names"]
        assert any("若最终出场" in statement for statement in card["statements"])
    assert "出战成疑" in output["lineups"][0]
    assert "大概率缺阵" in output["lineups"][1]
    assert "大概率出战" in output["lineups"][0] or "大概率出战" in output["lineups"][1]
    assert output["newsOnly"] in output["lineups"][1]
    assert output["pageErrors"] == []
    assert output["consoleErrors"] == []
