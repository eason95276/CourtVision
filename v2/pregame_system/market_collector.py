"""Fixed post-independent Market collection; no model or betting execution."""
from __future__ import annotations
import copy
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .sources import get
from .storage import new_run,save,now,instant,content_hash,digest
from .market import events,qualify_prediction_book,normalize_current_market,number
from .web_search import parse_public_market_reference,_market_domain,MARKET_SOURCE_TIER


def market_game(game):
    return {'game_id':game.get('game_id')or game.get('id'),
        'scheduled_tipoff':game.get('scheduled_tipoff')or game.get('tipoff'),
        'home_team_id':int((game.get('home')or{}).get('id',game.get('home_team_id'))),
        'away_team_id':int((game.get('away')or{}).get('id',game.get('away_team_id'))),
        'home_team':(game.get('home')or{}).get('name')or game.get('home_team'),
        'away_team':(game.get('away')or{}).get('name')or game.get('away_team'),
        'game_type':game.get('game_type'),'registered_game':True}


def normalized_records(reference,game):
    identity=market_game(game)
    rows=[]
    for side in ('home','away'):
        line=reference.get(side+'_spread')
        if line is not None:rows.append({'market_type':'spread','side':side,'line':line,'price':reference.get(side+'_spread_price')})
        price=reference.get(side+'_moneyline')
        if price is not None:rows.append({'market_type':'moneyline','side':side,'line':None,'price':price})
    if reference.get('total') is not None:rows.append({'market_type':'total','side':'game','line':reference['total'],'price':None})
    for row in rows:
        row.update(game_identity=identity,source=reference['source_url'],sportsbook=reference.get('sportsbook'),
            published_at=reference.get('published_at'),observed_at=reference.get('observed_at'),captured_at=reference['captured_at'],
            raw_response_sha256=reference.get('raw_response_sha256'),qualification='identity_direction_source_time_checked')
    return rows


def normalize_book_market(data,event,receipt,cutoff):
    if receipt.get('status')!='success' or not receipt.get('captured_at'):return None
    if instant(receipt['captured_at'])>instant(cutoff) or instant(cutoff)>=instant(event['scheduled_tipoff']):return None
    if receipt.get('raw_path') and digest(receipt['raw_path'])!=receipt.get('sha256'):raise ValueError('market_raw_hash_mismatch')
    qualified=[]
    for item in (data or{}).get('items',[]):
        if not qualify_prediction_book(item.get('provider'))['eligible']:continue
        def ref(side):
            match=re.search(r'/teams/(\d+)',item.get(side+'TeamOdds',{}).get('team',{}).get('$ref',''))
            return match.group(1)if match else None
        if ref('home')!=event['home_espn_id'] or ref('away')!=event['away_espn_id']:continue
        # Root spread is provider-specific and may be favorite-positive: never
        # guess its home sign. Only explicit paired current team handicaps count.
        value=copy.deepcopy(item);value.pop('spread',None)
        hs=number(value,'homeTeamOdds','current','pointSpread','american')
        away=number(value,'awayTeamOdds','current','pointSpread','american')
        if hs is None or away is None or abs(hs+away)>1e-8:
            value.get('homeTeamOdds',{}).get('current',{}).pop('pointSpread',None)
            value.get('awayTeamOdds',{}).get('current',{}).pop('pointSpread',None)
        qualified.append(value)
    quote=normalize_current_market({'items':qualified},event,receipt,cutoff)
    if not quote:return None
    reference={'status':'available','usage':'qualified_market_for_ai_review','eligible_prediction_input':False,
        'game_identity':market_game(event),'home_spread':quote['home_spread'],
        'away_spread':-quote['home_spread'] if quote['home_spread']is not None else None,
        'expected_home_margin':quote['expected_home_margin'],'total':quote['total'],
        'home_moneyline':quote['home_moneyline'],'away_moneyline':quote['away_moneyline'],
        'sportsbook':quote['provider'],'source_url':receipt['url'],'source_title':quote['provider']+' current odds',
        'source_tier':1,'published_at':None,'observed_at':receipt['captured_at'],'captured_at':receipt['captured_at'],
        'raw_response_sha256':receipt.get('sha256'),'time_semantics':'observed current sportsbook quote; publication time unknown'}
    if reference['total']is not None and not 100<=reference['total']<=400:return None
    if reference['home_spread']is not None and abs(reference['home_spread'])>60:return None
    return reference


class MarketCollector:
    def __init__(self,*,searcher=None,fetcher=get,clock=now,root=None):
        self.searcher=searcher;self.fetcher=fetcher;self.clock=clock;self.root=root

    def collect(self,game):
        folder=new_run('market_collector') if self.root is None else Path(self.root)
        folder.mkdir(parents=True,exist_ok=True)
        g=market_game(game);trace=[];reference=None;game_closed=False
        started=self.clock()
        if instant(started)>=instant(g['scheduled_tipoff']):
            result={'status':'unavailable','reason':'pregame_market_window_closed','evidence':[],'records':[],
                'sources':[],'finished_at':started,'path':str(folder)}
            save(folder/'result.json',result);return result
        eid=g['game_id'].removeprefix('espn:')
        try:
            summary_url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid
            odds_url=f'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds'
            with ThreadPoolExecutor(max_workers=2)as pool:
                summary_future=pool.submit(self.fetcher,summary_url);odds_future=pool.submit(self.fetcher,odds_url)
                summary,sr=summary_future.result();odds,receipt=odds_future.result()
            trace.append({'kind':'structured','summary_receipt':sr,'odds_receipt':receipt})
            header=(summary or{}).get('header',{});comp=(header.get('competitions')or[{}])[0]
            if str(header.get('id'))!=eid or str(comp.get('id'))!=eid:raise ValueError('market_event_identity_mismatch')
            board={'events':[{'id':eid,'date':comp['date'],'competitions':[comp]}]}
            candidates=events(board,sr)
            event=next((e for e in candidates if e['game_id']==g['game_id']),None)
            if not event or any(event[k]!=g[k]for k in ('home_team_id','away_team_id')) or instant(event['scheduled_tipoff'])!=instant(g['scheduled_tipoff']):
                raise ValueError('market_schedule_identity_mismatch')
            state=(comp.get('status')or{}).get('type',{}).get('state')
            if state in {'in','post'}:game_closed=True
            if state!='pre':raise ValueError('market_event_not_pregame')
            event['game_type']=g['game_type']
            reference=normalize_book_market(odds,event,receipt,self.clock())
        except Exception as exc:trace.append({'kind':'structured_error','error':type(exc).__name__,'reason':str(exc)[:200]})
        if reference is None and not game_closed and self.searcher is not None and instant(self.clock())<instant(g['scheduled_tipoff']):
            try:
                discovered=self.searcher.search(g,self.clock(),replay=False,categories={'market'})
                urls=list(dict.fromkeys(r['url']for r in discovered.get('records',[])if _market_domain(r.get('url',''))))[:3]
                bodies=self.searcher.fetch_bodies(urls,budget_seconds=35)if urls else []
                trace.append({'kind':'dedicated_market_search','discovery':discovered,'bodies':bodies})
                records={r['url']:r for r in discovered.get('records',[])}
                for body in bodies:
                    if not str(body.get('content')or'').strip():continue
                    url=body['url'];domain=_market_domain(url)
                    if domain is None:continue
                    captured=body.get('captured_at')or self.clock()
                    if instant(captured)>=instant(g['scheduled_tipoff']) or instant(captured)>instant(self.clock()):continue
                    pub=body.get('updated_at')or body.get('published_at')
                    if pub and len(pub)>10 and instant(pub)>instant(captured):continue
                    # A captured, matchup/date/price-bound reference existed
                    # before tipoff even when its publication instant is unknown.
                    # This is observation evidence, not certified latest/live odds.
                    parsed=parse_public_market_reference({'url':url,'title':records[url].get('title',''),
                        'content':body['content'],'published_date':pub},g,captured_at=captured)
                    if parsed:
                        reference={**parsed,'game_identity':g,'usage':'qualified_market_for_ai_review',
                            'sportsbook':{'fanduel.com':'FanDuel','betmgm.com':'BetMGM'}.get(domain),
                            'published_at':body.get('published_at'),'observed_at':captured,
                            'updated_at':body.get('updated_at'),'raw_response_sha256':body.get('raw_response_sha256'),
                            'full_text_sha256':__import__('hashlib').sha256(body['content'].encode('utf-8')).hexdigest(),
                            'publication_precision':'unknown' if not body.get('published_at') else 'date' if len(body['published_at'])==10 else 'timestamp',
                            'latest_quote_verified':False,'time_semantics':'Source page quote observed before tipoff; publication/update clocks retained separately; quote freshness not certified'}
                        break
            except Exception as exc:trace.append({'kind':'web_error','error':type(exc).__name__})
        finished=self.clock()
        if instant(finished)>=instant(g['scheduled_tipoff']):reference=None
        rows=normalized_records(reference,g)if reference else []
        evidence=[];sources=[]
        if reference:
            ident=content_hash({'game':g,'market':reference,'records':rows})
            evidence=[{'id':ident,'section':'web_context','kind':'MarketSnapshot','game_id':g['game_id'],'cutoff':finished,
                'value':{'category':'market','claim_type':'market_reference','eligible_for_fact':True,'market_reference':reference,
                    'records':rows,'quote':f"Market: home handicap {reference.get('home_spread')}; total {reference.get('total')}",
                    'url':reference['source_url'],'captured_at':reference['captured_at'],'published_at':reference.get('published_at')}}]
            sources=[{'id':ident,'url':reference['source_url'],'source_type':'qualified_market_snapshot',
                'captured_at':reference['captured_at'],'published_at':reference.get('published_at'),'sha256':reference.get('raw_response_sha256')}]
        result={'status':'available' if reference else 'unavailable','game_identity':g,'public_market_reference':reference,
            'observed_game_already_started':game_closed,
            'records':rows,'evidence':evidence,'sources':sources,'trace':trace,'started_at':started,'finished_at':finished,'path':str(folder)}
        save(folder/'result.json',result);return result


class BoundedMarketCollector:
    """Optional market I/O cannot consume the remaining publication window."""
    def collect(self,game):
        import subprocess,sys,os
        from .storage import BASE
        folder=new_run('market_collector_bounded');save(folder/'input.json',game)
        remaining=(instant(market_game(game)['scheduled_tipoff'])-instant(now())).total_seconds()
        budget=min(40,max(0,remaining-55))
        if budget<=0:
            result={'status':'unavailable','reason':'market_budget_unavailable','evidence':[],'sources':[],'finished_at':now()}
        else:
            try:
                process=subprocess.run([sys.executable,'-m','v2.pregame_system.market_collector',str(folder/'input.json'),str(folder/'collection')],
                    cwd=BASE.parent.parent,timeout=budget,capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                if process.returncode:raise ValueError('market_subprocess_failed')
                result=__import__('json').loads((folder/'collection'/'result.json').read_text(encoding='utf-8'))
            except Exception as exc:
                result={'status':'unavailable','reason':type(exc).__name__,'evidence':[],'sources':[],'finished_at':now()}
        save(folder/'result.json',result);return result


if __name__=='__main__':
    import sys,json
    from v2.frontend.scheduler_worker import isolate_worker_children
    from v2.frontend.server import load_local_env
    from .web_search import PregameWebSearch
    isolate_worker_children();load_local_env()
    game=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    MarketCollector(searcher=PregameWebSearch(),root=Path(sys.argv[2])).collect(game)
