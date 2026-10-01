"""Action Network small probes and ESPN-gap dates; never mix bookmaker quotes."""
import json
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from .market import cached_get,IDS,ALIASES,match,probability
from .features import history
from .prediction import latest
from .storage import save,new_run


def normalize(data,receipt,lookup,books):
    output=[]
    for g in (data or {}).get('games',[]):
        teams={t['id']:t for t in g['teams']}
        def tid(side):
            abbr=teams[g[side+'_team_id']]['abbr'].lower();return IDS[ALIASES.get(abbr,abbr)]
        try:
            identity=match({'home_team_id':tid('home'),'away_team_id':tid('away'),'scheduled_tipoff':g['start_time']},lookup)
        except KeyError:continue
        if not identity:continue
        for bid in ['15','30']:
            market=g.get('markets',{}).get(bid,{}).get('event',{})
            if not market:continue
            def value(kind,side,field='value'):
                selected=[r for r in market.get(kind,[]) if r.get('side')==side and r.get('period')=='event'
                          and not r.get('is_live',False) and str(r.get('book_id'))==bid]
                if kind in ['spread','moneyline']:
                    selected=[r for r in selected if r.get('team_id')==g[side+'_team_id']]
                return float(selected[0][field]) if len(selected)==1 else None
            hs=value('spread','home');ass=value('spread','away');total=value('total','over');under=value('total','under')
            hp=probability(value('moneyline','home','odds'));ap=probability(value('moneyline','away','odds'))
            output.append({**identity,'provider':'Action Network','provider_id':bid,'provider_name':books.get(bid),
                'quote_kind':'open' if books.get(bid)=='Open' else 'consensus_archive_unverified_close',
                'home_spread':hs if hs is not None and ass is not None and abs(hs+ass)<1e-8 else None,
                'total':total if total==under else None,'home_probability_no_vig':hp/(hp+ap) if hp and ap else None,
                'published_at':None,'eligible_prediction_input':False,'benchmark_only':True,
                'source':receipt,'source_event_id':g['id']})
    return output


def run():
    folder=new_run('action_gap_fill');games,_=history();lookup={}
    for g in games.itertuples():lookup.setdefault((str(g.game_date.date()),g.home_team_id,g.away_team_id),[]).append(g.game_id)
    bookdata,br=cached_get('https://api.actionnetwork.com/web/v1/books')
    books={str(b['id']):b['display_name'] for b in (bookdata or {}).get('books',[]) if b['id'] in [15,30]}
    save(folder/'books.json',{'books':books,'source':br})
    quotes=json.loads(latest('market_collection','quotes.json').read_text(encoding='utf8'))
    ids={q['game_id'] for q in quotes}
    gaps=games[(games.season_start>=2023)&~games.game_id.isin(ids)]
    dates=set(gaps.game_date.dt.strftime('%Y%m%d'))|{'20240113','20250113','20260113'}
    def fetch(day):
        d,r=cached_get(f'https://api.actionnetwork.com/web/v2/scoreboard/nba?date={day}')
        rows=normalize(d,r,lookup,books);save(folder/'days'/(day+'.json'),{'quotes':rows,'receipt':r})
        return rows,r
    with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(fetch,sorted(dates)))
    rows=[r for part,_ in result for r in part];save(folder/'quotes.json',rows)
    save(folder/'coverage.json',{'dates_attempted':len(dates),'games_with_quotes':len({r['game_id'] for r in rows}),
        'quote_count':len(rows),'espn_missing_games':len(gaps),'missing_espn_games_with_action_quotes':len(set(gaps.game_id)&{r['game_id'] for r in rows}),
        'prediction_eligible_quotes':0,'unavailable_requests':sum(r['status']!='success' for _,r in result),
        'scope':'three season probes plus missing ESPN dates only; no claim that consensus archive is closing',
        'provider_labels':books})
    print(folder,flush=True)

if __name__=='__main__':run()
