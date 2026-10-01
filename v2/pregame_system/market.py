"""ESPN archive adapter: same provider nested Open/Close, never timestamp claims."""
import calendar
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
from .sources import get
from .storage import BASE,new_run,save,content_hash,instant
from .features import history
from courtvision.data import TEAM_ID_TO_ABBR

ALIASES={'gsw':'gs','nyk':'ny','nop':'no','sas':'sa','uta':'utah','was':'wsh'}
IDS={v:k for k,v in TEAM_ID_TO_ABBR.items()}
# Semantic provider whitelist from the captured ESPN provider objects. Priority 0
# includes model forecasts (accuscore/betegy) and live lines, so it is NOT a book rank.
BOOK_ORDER=['58','100','40','47','45','31','36','41','48','50','52','53','57']

# A provider is eligible for an official prediction only when both its ESPN
# provider ID and normalized provider name match this explicit sportsbook list.
# Forecast services, live feeds and unknown providers are rejected even if they
# expose fields that resemble a spread or total.
PREDICTION_BOOKMAKERS={
    '58':{'espnbet'},
    '100':{'draftkings'},
    '40':{'draftkings'},
    '47':{'mgm'},
    '45':{'caesarsnewjersey'},
    '31':{'caesars'},
    '52':{'caesarscolorado'},
    '57':{'caesarstennessee'},
}
BLOCKED_PREDICTION_PROVIDER_TERMS={
    'accuscore','betegy','liveodds','prediction','predictions','forecast','model'
}


def _provider_name(value):
    return re.sub(r'[^a-z0-9]+','',str(value or '').casefold())


def qualify_prediction_book(provider):
    """Return an auditable sportsbook whitelist decision."""
    if not isinstance(provider,dict):
        return {'eligible':False,'reason':'provider_identity_missing','provider_id':None,'provider_name':None}
    provider_id=str(provider.get('id') or '').strip()
    provider_name=str(provider.get('name') or '').strip()
    normalized=_provider_name(provider_name)
    if not provider_id or not normalized:
        return {'eligible':False,'reason':'provider_identity_missing','provider_id':provider_id or None,
            'provider_name':provider_name or None}
    if any(term in normalized for term in BLOCKED_PREDICTION_PROVIDER_TERMS):
        return {'eligible':False,'reason':'provider_category_blocked','provider_id':provider_id,
            'provider_name':provider_name}
    allowed=PREDICTION_BOOKMAKERS.get(provider_id)
    if allowed is None:
        return {'eligible':False,'reason':'provider_id_not_allowlisted','provider_id':provider_id,
            'provider_name':provider_name}
    if normalized not in allowed:
        return {'eligible':False,'reason':'provider_name_not_allowlisted_for_id','provider_id':provider_id,
            'provider_name':provider_name}
    return {'eligible':True,'reason':None,'provider_id':provider_id,'provider_name':provider_name,
        'allowlist_match':normalized}


def validate_prediction_market_quote(market,game,cutoff):
    """Validate provider, game identity, time and supported current quote fields."""
    quote=json.loads(json.dumps(market))
    qualification=qualify_prediction_book({'id':quote.get('provider_id'),'name':quote.get('provider')})
    if not qualification['eligible']:
        raise ValueError('market_provider_not_allowed:'+qualification['reason'])
    quote['provider_qualification']=qualification
    if quote.get('game_id')!=game.get('game_id'):raise ValueError('market_wrong_game')
    if (int(quote.get('home_team_id',-1))!=int(game.get('home_team_id',-2)) or
            int(quote.get('away_team_id',-1))!=int(game.get('away_team_id',-2))):
        raise ValueError('market_wrong_teams')
    if not quote.get('eligible_prediction_input'):raise ValueError('market_not_eligible')
    if not quote.get('captured_at'):raise ValueError('market_missing_capture_time')
    if instant(quote['captured_at'])>instant(cutoff):raise ValueError('market_after_cutoff')
    if quote.get('source_published_at') and instant(quote['source_published_at'])>instant(cutoff):
        raise ValueError('market_published_after_cutoff')
    if instant(cutoff)>=instant(game['scheduled_tipoff']):raise ValueError('market_cutoff_not_pregame')
    usable={}
    for field in ['expected_home_margin','total','home_probability_no_vig']:
        try:value=float(quote.get(field))
        except (TypeError,ValueError):value=None
        usable[field]=value if value is not None and math.isfinite(value) else None
    if all(value is None for value in usable.values()):raise ValueError('eligible_market_has_no_supported_fields')
    if usable['home_probability_no_vig'] is not None and not 0<usable['home_probability_no_vig']<1:
        raise ValueError('market_probability_out_of_range')
    quote.update(usable)
    return quote


def benchmark_quotes(frame):
    frame=frame[frame.provider_id.astype(str).isin(BOOK_ORDER) & ~frame.provider_name.str.contains('Live',case=False)].copy()
    frame['book_order']=frame.provider_id.astype(str).map({bid:i for i,bid in enumerate(BOOK_ORDER)})
    chosen=frame.sort_values(['book_order','provider_id']).drop_duplicates('game_id')[['game_id','provider_id']]
    # One book identity per game, shared across Open/Close. Missing fields stay missing.
    return frame.merge(chosen,on=['game_id','provider_id'],validate='many_to_one').drop_duplicates(['game_id','quote_kind'])


def team_id(competitor):
    abbr=competitor['team']['abbreviation'].lower()
    return IDS[ALIASES.get(abbr,abbr)]


def events(board,receipt):
    output=[]
    for event in (board or {}).get('events',[]):
        comp=event['competitions'][0]
        byside={c['homeAway']:c for c in comp['competitors']}
        if set(byside)!={'home','away'}:continue
        try:
            row={'event_id':str(event['id']),'game_id':'espn:'+str(event['id']),
                'scheduled_tipoff':event['date'],'home_team_id':team_id(byside['home']),
                'away_team_id':team_id(byside['away']),
                'home_team':byside['home']['team']['displayName'],'away_team':byside['away']['team']['displayName'],
                'home_espn_id':str(byside['home']['team']['id']),'away_espn_id':str(byside['away']['team']['id']),
                'status':comp['status']['type']['state'],'source':receipt}
        except KeyError:continue
        output.append(row)
    return output


def number(d,*keys):
    try:
        for k in keys:d=d[k]
        return float(d)
    except (TypeError,KeyError,ValueError):return None


def probability(american):
    if american is None or abs(american)<100:return None
    return (-american)/(-american+100) if american<0 else 100/(american+100)


def normalize_odds(data,event,receipt):
    rows=[]
    if not data:return rows
    for item in data.get('items',[]):
        provider=item.get('provider',{})
        # Team refs must match the scoreboard direction; no winner/score fields used.
        def ref_id(side):return item.get(side+'TeamOdds',{}).get('team',{}).get('$ref','').split('/teams/')[-1].split('?')[0]
        if ref_id('home')!=event['home_espn_id'] or ref_id('away')!=event['away_espn_id']:continue
        for label in ['open','close']:
            home=item.get('homeTeamOdds',{}).get(label,{})
            away=item.get('awayTeamOdds',{}).get(label,{})
            hs=number(home,'pointSpread','american'); ass=number(away,'pointSpread','american')
            hm=number(home,'moneyLine','american');am=number(away,'moneyLine','american')
            hp=probability(hm);ap=probability(am)
            total=number(item,label,'total','american')
            spread_valid=hs is not None and ass is not None and abs(hs+ass)<1e-8
            if not spread_valid and total is None and hp is None:continue
            rows.append({'game_id':event['game_id'],'event_id':event['event_id'],
                'home_team_id':event['home_team_id'],'away_team_id':event['away_team_id'],
                'scheduled_tipoff':event['scheduled_tipoff'],'provider_id':str(provider.get('id')),
                'provider_name':provider.get('name'),'provider_priority':provider.get('priority',999),
                'quote_kind':label,'home_spread':hs if spread_valid else None,
                'expected_home_margin':-hs if spread_valid else None,'total':total,
                'home_moneyline':hm,'away_moneyline':am,
                'home_probability_no_vig':hp/(hp+ap) if hp and ap else None,
                'published_at':None,'time_semantics':'retrospectively_fetched_archive_label_only',
                'eligible_prediction_input':False,'benchmark_only':True,'source':receipt})
    return rows


def _current_number(item, side, field):
    if side:
        current=item.get(side+'TeamOdds',{}).get('current',{})
    else:
        current=item.get('current',{})
    return number(current,*field)


def normalize_current_market(data,event,receipt,cutoff):
    """Select one current provider quote captured before the requested cutoff."""
    if not data or receipt.get('status')!='success' or not receipt.get('captured_at'):
        return None
    if instant(receipt['captured_at'])>instant(cutoff):
        return None
    candidates=[]
    for item in data.get('items',[]):
        provider=item.get('provider',{})
        qualification=qualify_prediction_book(provider)
        if not qualification['eligible']:continue
        home_ref=item.get('homeTeamOdds',{}).get('team',{}).get('$ref','')
        away_ref=item.get('awayTeamOdds',{}).get('team',{}).get('$ref','')
        def ref(value):
            match=re.search(r'/teams/(\d+)',value)
            return match.group(1) if match else None
        if ref(home_ref) and ref(home_ref)!=event.get('home_espn_id'):continue
        if ref(away_ref) and ref(away_ref)!=event.get('away_espn_id'):continue
        home_spread=_current_number(item,'home',('pointSpread','american'))
        if home_spread is None:home_spread=number(item,'spread')
        total=_current_number(item,None,('total','american'))
        if total is None:total=number(item,'overUnder')
        home_ml=_current_number(item,'home',('moneyLine','american'))
        away_ml=_current_number(item,'away',('moneyLine','american'))
        if home_ml is None:home_ml=number(item,'homeTeamOdds','moneyLine')
        if away_ml is None:away_ml=number(item,'awayTeamOdds','moneyLine')
        hp=probability(home_ml);ap=probability(away_ml)
        values=[home_spread,total,hp]
        if all(v is None or not math.isfinite(float(v)) for v in values):continue
        quote={'game_id':event['game_id'],'event_id':event.get('event_id'),
            'home_team_id':event['home_team_id'],'away_team_id':event['away_team_id'],
            'scheduled_tipoff':event['scheduled_tipoff'],'provider_id':str(provider.get('id')),
            'provider':provider.get('name'),'home_spread':home_spread,
            'expected_home_margin':-home_spread if home_spread is not None else None,
            'total':total,'home_moneyline':home_ml,'away_moneyline':away_ml,
            'home_probability_no_vig':hp/(hp+ap) if hp and ap else None,
            'captured_at':receipt['captured_at'],'source_published_at':None,
            'eligible_prediction_input':qualification['eligible'] and instant(receipt['captured_at'])<=instant(cutoff)<instant(event['scheduled_tipoff']),
            'provider_qualification':qualification,
            'time_semantics':'current quote captured by CourtVision before cutoff; provider publication time unavailable',
            'source':{k:receipt.get(k) for k in ['url','captured_at','raw_path','sha256','http_status','status']}}
        candidates.append(quote)
    if not candidates:return None
    order={bid:i for i,bid in enumerate(BOOK_ORDER)}
    candidates.sort(key=lambda q:(order.get(q['provider_id'],999),q['provider_id']))
    return candidates[0]


def save_live_market_snapshot(quote,root=None):
    root=Path(root or BASE/'outputs/live_market')/(quote['game_id'].replace(':','_'))
    root.mkdir(parents=True,exist_ok=True)
    value=dict(quote);value['document_hash']=content_hash(value)
    path=root/(value['document_hash']+'.json')
    if not path.exists():save(path,value)
    return path


def latest_legal_market(game,cutoff,root=None):
    folder=Path(root or BASE/'outputs/live_market')/(game['game_id'].replace(':','_'))
    candidates=[]
    for path in folder.glob('*.json') if folder.exists() else []:
        try:
            value=json.loads(path.read_text(encoding='utf8'));expected=value.pop('document_hash')
            if content_hash(value)!=expected:continue
            value=validate_prediction_market_quote(value,game,cutoff)
            candidates.append((instant(value['captured_at']),value,path))
        except (ValueError,KeyError,TypeError,OSError,json.JSONDecodeError):continue
    if not candidates:return None
    _,quote,path=max(candidates,key=lambda row:row[0])
    return {'as_of_cutoff':quote,'historical_reference':None,
        'usage_mode':'latest_timestamp_qualified_local_quote',
        'courtvision_capture_time_verified':True,'provider_publication_time_verified':False,
        'exact_quote_time_verified':False,
        'source':quote.get('source'),'snapshot_path':str(path),
        'model_market_disagreement':None,'missing_fields':[k for k in
            ['expected_home_margin','total','home_probability_no_vig'] if quote.get(k) is None]}


def refresh_live_market(event,cutoff,fetcher=get,root=None):
    url=f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{event['event_id']}/competitions/{event['event_id']}/odds"
    data,receipt=fetcher(url)
    quote=normalize_current_market(data,event,receipt,cutoff)
    if quote is None:
        return {'status':'unavailable','reason':'no_eligible_current_quote','receipt':receipt}
    path=save_live_market_snapshot(quote,root=root)
    return {'status':'success','quote':quote,'path':str(path),'receipt':receipt}


_CACHE=None


def cached_get(url):
    # Exact URL, immutable receipt; no retries even for unavailable URLs in this stage.
    global _CACHE
    if _CACHE is None:
        _CACHE={}
        for p in (BASE/'outputs/network').glob('*/receipt.json'):
            r=json.loads(p.read_text(encoding='utf8'));_CACHE[r['url']]=r
        for p in (BASE/'outputs/network').glob('*/attempt.json'):
            if not p.with_name('receipt.json').exists():
                r=json.loads(p.read_text(encoding='utf8'))
                _CACHE.setdefault(r['url'],{'url':r['url'],'status':'interrupted','retry_count':0})
    if url in _CACHE:
        r=_CACHE[url];raw=r.get('raw_path')
        return (json.loads(open(raw,encoding='utf8').read()) if raw and r['status']=='success' else None),r
    data,r=get(url);_CACHE[url]=r
    return data,r


def match(event, lookup):
    day=str(pd.Timestamp(event['scheduled_tipoff']).tz_convert('America/New_York').date())
    candidates=lookup.get((day,event['home_team_id'],event['away_team_id']),[])
    if len(candidates)!=1:return None
    return {**event,'game_id':candidates[0]}


def collect():
    run=new_run('market_collection');games,_=history();lookup={}
    for g in games.itertuples():lookup.setdefault((str(g.game_date.date()),g.home_team_id,g.away_team_id),[]).append(g.game_id)
    inventory=[];failures=[];rows=[]
    for season in [2023,2024,2025]:
        for year,month in [(season,m) for m in [10,11,12]]+[(season+1,m) for m in range(1,7)]:
            dates=f'{year}{month:02d}01-{year}{month:02d}{calendar.monthrange(year,month)[1]}'
            board,r=cached_get(f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={dates}&limit=1000')
            if not board:failures.append(r);continue
            for event in events(board,r):
                matched=match(event,lookup)
                if matched:inventory.append(matched)
    inventory=list({e['game_id']:e for e in inventory}.values())
    save(run/'inventory.json',inventory)
    def fetch(e):
        eid=e['event_id'];d,r=cached_get(f'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds')
        return normalize_odds(d,e,r),r
    # Bounded public reads; no retries. Failed samples are retained, not synthesized.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending={pool.submit(fetch,e):e for e in inventory[:4500]}
        for i,f in enumerate(as_completed(pending),1):
            quotes,r=f.result();rows.extend(quotes)
            save(run/'games'/(pending[f]['game_id']+'.json'),{'quotes':quotes,'receipt':r})
            if r['status']!='success':failures.append(r)
            if i%100==0:print(f'odds {i}/{len(pending)}, quotes {len(rows)}, failures {len(failures)}',flush=True)
    save(run/'quotes.json',rows)
    save(run/'coverage.json',{'requested_seasons':[2023,2024,2025],'matched_games':len(inventory),
        'games_with_quotes':len({r['game_id'] for r in rows}),'quote_count':len(rows),'failures':failures,
        'prediction_eligible_quotes':0,'source':'ESPN Core','truncated_by_request_cap':len(inventory)>4500})
    return run


def future_schedule():
    run=new_run('future_schedule')
    board,r=cached_get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261020-20261027&limit=1000')
    result=[dict(e,season_start=2026,is_playoffs=0,status='scheduled') for e in events(board,r) if e['status']=='pre']
    save(run/'games.json',result);save(run/'receipt.json',r)
    return run


if __name__=='__main__':
    import sys
    print(future_schedule() if '--schedule' in sys.argv else collect(),flush=True)
