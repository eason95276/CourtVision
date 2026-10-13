const {chromium}=require(process.argv[2]);
const [base,id,width,height,captures]=process.argv.slice(3);
const prefix=id.replace(/[^a-z0-9-]/gi,'_');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:Number(width),height:Number(height)},reducedMotion:'reduce'});
  const pageErrors=[],consoleErrors=[];let postRequests=0;
  page.on('pageerror',e=>pageErrors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text())});
  page.on('request',r=>{if(r.method()==='POST')postRequests++});
  await page.goto(base+'/#game/'+encodeURIComponent(id),{waitUntil:'domcontentloaded'});
  await page.waitForFunction(id=>state.activeGame?.id===id&&state.currentAnalyses.has(id),id);
  const initial=await page.evaluate(id=>({predictionVersion:state.currentAnalyses.get(id).version_id,
    manualSidecar:state.editorialOverlays.has(id),postgameVersion:state.activeGame.postgame?.version_id,
    marketText:document.querySelector('#hero-market-source').textContent}),id);
  await page.locator('#ai-analysis').click();await page.waitForSelector('.analyst-short-review');
  await page.waitForFunction(()=>document.querySelector('#sheet-content').getAttribute('aria-busy')==='false');
  const reader=await page.locator('#sheet-content').textContent();
  await page.screenshot({path:captures+`/${prefix}-pregame-${width}.png`});
  await page.locator('#close-sheet').click();
  const fixture=await page.evaluate(id=>{
    const game=state.activeGame,version=structuredClone(state.currentAnalyses.get(id)),names={};
    const at=new Date(Date.parse(game.tipoff)-30*60*1000).toISOString();
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:true}).formatToParts(new Date(at)).map(x=>[x.type,x.value]));
    const source=`https://ak-static.cms.nba.com/referee/injury/Injury-Report_${parts.year}-${parts.month}-${parts.day}_${parts.hour}_${parts.minute}${parts.dayPeriod}.pdf`;
    const reports=[];const lineups=[];const claims=[];
    for(const side of ['away','home']) {
      const roster=frozenRoster(game,version,side);if(roster.length<6)throw Error('Frozen source roster too small for fixture');
      names[side]=roster.slice(0,6).map(p=>p.name||p.player_name);
      lineups.push({side,basis:'agent_expected',players:names[side].slice(0,5),candidates:[names[side][5]],evidence_ids:[],rationale:'Fixture ordered role candidates'});
      names[side].slice(0,5).forEach((name,i)=>{
        claims.push({player_name:name,statement:i===2 ? '测试候选：预计打18分钟，观察持球职责。' : `测试候选${i+1}：这个球员的持球或防守角色影响本场对位。`,evidence_ids:[]});
        reports.push({game_id:id,team_id:game[side].id,player_name:name,status:['Out','Questionable','Probable','Doubtful','Available'][i],reason:'fixture',source_url:source,source_published_at:at});
      });
    }
    version.editorial_material=[{pregame_plan:{lineups,player_usage:[],key_matchup:'Fixture',uncertainty:[]}}];
    version.judgment.player_analysis=claims;state.currentAnalyses.set(id,version);
    const fixtureGame={...game,official_injury_report:{status:'published',game_id:id,source_url:source,source_published_at:at,records:reports}};
    renderDetail(fixtureGame);
    const report=officialInjuryReportProjection(fixtureGame,version);
    const checks={keyPlayers:[...document.querySelectorAll('.player-side-list')].map(n=>n.querySelectorAll('.player-entry').length),
      lineupCounts:[...document.querySelectorAll('.lineup-player-list')].map(n=>n.querySelectorAll('.lineup-player').length),
      outReplaced:['away','home'].every(side=>!document.querySelector('#key-players').textContent.includes(playerName({name:names[side][0]}))&&
        [...document.querySelectorAll('.lineup-player-list')].some(n=>n.textContent.includes(playerName({name:names[side][5]})))),
      questionableKept:report.records.filter(r=>r.status==='questionable').every(r=>document.querySelector('#key-players').textContent.includes(playerName(r.player)))&&
        document.querySelector('#key-players').textContent.includes('若最终出场'),
      injuryText:document.querySelector('#injury-context').textContent,
      conditionalSingleMinutes:[...document.querySelectorAll('#key-players .player-entry p')].some(n=>n.textContent.includes('18分钟')&&n.textContent.startsWith('若最终出场')),
      overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth};
    for(const side of ['away','home']) {
      const team=version.input_snapshot.roster_context.teams[side];team.confirmed_starters=[];team.projected_starters=[];team.current_rotation=null;
      const factId='fixture-confirmed-'+side;
      version.analyst_context.objective_evidence.push({id:factId,section:'web_context',value:{claim_type:'confirmed_starter',eligible_for_fact:true,
        game_id:id,team_id:game[side].id,quote:names[side].slice(1,6).join(', ')}});
      team.confirmed_starters={players:names[side].slice(1,6),evidence_ids:[factId]};
    }
    renderLineups(fixtureGame,version,report);
    checks.confirmedPriority=document.querySelector('#lineup-context h3').textContent==='首发阵容';
    for(const side of ['away','home']) {
      const team=version.input_snapshot.roster_context.teams[side];team.confirmed_starters=[];
      const factId='fixture-source-'+side;
      version.analyst_context.objective_evidence.push({id:factId,section:'web_context',value:{claim_type:'projected_starters',eligible_for_fact:true,
        game_id:id,team_id:game[side].id,quote:names[side].slice(1,6).join(', ')}});
      team.projected_starters={players:names[side].slice(1,6),basis:'source_expected',evidence_ids:[factId]};
    }
    renderLineups(fixtureGame,version,report);
    const sourceLabels=[...document.querySelectorAll('.lineup-basis')];
    checks.sourceExpectedPriority=sourceLabels.length===2 && sourceLabels.every(n=>n.textContent==='来源预计');
    for(const side of ['away','home'])version.input_snapshot.roster_context.teams[side].projected_starters=[];
    lineups.forEach(row=>row.candidates=[]);renderLineups(fixtureGame,version,report);
    checks.insufficientCandidateState=document.querySelector('#lineup-context').textContent.includes('预计首发信息不足') &&
      [...document.querySelectorAll('.lineup-player-list')].every(n=>n.querySelectorAll('.lineup-player').length===4);
    lineups.forEach(row=>row.candidates=[names[row.side][5]]);renderLineups(fixtureGame,version,report);
    return checks;
  },id);
  await page.locator('#lineup-context').scrollIntoViewIfNeeded();
  await page.screenshot({path:captures+`/${prefix}-injury-candidates-fixture-${width}.png`});
  await page.locator('#key-players').scrollIntoViewIfNeeded();
  await page.screenshot({path:captures+`/${prefix}-key-players-fixture-${width}.png`});
  await page.reload();await page.waitForFunction(id=>state.currentAnalyses.has(id),id);
  const restoredVersion=await page.evaluate(id=>state.currentAnalyses.get(id).version_id,id);
  await page.locator('#postgame-entry button').click();await page.waitForSelector('.postgame-reader-article');
  const pg=await page.evaluate(()=>({postgameReader:document.querySelector('#sheet-content').textContent,
    settlementSummary:document.querySelector('.postgame-short-review').textContent,
    overflow:document.querySelector('.analysis-sheet').scrollWidth>document.querySelector('.analysis-sheet').clientWidth}));
  await page.screenshot({path:captures+`/${prefix}-postgame-fixture-${width}.png`});
  console.log(JSON.stringify({...initial,...fixture,...pg,reader,restoredVersion,pageErrors,consoleErrors,postRequests}));
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
