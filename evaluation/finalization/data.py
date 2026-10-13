"""Practical archive reconstruction; target outcomes are not materialized."""
from pathlib import Path
import concurrent.futures,copy,json,re,sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .common import *
from evaluation.historical_t5.metadata import golden_metadata,META_FIELDS,SOURCE_GAMES,et_tipoff,ET,AUDIT
from evaluation.historical_t5.safe_csv import projected_rows
from evaluation.historical_t5.ledger import PriorLedger
from evaluation.historical_t5 import injury as official,research

RUNTIME=Path(str(Path.home() / '.agent-reach/audits/courtvision-finalization-20261013'))
CONTEXTS=HERE/'contexts'/DATA
TRICODE={'Atlanta Hawks':'ATL','Boston Celtics':'BOS','Brooklyn Nets':'BKN','Charlotte Hornets':'CHA','Chicago Bulls':'CHI','Cleveland Cavaliers':'CLE','Dallas Mavericks':'DAL','Denver Nuggets':'DEN','Detroit Pistons':'DET','Golden State Warriors':'GSW','Houston Rockets':'HOU','Indiana Pacers':'IND','LA Clippers':'LAC','Los Angeles Clippers':'LAC','Los Angeles Lakers':'LAL','Memphis Grizzlies':'MEM','Miami Heat':'MIA','Milwaukee Bucks':'MIL','Minnesota Timberwolves':'MIN','New Orleans Pelicans':'NOP','New York Knicks':'NYK','Oklahoma City Thunder':'OKC','Orlando Magic':'ORL','Philadelphia 76ers':'PHI','Phoenix Suns':'PHX','Portland Trail Blazers':'POR','Sacramento Kings':'SAC','San Antonio Spurs':'SAS','Toronto Raptors':'TOR','Utah Jazz':'UTA','Washington Wizards':'WAS'}

def samples():
    rows=golden_metadata();ids={r['target_game_id'].split(':')[-1] for r in rows}
    meta={r['gameId'].zfill(10):r for r in projected_rows(SOURCE_GAMES,META_FIELDS,allowed_game_ids=ids)}
    for r in rows:
        m=meta[r['target_game_id'].split(':')[-1]]
        if (int(m['hometeamId']),int(m['awayteamId']))!=(int(r['home_team_id']),int(r['away_team_id'])):raise ValueError('schedule_identity_conflict')
        r['scheduled_tipoff']=et_tipoff(m['gameDateTimeEst']);r['cutoff_at']=(datetime.fromisoformat(r['scheduled_tipoff'])-timedelta(minutes=20)).isoformat()
        r['schedule_raw']=m
    return sorted(rows,key=lambda r:(r['scheduled_tipoff'],r['target_game_id']))

def market_rows(items):
    path=ROOT/'artifacts/test_predictions_with_market.csv'
    fields={'game_id','home_team_id','away_team_id','market_expected_home_margin','market_total','moneyline_home','moneyline_away','id_spread','id_total'}
    lookup={r['target_game_id'].split(':')[-1]:r for r in items};out={}
    for r in projected_rows(path,fields,allowed_game_ids=set(lookup)):
        sid=r['game_id'].replace('nba:','').zfill(10);s=lookup[sid]
        if (int(r['home_team_id']),int(r['away_team_id']))!=(int(s['home_team_id']),int(s['away_team_id'])):raise ValueError('market_identity_conflict')
        def val(k):
            try:return float(r[k]) if r.get(k) else None
            except ValueError:return None
        q={'game_id':s['target_game_id'],'home_spread':-val('market_expected_home_margin') if val('market_expected_home_margin') is not None else None,'total':val('market_total'),
           'moneyline_home':val('moneyline_home'),'moneyline_away':val('moneyline_away'),'source':str(path.relative_to(ROOT)),
           'source_sha256':digest(path.read_bytes()),'reference_type':'historical_pregame_market_reference','open_close':'unknown',
           'quote_timestamp':None,'identity_verified':True,'strict_point_in_time_proof':False}
        if sid in out and out[sid]!=q:raise ValueError('historical_market_duplicate_conflict')
        out[sid]=q
    return out

def injury_for(s):
    cut=datetime.fromisoformat(s['cutoff_at']);local=cut.astimezone(ET);folder=RUNTIME/'injury'/s['sample_id'];folder.mkdir(parents=True,exist_ok=True)
    saved=folder/'result.json'
    if saved.exists():return read(saved)
    results=[];attempts=[]
    # Three legacy hourly reports at most. Header, not filename, determines time.
    for back in (0,1,2):
        d=local-timedelta(hours=back);url='https://ak-static.cms.nba.com/referee/injury/'+d.strftime('Injury-Report_%Y-%m-%d_%I%p.pdf')
        name=url.rsplit('/',1)[-1];cache=folder/name
        if cache.exists():
            try:t,rows=official.parse(cache,url);receipt={'captured_at':now()}
            except Exception as e:attempts.append({'url':url,'error':str(e)});continue
        else:
            cache,receipt=official.obtain(url,folder)
            attempts.append({'url':url,**receipt})
            if not cache:continue
            t,rows=official.parse(cache,url)
        if t.astimezone(timezone.utc)>cut:continue
        matchup=TRICODE[s['away_team']]+'@'+TRICODE[s['home_team']]
        day=local.strftime('%m/%d/%Y');clock=datetime.fromisoformat(s['scheduled_tipoff']).astimezone(ET).strftime('%I:%M')+'(ET)'
        matches=[r for r in rows if r['matchup']==matchup and r['game_date']==day and r['game_time']==clock and r['team'] in {s['home_team'],s['away_team']}]
        if matches:
            results.append({'status':'COMPLETE' if len({r['team'] for r in matches})==2 else 'PARTIAL','records':matches,'report_time':t.astimezone(timezone.utc).isoformat(),
              'source_url':url,'source_path':str(cache),'document_hash':digest(cache.read_bytes()),'captured_at':receipt.get('captured_at'),'grade':'A','latest_edition_exhaustively_proven':False})
            break
    result=max(results,key=lambda r:r['report_time']) if results else {'status':'UNAVAILABLE','records':[],'reason':'bounded_official_archive_scan_missing'}
    write(saved,result);write(folder/'attempts.json',attempts);return result

def research_for(s):
    pilot=ROOT/'evaluation/historical_t5/pilot-v1'/s['sample_id']/'historical_t5_context.json'
    if pilot.exists():
        old=read(pilot);e=old['research']['value']['evidence']
        return {'evidence':[r for r in e if r.get('published_at') and r['published_at']<=s['cutoff_at']],'leads':[], 'mode':'reuse_existing_pre_cutoff_original_sources'}
    folder=RUNTIME/'research'/s['sample_id'];saved=folder/'result.json'
    if saved.exists():return read(saved)
    # Existing discovery routing; bounded to two queries for this final dataset.
    original=research.query_rows
    research.query_rows=lambda sample:original(sample)[:2]
    rows,errors=research.discovery(s,folder/'discovery')
    evidence,rejected,receipt=research.qualify_sources(s,rows,folder/'fetch',budget=3)
    result={'evidence':evidence,'leads':rejected,'errors':errors,'fetch':receipt,'mode':'bounded_exa_original_source'}
    write(saved,result);return result

def make_context(s,ledger,inj,r,market):
    roster=research.roster_from_history(s,ledger,inj)
    # Cached pilot evidence keeps acquisition_reference rather than parsed_path.
    ev=copy.deepcopy(r['evidence'])
    for e in ev:
        if 'parsed_path' not in e and e.get('acquisition_reference'):e['parsed_path']=e['acquisition_reference']
    lineup=research.extract_lineups(s,ev,roster,inj)
    ctx={'matchup':{k:s[k] for k in ('target_game_id','game_date','season','home_team','away_team','game_type','scheduled_tipoff')},'cutoff':s['cutoff_at'],
         'head_to_head':ledger['head_to_head'],'schedule_context':{side:ledger[side]['rest'] for side in ('home','away')},'injury':inj,'roster':roster,
         'starting_lineup':lineup,'market':{'status':'COMPLETE' if market and market['home_spread'] is not None and market['total'] is not None else 'PARTIAL' if market else 'UNAVAILABLE','reference':market},
         'research':{'status':'PARTIAL' if ev else 'UNAVAILABLE','evidence':[]},'pure':{'status':'UNAVAILABLE','reason':'no frozen independent production baseline for this historical replay'},'known_missing_information':[]}
    for side,key in (('home','team_a'),('away','team_b')):
        h=ledger[side];players=h['player_history']
        ctx[key]={'team_id':int(s[side+'_team_id']),'team_name':s[side+'_team'],'record':h['record'],'recent_form':h['recent'],'previous_season':h['previous'],
             'player_history':players,'recent_rotation_candidates':{'players':players['players'],'scope':'prior observed candidates, not confirmed starters'}}
    for index,e in enumerate(ev):
        ctx['research']['evidence'].append({'id':'research.'+str(index),'source_url':e['source_url'],'title':e['title'],'published_at':e['published_at'],'updated_at':e.get('updated_at'),
          'grade':'A' if e.get('source_tier')=='TIER_A' else 'B','source_excerpt':e['source_excerpt'],'document_hash':e['document_hash'],'acquisition_reference':e.get('parsed_path'),
          'qualification':'credible original pregame article; retrospective reconstruction, not as-was captured snapshot'})
    for group in ('roster','injury','market','research'):
        if ctx[group].get('status','PARTIAL')!='COMPLETE':ctx['known_missing_information'].append({'field':group,'status':ctx[group].get('status','PARTIAL')})
    ctx['known_missing_information'].append({'field':'lineup','unknown_teams':[t['team'] for t in lineup['teams'] if t['basis']=='unknown']})
    check_visible(ctx)
    for event in ledger['event_records']:
        if event['game_id']==s['target_game_id'] or event['event_date']>=s['game_date']:raise ValueError('target_or_future_history')
    return ctx

def build():
    if (CONTEXTS/'manifest.json').exists():return read(CONTEXTS/'manifest.json')
    CONTEXTS.mkdir(parents=True,exist_ok=True);RUNTIME.mkdir(parents=True,exist_ok=True)
    if not (HERE/'protection-before.json').exists():write(HERE/'protection-before.json',protected())
    items=samples();markets=market_rows(items);summaries=[]
    write(HERE/'market-coverage.json',{'n':20,'spread':sum(q['home_spread'] is not None for q in markets.values()),'total':sum(q['total'] is not None for q in markets.values()),'both':sum(q['home_spread'] is not None and q['total'] is not None for q in markets.values()),'missing':[s['sample_id'] for s in items if s['target_game_id'].split(':')[-1] not in markets],'policy':'historical reference with unknown open/close; not timestamped production quote'})
    # Historical ledgers are per target: legal earlier Golden games may support
    # later contexts, but each target is excluded BEFORE numeric parsing.
    for s in items:
        folder=CONTEXTS/s['sample_id'];saved=folder/'context.json'
        if saved.exists():
            summaries.append(read(folder/'receipt.json'));continue
        ledger=PriorLedger([s]).for_sample(s);inj=injury_for(s);r=research_for(s)
        ctx=make_context(s,ledger,inj,r,markets.get(s['target_game_id'].split(':')[-1]));write(saved,ctx)
        receipt={'sample_id':s['sample_id'],'game_id':s['target_game_id'],'game_date':s['game_date'],'context_hash':digest(ctx),'context_path':str(saved.relative_to(ROOT)),
             'parent_context_hash':s['context_sha256'],'cutoff':s['cutoff_at'],'injury_status':inj['status'],'injury_rows':len(inj['records']),
             'lineup_basis':[t['basis'] for t in ctx['starting_lineup']['teams']],'research_sources':len(ctx['research']['evidence']),'market_status':ctx['market']['status'],
             'history_event_count':len(ledger['event_records']),'target_numeric_access':0,'source_hashes':{'games':digest(SOURCE_GAMES.read_bytes())}}
        write(folder/'receipt.json',receipt);summaries.append(receipt);print(json.dumps({'context_frozen':s['sample_id'],'injury':inj['status'],'research':receipt['research_sources'],'market':receipt['market_status']}),flush=True)
    body={'dataset_revision':DATA,'golden_set_revision':GOLD,'parent_dataset_revision':'controlled-cb-v1-6d788de247e93eaaedf4','selection_hash':digest((ROOT/'evaluation/golden_set/v1/selection.csv').read_bytes()),
          'n':len(summaries),'samples':summaries,'policy':'Historical Final Pregame Replay; nominal T-20; retrospective archive reconstruction, not strict point-in-time proof','created_at':now()}
    body['manifest_hash']=digest(body);write(CONTEXTS/'manifest.json',body);return body

if __name__=='__main__':build()
