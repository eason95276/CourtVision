const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({ console, URL, Intl, Date, Map, Set,
  document: { addEventListener() {} }, window: {} });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext(`
  const sample = (id, tipoff) => ({id, tipoff, home:{id:1}, away:{id:2}});
  state.todayDate='2026-10-21';state.tomorrowDate='2026-10-22';
  state.data = {schedule_window:{today:state.todayDate,tomorrow:state.tomorrowDate}, games:[
    sample('espn:401909088','2026-10-20T19:00:00Z'),
    sample('espn:401909089','2026-10-20T23:00:00Z'),
    sample('espn:401909090','2026-10-21T01:30:00Z'),
    sample('old','2026-10-14T02:00:00Z'),
    sample('day-two','2026-10-22T01:00:00Z'),
    sample('unpublished','2026-10-14T03:00:00Z'),
    sample('other-opening','2026-10-21T03:00:00Z'),
    {...sample('preseason-report','2026-10-04T03:00:00Z'),game_type:'preseason'}
  ]};
  for (const game of state.data.games.filter(g=>!['unpublished','day-two'].includes(g.id)))
    state.currentAnalyses.set(game.id, {version_id:'version-'+game.id});
`, context);
const run = code => vm.runInContext(code, context);
assert.deepEqual(Array.from(run('homeCollectionOptions().map(x=>x.key)')), ['2026-10-21','2026-10-22','published']);
assert.equal(run('homeCollectionOptions()[0].label'), '今天');
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,state.todayDate)).map(g=>g.id)")),
  ['espn:401909088','espn:401909089','espn:401909090','other-opening']);
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,state.tomorrowDate)).map(g=>g.id)")),['day-two']);
assert.equal(run("gameInHomeCollection(state.data.games.find(g=>g.id==='day-two'),'published')"),false);
run("state.data.games[0].tipoff='2026-10-22T01:00:00Z'");
assert.equal(run("gameInHomeCollection(state.data.games[0],state.todayDate)"), false);
assert.equal(run("gameInHomeCollection(state.data.games[0],state.tomorrowDate)"), true);
run("state.data.games[0].tipoff='2026-10-20T19:00:00Z'; state.data.games[0].homepage_visible=false");
assert.equal(run("gameInHomeCollection(state.data.games[0],state.todayDate)"), false);
assert.equal(run("gameInHomeCollection(state.data.games.find(g=>g.id==='unpublished'),'published')"), false);
assert.equal(run("gameInHomeCollection(state.data.games.find(g=>g.id==='preseason-report'),'published')"), false);
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,'published')).sort((a,b)=>compareHomeGames(a,b,'published')).map(g=>g.id)")),
  ['other-opening','espn:401909090','espn:401909089','espn:401909088','old']);
run("state.data.games.find(g=>g.id==='old').home.id=99; state.favoriteTeamId=99");
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,'published')).sort((a,b)=>compareHomeGames(a,b,'published')).map(g=>g.id)")),
  ['other-opening','espn:401909090','espn:401909089','espn:401909088','old']);
console.log('Daily collections: all schedule facts, published reports, visibility and rescheduling passed');
run(`state.testRecentGame={id:'verified-sample',tipoff:'2026-10-26T01:00:00Z',game_type:'regular_season',
  season_form:{status:'available',source:'schedule_catalog_final_games',competition_scope:'regular_season',cutoff:'2026-10-25T20:00:00Z',teams:{home:{
    current_season_games:2,current_season_record:{wins:2,losses:0},current_last5:{status:'available',games:2,wins:2,losses:0},streak:{result:'win',games:2}}}}};`);
const lines=Array.from(run("recentStatusLines(state.testRecentGame,{input_cutoff_at:'2026-10-25T20:00:00Z'},'home')"));
assert.ok(lines[0].includes('赛前已核实2场常规赛'));
assert.ok(!lines.join('').includes('当前连胜'));
assert.equal(run("settlementLabel('no_edge')"),'未给方向');
