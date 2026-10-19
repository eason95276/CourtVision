/* Maps the local reader's API calls to published JSON; writes stay disabled. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  const root = new URL('.', document.currentScript.src);
  const cache = new Map();
  let indexValue = null, indexReadAt = 0;
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
      const id = url.searchParams.get('game_id');
      let row = rows.find(x => x.game.id === id);
      if (['/api/game','/api/postgame','/api/market-reference'].includes(path) && row) {
        const entry = index.games.find(item => item.game_id === id);
        row = await json('public_data/' + entry.file);
        if (row.public_sha256 !== entry.public_sha256) {
          throw new Error('发布版本尚在更新，请稍后重试');
        }
      }
      if (path === '/api/current-analyses') {
        const selections = await Promise.all(index.games.map(async (entry, i) => {
          const loaded = cache.get('public_data/' + entry.file);
          return [entry.game_id, loaded ? (await loaded).prediction : rows[i].prediction];
        }));
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
        const games = rows.map(x => x.game);
        const teams = [...new Map(
          games.flatMap(g => [g.home,g.away]).map(t => [t.id,t])
        ).values()];
        const dates = [...new Set(games.map(g => new Intl.DateTimeFormat(
          'en-CA', {
            timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'
          }
        ).format(new Date(g.tipoff))))].sort().reverse();
        return response({
          games,teams,public_archive:true,public_revision:index.published_at,
          published_dates:dates,
          schedule_window:{today:dates[0],tomorrow:dates[1]||dates[0],status:'published'}
        });
      }
      return response({status:'unavailable'}, 404);
    }
    return nativeFetch(input, options);
  };
})();
