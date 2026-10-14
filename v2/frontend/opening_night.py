"""Operator-authorized three-game intake/EARLY execution; never expands the slate."""
from __future__ import annotations
import argparse,copy,json,uuid
from pathlib import Path
from datetime import datetime,timezone
from v2.pregame_system.prediction_versions import PredictionVersionStore,DEFAULT_DATABASE,game_identity,_instant
from v2.pregame_system.generation_tasks import GenerationTaskStore
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
from v2.pregame_system.storage import save,now,content_hash
from v2.pregame_system.final_readiness import read_http
from v2.pregame_system.analyst import EvidenceBoundAnalyst
from v2.pregame_system.web_research_agent import AutonomousWebResearcher
from v2.pregame_system.web_search import PregameWebSearch
from v2.pregame_system.cost_ledger import CostLedger
from .prediction_version_service import PredictionVersionService
from .analysis_service import SavedGameAnalysisService
from .generation_input import prepare_generation_data,registered_fact_game
from .current_roster_preflight import prepare_current_roster_preflight
from .schedule_catalog import ScheduleCatalog
from .export_data import team
from .server import load_local_env

ROOT=Path(__file__).resolve().parents[2]
TARGETS={'espn:401909088':(1610612765,1610612738),'espn:401909089':(1610612752,1610612755),'espn:401909090':(1610612759,1610612760)}
NBA_SCHEDULE_SOURCE='https://pr.nba.com/2026-27-nba-regular-season-schedule/'

def intake(versions,root):
    catalog=ScheduleCatalog(versions.path);games=[];receipts=[]
    for gid,(home,away)in TARGETS.items():
        row=catalog.get_game(gid)
        if not row or row['season']!=2027 or row['game_type']!='regular_season' or (row['home_team_id'],row['away_team_id'])!=(home,away):raise ValueError('opening_catalog_identity_or_season_mismatch:'+gid)
        eid=gid.removeprefix('espn:')
        try:
            data,receipt=read_http('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid,timeout=25)
        except (TimeoutError,ConnectionError):
            data,receipt=read_http('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid,timeout=25)
        header=data.get('header')or{};event=(header.get('competitions')or[{}])[0]
        if str(header.get('id'))!=eid or str(event.get('id'))!=eid or _instant(event.get('date'),'event_date')!=_instant(row['scheduled_tipoff'],'tipoff'):raise ValueError('opening_live_schedule_mismatch:'+gid)
        expected={'home':str(row['home_espn_team_id']),'away':str(row['away_espn_team_id'])}
        actual={r['homeAway']:str(r['team']['id'])for r in event.get('competitors',[])}
        if actual!=expected or event.get('status',{}).get('type',{}).get('state')!='pre':raise ValueError('opening_not_matching_future_game:'+gid)
        g={'id':gid,'home':{**team(home,row['home_team_name']),'espn_team_id':expected['home']},'away':{**team(away,row['away_team_name']),'espn_team_id':expected['away']},
            'tipoff':row['scheduled_tipoff'],'scheduled_tipoff':row['scheduled_tipoff'],'timezone':'UTC','status':'scheduled',
            'game_type':'regular_season','season':2027,'season_start':2026,'venue':row['venue'],'neutral_site':bool(row['neutral_site']),
            'schedule_revision':row['schedule_revision'],'registered_game':True,
            'season_event':{'name':'opening night','verified':True,'source_url':NBA_SCHEDULE_SOURCE,
                            'observed_at':receipt['captured_at'] if 'captured_at' in receipt else now()},
            'source':{**receipt,'source_type':'espn_game_schedule','nba_schedule_crosscheck_url':NBA_SCHEDULE_SOURCE}}
        if _instant(g['tipoff'],'tipoff')<=versions.clock():raise ValueError('opening_already_started:'+gid)
        games.append(g);receipts.append({'game_id':gid,'verified_identity':game_identity(g),'receipt':receipt,'season_validation':row['validation']})
        save(root/(eid+'-schedule.json'),{'game':g,'source_receipt':receipt,'event':event})
    save(root/'intake.json',{'games':games,'receipts':receipts,'scope':list(TARGETS),'llm_calls':0,'nba_crosscheck':'official published tripleheader manually crosschecked; no claimed complete league dual-source audit'})
    return games

def service_for(versions,tasks):
    def analyst_factory(request):
        with versions.connect()as db:attempt=db.execute('SELECT attempt_count FROM generation_tasks WHERE request_id=?',(request['request_id'],)).fetchone()[0]
        root=ROOT/'output/opening-night-20261014/generation'/request['game_identity']['game_id'].replace(':','_')/request['request_id']/f'attempt-{attempt:02d}'
        return EvidenceBoundAnalyst(root=root/'analyst',max_calls=6,include_forecasting_skill=True)
    def factory(data,*,analyst):
        from .player_display_names import PlayerDisplayNameStore
        from .market_reference_store import MarketReferenceStore
        search=PregameWebSearch()
        return SavedGameAnalysisService(data,analyst=analyst,searcher=search,
            researcher=AutonomousWebResearcher(searcher=search,root=analyst.root/'research'),
            display_name_store=PlayerDisplayNameStore(versions),market_reference_store=MarketReferenceStore(versions),require_research_success=True)
    def prepare(data,gid,*,cutoff):
        copied=copy.deepcopy(data)
        copied['games']=[registered_fact_game(g)if g['id']==gid else g for g in copied['games']]
        # A fresh EARLY must use production assets, never old demo numbers.
        return prepare_generation_data(copied,gid,cutoff=cutoff)
    return PredictionVersionService(store=versions,task_store=tasks,service_factory=factory,analyst_factory=analyst_factory,
        input_preparer=prepare,pre_generation_preparer=prepare_current_roster_preflight)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute-early',action='store_true');parser.add_argument('--game-id',choices=list(TARGETS));args=parser.parse_args(argv)
    load_local_env();versions=PredictionVersionStore();root=ROOT/'output/opening-night-20261014/intake'/now().replace(':','').replace('.','')
    root.mkdir(parents=True,exist_ok=False);games=intake(versions,root)
    tasks=GenerationTaskStore(versions);planner=SchedulerPolicyPlanner(tasks);planned={}
    for game in games:
        registered=tasks.register_game(game)['game']
        old=registered.get('policy_config')or{}
        policy=old if old.get('lifecycle')=='early-final-v1'else {'report_enabled':True,'lifecycle':'early-final-v1','early_requested_at':now(),'prefer_off_peak':True}
        configured=planner.configure(game['id'],policy);planned[game['id']]=planner.plan_game(configured)
        game['policy_config']=configured['policy_config']
    runtime_data=ROOT/'output/opening-night-20261014/opening-games.json'
    if runtime_data.exists():
        previous=json.loads(runtime_data.read_text(encoding='utf-8'))
        if {g['id']:game_identity(g)for g in previous['games']}!={g['id']:game_identity(g)for g in games}:raise ValueError('opening_runtime_identity_changed')
        # This is a mutable launcher input, not an immutable model artifact.
        # Each intake remains separately frozen above, including earlier names.
        runtime_data.write_text(json.dumps({'games':games},ensure_ascii=False,indent=2),encoding='utf-8')
    else:save(runtime_data,{'games':games})
    save(root/'planned.json',planned);result={'intake':'passed','scope':list(TARGETS),'planned':{gid:[{'task_id':r['task_id'],'stage':r['stage'],'scheduled_for':r['scheduled_for'],'attempt_count':r['attempt_count']}for r in rows]for gid,rows in planned.items()},'executions':[]}
    if args.execute_early:
        service=service_for(versions,tasks)
        for game in games:
            if args.game_id and game['id']!=args.game_id:continue
            selected=service.selected(game)
            if (selected.get('version')or{}).get('stage')in {'EARLY_PREVIEW','FINAL_PREGAME'}:
                result['executions'].append({'game_id':game['id'],'status':'already_published','version_id':selected['version_id']});continue
            task=next(r for r in planned[game['id']]if r['stage']=='EARLY_PREVIEW')
            if _instant(task['scheduled_for'],'due')>versions.clock():
                result['executions'].append({'game_id':game['id'],'status':'scheduled_off_peak','due':task['scheduled_for']});continue
            try:
                reply=service.execute_task({'games':games},game['id'],task['task_id'])
                result['executions'].append({'game_id':game['id'],'status':reply['update_status'],'version_id':reply.get('version_id')})
            except Exception as error:
                result['executions'].append({'game_id':game['id'],'status':'failed','error_type':type(error).__name__,'error':str(error)[:1200]})
                save(root/'execution.json',result)
                raise
    result['costs']=CostLedger(versions.path).summary();save(root/'execution.json',result)
    print(json.dumps({k:v for k,v in result.items()if k!='costs'},ensure_ascii=True));return result
if __name__=='__main__':main()
