const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let now=Date.parse('2026-10-21T15:59:00Z');
let tick=10;
class ClockDate extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const game=(id,tip,selected=false)=>({id,tipoff:tip,status:'scheduled',report_selected:selected,home:{id:1},away:{id:2}});
const published={game:game('published','2026-10-21T01:30:00Z'),prediction:{version_id:'v1',version:{version_id:'v1',payload_sha256:'frozen',game_identity:{scheduled_tipoff:'2026-10-21T01:30:00Z'}}},public_sha256:'public-hash'};
const original=JSON.stringify(published);
const index={published_at:'now',games:[{game_id:'published',file:'games/published.json',public_sha256:'public-hash',home_record:published}]};
const schedule={complete:true,coverage_dates:['2026-10-21','2026-10-22','2026-10-23'],games:[published.game,
  game('selected','2026-10-21T23:00:00Z',true),game('unselected','2026-10-22T01:00:00Z'),game('next-day','2026-10-22T23:00:00Z')]};
const fakeFetch=async input=>{const path=new URL(input).pathname;return new Response(JSON.stringify(path.endsWith('index.json')?index:path.endsWith('schedule.json')?schedule:published));};
const context=vm.createContext({URL,Intl,Date:ClockDate,Map,Set,Response,console,performance:{now:()=>tick},
  document:{currentScript:{src:'https://example.test/static-data.js'}},window:{fetch:fakeFetch}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const call=async path=>(await context.window.fetch('https://example.test'+path)).json();
(async()=>{
  let result=await call('/api/schedule-window');
  assert.equal(result.schedule_window.today,'2026-10-21');assert.equal(result.schedule_window.tomorrow,'2026-10-22');
  assert.equal(result.schedule_window.tomorrow_count,2);
  let detail=await call('/api/game?game_id=unselected');assert.equal(detail.status,'success');assert.equal(detail.prediction.version,null);assert.equal(detail.prediction.status,'unavailable');
  detail=await call('/api/game?game_id=selected');assert.equal(detail.prediction.status,'pending');assert.equal(detail.prediction.version,null);
  detail=await call('/api/game?game_id=published');assert.equal(detail.prediction.version.payload_sha256,'frozen');
  now=Date.parse('2026-10-21T16:01:00Z');result=await call('/api/schedule-window');
  assert.equal(result.schedule_window.today,'2026-10-22');assert.equal(result.schedule_window.tomorrow,'2026-10-23');
  assert.equal(result.schedule_window.today_count,2);assert.equal(result.schedule_window.tomorrow_count,1);
  assert.equal((await context.window.fetch('https://example.test/api/generate',{method:'POST'})).status,405);
  assert.equal(JSON.stringify(published),original);
  schedule.games[0]={...published.game,tipoff:'2026-10-22T01:30:00Z'};
  tick+=31000;
  detail=await call('/api/game?game_id=published');assert.equal(detail.prediction.status,'schedule_changed');assert.equal(detail.prediction.version,null);
  console.log('Public schedule: Beijing rollover, all fixtures, selected-only pending, immutable reports and no writes passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
