/* Maps the local reader's API calls to published JSON; writes stay disabled. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  const root = new URL('.', document.currentScript.src);
  const cache = new Map();
  let indexValue = null, indexReadAt = 0;
  let scheduleValue = null, scheduleReadAt = 0;
  window.COURTVISION_PUBLIC = true;

  async function json(path) {
    if (!cache.has(path)) {
      cache.set(path, nativeFetch(new URL(path, root)).then(async r => {
        if (!r.ok) throw new Error('已发布资料暂不可用');
        return r.json();
      }));
    }
    return cache.get(path);
  }

  async function all() {
    const index = await catalog();
    return Promise.all(index.games.map(row => json('public_data/' + row.file)));
  }

  async function catalog() {
    if (!indexValue || performance.now() - indexReadAt > 30000) {
      const response = await nativeFetch(
        new URL('public_data/index.json', root), {cache:'no-cache'}
      );
      if (!response.ok) throw new Error('已发布资料暂不可用');
      const next = await response.json();
      if (indexValue) {
        for (const row of next.games) {
          const old = indexValue.games.find(item => item.game_id === row.game_id);
          if (old?.public_sha256 !== row.public_sha256) {
            cache.delete('public_data/' + row.file);
          }
        }
      }
      indexValue = next;
      indexReadAt = performance.now();
    }
    return indexValue;
  }

  const response = (data, status = 200) => new Response(
    JSON.stringify(data), {status, headers:{'Content-Type':'application/json'}}
  );

  async function schedule() {
    if (!scheduleReadAt || performance.now() - scheduleReadAt > 30000) {
      scheduleReadAt = performance.now();
      try {
        const result = await nativeFetch(new URL('public_data/schedule.json', root), {cache:'no-cache'});
        if (result.ok) scheduleValue = await result.json();
      } catch (_) { /* Retain the last successfully read facts during a network interruption. */ }
    }
    return scheduleValue;
  }

  const chinaDay = at => new Intl.DateTimeFormat('en-CA', {
    timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'
  }).format(at);

  function withFacts(record, fact) {
    if (!fact) return record;
    const old = record || {};
    let prediction = old.prediction || {status:fact.report_selected?'pending':'unavailable',
      game_id:fact.id,version_id:null,version:null,state_revision:0};
    const frozenTip = prediction.version?.game_identity?.scheduled_tipoff || old.game?.tipoff;
    if (prediction.version && new Date(frozenTip).getTime() !== new Date(fact.tipoff).getTime()) {
      prediction = {status:'schedule_changed',game_id:fact.id,version_id:null,version:null};
    }
    return {...old,game:{...old.game,...fact,
      registered_game:old.game?.registered_game || false,
      postgame:old.game?.postgame || fact.postgame},prediction,
      postgame:old.postgame || {status:'unavailable',postgame:null},
      market:old.market || {status:'unavailable',public_market_reference:null}};
  }

  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, root);
    const path = url.pathname;
    const method = options.method || (typeof input !== 'string' ? input.method : 'GET') || 'GET';
    if (method !== 'GET' && method !== 'HEAD') {
      return response({status:'unavailable',message:'公开网站只展示已发布内容。'}, 405);
    }
    if (path.startsWith('/api/') || path === '/app_data.json') {
      const index = await catalog();
      const light = index.games.every(row => row.home_record);
      const rows = light ? index.games.map(row => row.home_record) : await all();
      const board = await schedule();
      const facts = board?.games || [];
      const merged = new Map(rows.map(row => [row.game.id,row]));
      for (const fact of facts) merged.set(fact.id,withFacts(merged.get(fact.id),fact));
      const id = url.searchParams.get('game_id');
      let row = merged.get(id);
      if (['/api/game','/api/postgame','/api/market-reference'].includes(path) && row) {
        const entry = index.games.find(item => item.game_id === id);
        if (entry) {
          row = await json('public_data/' + entry.file);
          if (row.public_sha256 !== entry.public_sha256) {
            throw new Error('发布版本尚在更新，请稍后重试');
          }
          row = withFacts(row,facts.find(f => f.id === id));
        }
      }
      if (path === '/api/current-analyses') {
        const selections = await Promise.all(index.games.map(async (entry, i) => {
          const loaded = cache.get('public_data/' + entry.file);
          const record = loaded ? await loaded : rows[i];
          return [entry.game_id,withFacts(record,facts.find(f => f.id===entry.game_id)).prediction];
        }));
        for (const fact of facts) if (!index.games.some(e => e.game_id===fact.id)) {
          selections.push([fact.id,withFacts(null,fact).prediction]);
        }
        return response({status:'success',predictions:Object.fromEntries(selections)});
      }
      if (path === '/api/game') {
        return row
          ? response({status:'success',game:row.game,prediction:row.prediction})
          : response({status:'not_found'}, 404);
      }
      if (path === '/api/postgame') {
        return response(row?.postgame || {status:'unavailable',postgame:null});
      }
      if (path === '/api/market-reference') {
        return response(row?.market || {status:'unavailable',public_market_reference:null});
      }
      if (path === '/api/schedule-window' || path === '/app_data.json') {
        const games = [...merged.values()].map(x => x.game);
        const teams = [...new Map(
          games.flatMap(g => [g.home,g.away]).map(t => [t.id,t])
        ).values()];
        const dates = [...new Set(games.map(g => chinaDay(new Date(g.tipoff))))].sort().reverse();
        const today = chinaDay(new Date());
        const tomorrow = chinaDay(new Date(Date.parse(`${today}T12:00:00+08:00`) + 86400000));
        return response({
          games,teams,public_archive:true,public_revision:index.published_at,
          published_dates:dates,
          schedule_window:{today,tomorrow,status:board?.complete && board.coverage_dates.includes(today) && board.coverage_dates.includes(tomorrow)?'ready':'stale',
            today_count:games.filter(g=>chinaDay(new Date(g.tipoff))===today).length,
            tomorrow_count:games.filter(g=>chinaDay(new Date(g.tipoff))===tomorrow).length}
        });
      }
      return response({status:'unavailable'}, 404);
    }
    return nativeFetch(input, options);
  };
})();
