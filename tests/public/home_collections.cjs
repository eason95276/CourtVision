const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({ console, URL, Intl, Date, Map, Set,
  document: { addEventListener() {} }, window: {} });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext(`
  const sample = (id, tipoff) => ({id, tipoff, home:{id:1}, away:{id:2}});
  state.data = {schedule_window:{today:'2026-10-15',tomorrow:'2026-10-16'}, games:[
    sample('espn:401909088','2026-10-20T19:00:00Z'),
    sample('espn:401909089','2026-10-20T23:00:00Z'),
    sample('espn:401909090','2026-10-21T01:30:00Z'),
    sample('old','2026-10-14T02:00:00Z'),
    sample('day-two','2026-10-22T01:00:00Z'),
    sample('unpublished','2026-10-14T03:00:00Z'),
    sample('other-opening','2026-10-21T03:00:00Z')
  ]};
  for (const game of state.data.games.filter(g=>g.id!=='unpublished'))
    state.currentAnalyses.set(game.id, {version_id:'version-'+game.id});
`, context);
const run = code => vm.runInContext(code, context);
assert.deepEqual(Array.from(run('homeCollectionOptions().map(x=>x.key)')), ['opening', 'published']);
assert.equal(run('homeCollectionOptions()[0].subtitle'), '10月21日');
assert.equal(run('homeCollectionOptions()[0].label'), '揭幕战');
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,'opening')).map(g=>g.id)")),
  ['espn:401909088', 'espn:401909089', 'espn:401909090']);
assert.deepEqual(Array.from(run("state.data.games.filter(g=>gameInHomeCollection(g,'published')).map(g=>g.id)")), ['old']);
run("state.data.games[0].tipoff='2026-10-22T01:00:00Z'");
assert.equal(run("gameInHomeCollection(state.data.games[0],'opening')"), false);
run("state.data.games[0].tipoff='2026-10-20T19:00:00Z'; state.data.games[0].homepage_visible=false");
assert.equal(run("gameInHomeCollection(state.data.games[0],'opening')"), true);
assert.equal(run("gameInHomeCollection(state.data.games.find(g=>g.id==='unpublished'),'published')"), false);
console.log('Home collections: fixed first day, timezone, archive, unavailable and day-two exclusions passed');
