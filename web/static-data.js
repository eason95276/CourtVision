/* Public site: saved JSON reads only; never forwards a generation/API POST. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  const root = new URL('.', document.currentScript.src);
  const cache = new Map();
  window.COURTVISION_PUBLIC = true;
  async function json(path) {
    if (!cache.has(path)) cache.set(path, nativeFetch(new URL(path, root)).then(async r => { if (!r.ok) throw new Error('已发布资料暂不可用'); return r.json(); }));
    return cache.get(path);
  }
  async function all() {
    const index = await json('public_data/index.json');
    return Promise.all(index.games.map(row => json('public_data/' + row.file)));
  }
  const response = (data, status = 200) => new Response(JSON.stringify(data), {status, headers:{'Content-Type':'application/json'}});
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, root);
    const path = url.pathname;
    const method = options.method || (typeof input !== 'string' ? input.method : 'GET') || 'GET';
    if (method !== 'GET' && method !== 'HEAD') return response({status:'unavailable',message:'公开网站只展示已发布内容。'},405);
    if (path.startsWith('/api/') || path === '/app_data.json') {
      const rows = await all();
      const id = url.searchParams.get('game_id');
      const row = rows.find(x => x.game.id === id);
      if (path === '/api/current-analyses') return response({status:'success',predictions:Object.fromEntries(rows.map(x=>[x.game.id,x.prediction]))});
      if (path === '/api/game') return row ? response({status:'success',game:row.game,prediction:row.prediction}) : response({status:'not_found'},404);
      if (path === '/api/postgame') return response(row?.postgame || {status:'unavailable',postgame:null});
      if (path === '/api/market-reference') return response(row?.market || {status:'unavailable',public_market_reference:null});
      if (path === '/api/schedule-window' || path === '/app_data.json') {
        const games = rows.map(x=>x.game);
        const teams = [...new Map(games.flatMap(g=>[g.home,g.away]).map(t=>[t.id,t])).values()];
        const dates = [...new Set(games.map(g=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(g.tipoff))))].sort().reverse();
        return response({games,teams,public_archive:true,published_dates:dates,schedule_window:{today:dates[0],tomorrow:dates[1]||dates[0],status:'published'}});
      }
      return response({status:'unavailable'},404);
    }
    return nativeFetch(input,options);
  };
})();
