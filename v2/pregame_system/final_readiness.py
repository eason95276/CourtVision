"""HTTP-only final readiness; unknown facts never consume a generation attempt."""
from __future__ import annotations
import copy,json,sqlite3,hashlib,urllib.request
from datetime import datetime,timedelta
from pathlib import Path
from .prediction_versions import game_identity,VersionStoreError,_instant,_utc
from .storage import content_hash,now

CHECK_LEADS=(20,15,10,5)
CLEAR={'available','active','out','inactive'}
UNCERTAIN={'unknown','questionable','doubtful','probable','unavailable'}

def key(name):return ' '.join(str(name).casefold().split())
def clock_value(clock):return _instant(clock() if clock else now(),'readiness_clock')
def next_check(tipoff,at):
    tip=_instant(tipoff,'tipoff');at=_instant(at,'at')
    for lead in CHECK_LEADS:
        slot=tip-timedelta(minutes=lead)
        if slot>at:return _utc(slot)
    return _utc(min(tip,at+timedelta(seconds=30)))

def important_players(early):
    players={side:set()for side in ('home','away')}
    for row in early.get('editorial_material')or[]:
        plan=row.get('pregame_plan')or{}
        for lineup in plan.get('lineups',[]):
            if lineup.get('side')in players:players[lineup['side']].update(lineup.get('players')or[])
        for usage in plan.get('player_usage',[]):
            if usage.get('side')in players and usage.get('player_name'):players[usage['side']].add(usage['player_name'])
    game=early.get('input_snapshot')or{}
    aliases={r.get('display_name_zh'):r.get('canonical_name_en') for r in game.get('player_display_names')or[] if isinstance(r,dict)}
    for row in early.get('judgment',{}).get('player_analysis',[]):
        name=aliases.get(row.get('player_name'))or row.get('player_name')
        for side in players:
            roster=((game.get('roster_context')or{}).get('teams')or{}).get(side,{})
            names=[p.get('name')or p.get('player_name') for p in (roster.get('official_team_roster')or{}).get('players',[])]
            if key(name)in {key(x)for x in names}:players[side].add(name)
    return {s:sorted(v)for s,v in players.items()}

def evaluate_readiness(game,early,snapshot,at,*,market_max_age=300,injury_max_age=3600):
    at=_instant(_utc(at)if isinstance(at,datetime)else at,'at');tip=_instant(game['tipoff'],'tipoff');reasons=[]
    identity=game_identity(game)
    if at>=tip or snapshot.get('game_state')in {'in','post','live','in_progress','final'}:reasons.append('game_started_or_cutoff_reached')
    if snapshot.get('game_identity')!=identity:reasons.append('snapshot_game_identity_mismatch')
    if snapshot.get('schedule_revision',1)!=game.get('schedule_revision',1):reasons.append('snapshot_schedule_revision_mismatch')
    try:
        captured=_instant(snapshot.get('captured_at'),'captured_at')
        if captured>at or at-captured>timedelta(minutes=5):reasons.append('snapshot_not_fresh')
    except (ValueError,TypeError,VersionStoreError):reasons.append('snapshot_time_unverified')
    cores=important_players(early);lineups=[];states={}
    for side in ('home','away'):
        lineup=(snapshot.get('lineups')or{}).get(side)or{}
        names=lineup.get('players')or[]
        roster=(snapshot.get('rosters')or{}).get(side)or{}
        members={key(p.get('name')or p.get('player_name'))for p in roster.get('players',[])}
        if lineup.get('basis')!='confirmed' or len(names)!=5 or len({key(n)for n in names})!=5 or not lineup.get('source_url'):
            reasons.append(side+'_lineup_not_confirmed')
        elif roster.get('status')!='verified' or not {key(n)for n in names}<=members:
            reasons.append(side+'_starter_identity_unverified')
        else:lineups.append({'side':side,**lineup})
        for r in (snapshot.get('availability')or{}).get('records',[]):
            if r.get('side')==side:states[(side,key(r.get('player_name')))]=r
        relevant=set(cores[side])|set(names)
        if not relevant:reasons.append(side+'_important_players_unknown')
        for name in relevant:
            r=states.get((side,key(name)))
            if not r or str(r.get('status','unknown')).casefold()not in CLEAR or not r.get('source_url'):
                reasons.append(side+'_availability_unresolved:'+str(name))
                continue
            try:
                observed=_instant(r.get('published_at')or r.get('observed_at'),'availability_time')
                if observed>at or at-observed>timedelta(seconds=injury_max_age):reasons.append(side+'_availability_stale:'+name)
            except (ValueError,TypeError,VersionStoreError):reasons.append(side+'_availability_time_unknown:'+name)
            if key(name)in {key(n)for n in names} and r['status'].casefold()in {'out','inactive'}:reasons.append(side+'_starter_status_conflict:'+name)
    injury=snapshot.get('injury')or{}
    if injury.get('coverage')!='complete' or not injury.get('source_url'):reasons.append('injury_coverage_incomplete')
    try:
        source_at=_instant(injury.get('source_published_at')or injury.get('published_at')or injury.get('observed_at'),'injury_time')
        if source_at>at or at-source_at>timedelta(seconds=injury_max_age):reasons.append('injury_stale')
    except (ValueError,TypeError,VersionStoreError):reasons.append('injury_time_unverified')
    market=(snapshot.get('market_collection')or{}).get('public_market_reference')or{}
    if market.get('game_identity'):
        m=market['game_identity']
        if any(str(m.get(k))!=str(identity[k])for k in ('game_id','home_team_id','away_team_id')):reasons.append('market_identity_mismatch')
        try:
            if _instant(m.get('scheduled_tipoff'),'market_tipoff')!=tip:reasons.append('market_identity_mismatch')
        except (ValueError,TypeError,VersionStoreError):reasons.append('market_identity_mismatch')
    else:reasons.append('market_identity_unverified')
    if market.get('home_spread')is None or market.get('total')is None or not market.get('source_url'):reasons.append('fresh_market_unavailable')
    try:
        stamp=_instant(market.get('captured_at'),'market_captured_at')
        if stamp>at or at-stamp>timedelta(seconds=market_max_age):reasons.append('market_stale')
    except (ValueError,TypeError,VersionStoreError):reasons.append('market_time_unverified')
    result={'schema_version':'final-readiness-v1','game_id':game['id'],'ready':not reasons,'reasons':sorted(set(reasons)),
        'checked_at':_utc(at),'final_ready_at':_utc(at)if not reasons else None,'next_check_at':next_check(game['tipoff'],_utc(at)),
        'important_players':cores,'lineups':lineups,'snapshot':snapshot,'snapshot_hash':content_hash(snapshot),'llm_calls':0}
    return result

def read_http(url,timeout=4):
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'CourtVision/1.0'}),timeout=timeout)as response:
            body=response.read(4_000_001)
    except (TimeoutError,urllib.error.URLError):
        # Isolated Windows TLS transport fallback. Same URL and qualification;
        # no proxy/system setting changes, no shell and no credential arguments.
        import os,subprocess,shutil
        from urllib.parse import urlsplit
        if os.name!='nt' or urlsplit(url).hostname not in {'site.api.espn.com','sports.core.api.espn.com','cdn.nba.com'}:
            raise
        executable=shutil.which('curl.exe')
        if not executable:raise
        r=subprocess.run([executable,'--fail','--silent','--show-error','--max-time',str(timeout),url],
            capture_output=True,timeout=timeout+1,check=True)
        body=r.stdout
    if len(body)>4_000_000:raise ValueError('readiness_response_too_large')
    return json.loads(body),{'url':url,'captured_at':now(),'status':'success','sha256':hashlib.sha256(body).hexdigest()}

def snapshot_from_espn(game,data,receipt):
    """Only match-bound starter/status fields; athlete.active (league) is ignored."""
    eid=game['id'].removeprefix('espn:');header=data.get('header')or{};competition=(header.get('competitions')or[{}])[0]
    if str(header.get('id'))!=eid or str(competition.get('id'))!=eid or _instant(competition.get('date'),'event_date')!=_instant(game['tipoff'],'tipoff'):
        raise ValueError('readiness_event_identity_mismatch')
    expected={str(game[s].get('espn_team_id')):s for s in ('home','away')}
    competitors=competition.get('competitors')or[]
    for row in competitors:
        side=expected.get(str(row.get('team',{}).get('id')))
        if not side or row.get('homeAway')!=side:raise ValueError('readiness_team_direction_mismatch')
    result={'game_identity':game_identity(game),'schedule_revision':game.get('schedule_revision',1),
        'captured_at':receipt['captured_at'],'game_state':competition.get('status',{}).get('type',{}).get('state','unknown'),
        'lineups':{},'rosters':{},'availability':{'records':[]},'injury':{'coverage':'unknown','source_url':receipt['url']},'sources':[receipt]}
    for block in data.get('boxscore',{}).get('players',[]):
        side=expected.get(str(block.get('team',{}).get('id')))
        if side is None:continue
        rows=[r for stat in block.get('statistics',[])for r in stat.get('athletes',[])]
        names=[r.get('athlete',{}).get('displayName')for r in rows]
        names=[n for n in names if n]
        result['rosters'][side]={'status':'verified','players':[{'name':n}for n in sorted(set(names))],'source_url':receipt['url'],'observed_at':receipt['captured_at']}
        starters=[r.get('athlete',{}).get('displayName')for r in rows if r.get('starter')is True]
        # Per-event boxscore starter flags are a source status, never a minutes ranking.
        if len(set(starters))==5 and not block.get('projected',False):
            result['lineups'][side]={'basis':'confirmed','players':starters,'source_url':receipt['url'],'observed_at':receipt['captured_at'],'source_kind':'espn_game_starter_flags'}
        for r in rows:
            status=r.get('gameStatus')or r.get('availability')
            if isinstance(status,dict):status=status.get('name')or status.get('type')
            if isinstance(status,str) and status.casefold()in CLEAR|UNCERTAIN:
                result['availability']['records'].append({'side':side,'player_name':r.get('athlete',{}).get('displayName'),'status':status,'source_url':receipt['url'],'observed_at':receipt['captured_at'],'source_kind':'game_availability'})
    for row in data.get('startingLineups')or[]:
        side=expected.get(str(row.get('teamId')))
        if side and row.get('confirmed')is True:
            names=[p.get('displayName')or p.get('name')for p in row.get('players',[])]
            if len(names)==5 and all(names):
                result['lineups'][side]={'basis':'confirmed','players':names,'source_url':receipt['url'],'observed_at':receipt['captured_at'],'source_kind':'espn_confirmed_lineup'}
                result['rosters'][side]={'status':'verified','players':[{'name':n}for n in names],'source_url':receipt['url'],'observed_at':receipt['captured_at']}
    return result

class FinalReadinessStore:
    def __init__(self,versions,collector=None):
        self.versions=versions;self.collector=collector
        with versions.connect()as db:
            db.execute('''CREATE TABLE IF NOT EXISTS final_readiness_checks (
                check_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,checked_at TEXT NOT NULL,
                next_check_at TEXT NOT NULL,ready INTEGER NOT NULL,baseline_version_id TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,payload_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_final_readiness_game ON final_readiness_checks(game_id,checked_at)')
    def latest(self,game_id):
        with self.versions.connect()as db:
            row=db.execute('SELECT payload_json FROM final_readiness_checks WHERE game_id=? ORDER BY checked_at DESC,rowid DESC LIMIT 1',(game_id,)).fetchone()
        return json.loads(row[0])if row else None
    def check(self,game,early,*,force=False):
        at=_utc(self.versions.clock());last=self.latest(game['id'])
        if not force and last and last['baseline_version_id']==early['version_id']and _instant(last['next_check_at'],'next_check_at')>_instant(at,'at'):
            if last['ready']:
                refreshed=evaluate_readiness(game,early,last['snapshot'],at)
                if not refreshed['ready']:return {**last,**refreshed}
            return last
        if self.collector is None:self.collector=collect_final_snapshot
        try:snapshot=self.collector(game,early,at,self.versions)
        except Exception as error:snapshot={'game_identity':game_identity(game),'schedule_revision':game.get('schedule_revision',1),'captured_at':at,'game_state':'unknown','source_error':type(error).__name__}
        at=_utc(self.versions.clock())
        result=evaluate_readiness(game,early,snapshot,at);result['baseline_version_id']=early['version_id']
        result['final_window_target_at']=_utc(_instant(game['tipoff'],'tipoff')-timedelta(minutes=20))
        result['final_window_started_at']=last.get('final_window_started_at',at)if last else at
        result['lineup_confirmed_at']=max(r.get('observed_at')or at for r in result['lineups'])if len(result['lineups'])==2 else None
        result['availability_confirmed_at']=at if not any('availability_'in r or 'injury_'in r for r in result['reasons']) else None
        with self.versions.connect()as db:db.execute('INSERT OR IGNORE INTO final_readiness_checks VALUES(?,?,?,?,?,?,?,?)',
            (content_hash({'game':game['id'],'at':at,'snapshot':result['snapshot_hash']}),game['id'],at,result['next_check_at'],int(result['ready']),early['version_id'],result['snapshot_hash'],json.dumps(result,ensure_ascii=False)))
        return result

def collect_final_snapshot(game,early,at,versions):
    """Bounded current game JSON + official injury + structured market, no search/LLM."""
    from concurrent.futures import ThreadPoolExecutor
    from .market_collector import normalize_book_market
    from .market import events
    from v2.frontend.official_injury_reports import OfficialInjuryReportStore
    eid=game['id'].removeprefix('espn:');summary_url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid
    odds_url=f'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds'
    with ThreadPoolExecutor(max_workers=2)as pool:
        sf=pool.submit(read_http,summary_url);mf=pool.submit(read_http,odds_url)
        summary,sr=sf.result()
        try:market,mr=mf.result()
        except Exception:market,mr={},{}
    result=snapshot_from_espn(game,summary,sr)
    comp=summary['header']['competitions'][0]
    event=next((e for e in events({'events':[{'id':eid,'date':comp['date'],'competitions':[comp]}]},sr)if e['game_id']==game['id']),None)
    quote=normalize_book_market(market,event,mr,now())if event and mr else None
    result['market_collection']={'status':'available'if quote else'unavailable','public_market_reference':quote,'evidence':[],'sources':[mr]if mr else[],'finished_at':now()}
    store=OfficialInjuryReportStore(versions.path,clock=versions.clock)
    try:store.refresh(game,max_seconds=5,max_reports=1)
    except Exception:pass
    report=store.read_as_of(game,now(),captured_before_cutoff=True)
    if report.get('status')in {'published','available'} and report.get('coverage')in {'complete','COMPLETE'}:
        result['injury']=report
        for row in report.get('records',[]):
            side=next((s for s in ('home','away')if int(row.get('team_id',0))==int(game[s]['id'])),None)
            if side:result['availability']['records'].append({**row,'side':side,'source_url':report.get('source_url'),'published_at':row.get('source_published_at')or report.get('source_published_at'),'observed_at':report.get('captured_at')})
    # A positive confirmed-starting assignment is availability evidence, unlike
    # absence from an injury report. Explicit newer Out/Questionable overrides it.
    for side,lineup in result['lineups'].items():
        listed={key(r['player_name'])for r in result['availability']['records']if r.get('side')==side}
        for name in lineup['players']:
            if key(name)not in listed:result['availability']['records'].append({'side':side,'player_name':name,'status':'Available','source_url':lineup['source_url'],'observed_at':lineup['observed_at'],'source_kind':'confirmed_starting_assignment'})
    result['captured_at']=now();return result
