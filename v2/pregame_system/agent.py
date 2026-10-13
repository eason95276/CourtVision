"""Actual LangGraph orchestration, LangChain Tools and structured query planning.

Model output contains scope/tool choices only. All facts are rendered from
validated tool results; the model cannot supply numerical answers or injury facts.
"""
import json
import os
import copy
from pathlib import Path
from typing import Literal,TypedDict
from contextlib import contextmanager
import pandas as pd
from pydantic import BaseModel,ConfigDict,Field
from langchain_core.tools import tool
from langgraph.graph import StateGraph,START,END
from .storage import BASE,save,new_run,now,instant,digest,clean,content_hash
from . import ROOT
from .prediction import Predictor,feature_row,historical_identity,latest
from .market import normalize_odds,events,cached_get,BOOK_ORDER

Section=Literal['prediction','market','team_form','matchup','availability','provenance']
TEAM_NAMES={1610612737:'老鹰',1610612738:'凯尔特人',1610612739:'骑士',1610612740:'鹈鹕',1610612741:'公牛',
1610612742:'独行侠',1610612743:'掘金',1610612744:'勇士',1610612745:'火箭',1610612746:'快船',1610612747:'湖人',
1610612748:'热火',1610612749:'雄鹿',1610612750:'森林狼',1610612751:'篮网',1610612752:'尼克斯',1610612753:'魔术',
1610612754:'步行者',1610612755:'76人',1610612756:'太阳',1610612757:'开拓者',1610612758:'国王',1610612759:'马刺',
1610612760:'雷霆',1610612761:'猛龙',1610612762:'爵士',1610612763:'灰熊',1610612764:'奇才',1610612765:'活塞',1610612766:'黄蜂'}


class QueryPlan(BaseModel):
    model_config=ConfigDict(extra='forbid')
    intent:Literal['report','prediction','team_form','matchup','availability','market','provenance','clarify','unsupported']
    game_id:str|None=None
    date:str|None=Field(None,description='YYYY-MM-DD America/New_York date explicitly requested, never invent a date')
    team_ids:list[int]=Field(default_factory=list,max_length=2)
    player_name:str|None=None
    inherit_game:bool=False
    sections:list[Section]=Field(default_factory=list,max_length=6)
    clarification:str|None=None


class Session(BaseModel):
    game:dict|None=None
    team_id:int|None=None
    player:str|None=None
    analysis_level:str='pregame'
    previous_intent:str|None=None
    evidence_refs:list[str]=Field(default_factory=list)
    pending_clarification:dict|None=None
    prediction_snapshot:dict|None=None
    cutoff:str|None=None
    analyst_context_hash:str|None=None
    analyst_judgment:dict|None=None


class Report(BaseModel):
    model_config=ConfigDict(extra='forbid')
    game:dict|None=None
    snapshot:dict|None=None
    prediction:dict|None=None
    pure_prediction:dict|None=None
    market_benchmark:dict|None=None
    divergence:dict|None=None
    residual_prediction:dict|None=None
    official_numeric_prediction:dict|None=None
    official_market_adjustment:dict|None=None
    prediction_method:str|None=None
    residual_model:dict|None=None
    prediction_layers:dict|None=None
    residual_feature_values:dict|None=None
    analyst_context:dict|None=None
    analyst_judgment:dict|None=None
    basketball_only_prediction:dict|None=None
    market_informed_prediction:dict|None=None
    market_usage:dict|None=None
    market:dict|None=None
    team_form:dict|None=None
    matchup:dict|None=None
    availability:dict|None=None
    roster_context:dict|None=None
    web_context:dict|None=None
    public_market_reference:dict|None=None
    fact_context:dict|None=None
    analysis:dict=Field(default_factory=dict)
    evidence:list[dict]=Field(default_factory=list)
    missing_data:list[dict]=Field(default_factory=list)
    sources:list[dict]=Field(default_factory=list)
    trace:list[dict]=Field(default_factory=list)


class StructuredPlanner:
    """One structured call per turn; four persisted attempts across this stage."""
    def __init__(self,budget_dir=None,max_calls=4):
        self.directory=Path(budget_dir or BASE/'outputs/llm_calls');self.directory.mkdir(parents=True,exist_ok=True)
        self.max_calls=min(max_calls,4)

    def plan(self,question,session):
        from langchain_openai import ChatOpenAI
        os.environ['LANGSMITH_TRACING']='false'
        @contextmanager
        def budget_lock():
            path=self.directory/'budget.lock'
            with path.open('x'):
                pass
            try:yield
            finally:path.unlink()
        with budget_lock():
            attempts=list(self.directory.glob('*/attempt.json'))
            count=sum(not p.with_name('aborted_before_transport.json').exists() for p in attempts)
            if count>=self.max_calls:raise ValueError('llm_budget_exhausted')
            folder=self.directory/f'{len(attempts)+1:02d}';folder.mkdir(exist_ok=False)
            request={'question':question,'session':session.model_dump(), 'team_catalog':TEAM_NAMES}
            save(folder/'attempt.json',{'time':now(),'max_retries':0,'max_output_tokens':1200,
                 'model':'deepseek-v4-flash','request':request})
        try:
            llm=ChatOpenAI(model='deepseek-v4-flash',api_key=os.environ['DEEPSEEK_API_KEY'],
                 base_url='https://api.deepseek.com',max_retries=0,timeout=40,max_tokens=1200,temperature=0,
                 use_responses_api=False,disabled_params={'parallel_tool_calls':None},
                 extra_body={'thinking':{'type':'disabled'},'max_tokens':1200})
            structured=llm.with_structured_output(QueryPlan,method='function_calling',include_raw=True)
            save(folder/'transport_started.json',{'time':now(),'max_retries':0})
            result=structured.invoke([
                ('system','你只规划NBA赛前证据查询，不回答数字。输出 QueryPlan。根据用户语义选择所需 sections。report 要全六项。'
                 '不支持赛后复盘、系列赛、战术因果、无依据球员贡献。只有明确延续上一场才 inherit_game=true。'
                 '用户指定新球队或日期时重新解析，不能继承旧球员。主体不明就 clarify，澄清用中文。'
                 'date 是美国东部比赛日，日期缺失时保持空。不得制造比赛 ID、队伍或球员身份。'),
                ('human',json.dumps(request,ensure_ascii=False))])
            raw=result.get('raw')
            save(folder/'response.json',{'time':now(),'raw':raw.model_dump(mode='json') if raw else None,
                'parsing_error':str(result.get('parsing_error')) if result.get('parsing_error') else None,
                'plan':result['parsed'].model_dump() if result.get('parsed') else None})
            if not result.get('parsed'):raise ValueError('structured_plan_invalid')
            return result['parsed']
        except Exception as exc:
            message=str(exc).replace(os.environ.get('DEEPSEEK_API_KEY','__NO_KEY__'),'[REDACTED]')[:2500]
            save(folder/'failure.json',{'time':now(),'error_type':type(exc).__name__,'message':message,
                 'http_status':getattr(exc,'status_code',None)})
            raise


def validate_availability(data,game,cutoff):
    if not data:return None
    path=data.get('raw_path');sha=data.get('raw_sha256')
    if not path or not sha or not Path(path).is_file() or digest(path)!=sha:raise ValueError('availability_raw_integrity')
    if instant(data['source_published_at'])>instant(cutoff):raise ValueError('availability_after_cutoff')
    records=data.get('records',[])
    for row in records:
        if row.get('game_id')!=game['game_id']:raise ValueError('availability_wrong_game')
        if str(row.get('team_id')) not in [str(game['home_team_id']),str(game['away_team_id'])]:raise ValueError('availability_wrong_team')
        if instant(row['source_published_at'])>instant(cutoff):raise ValueError('availability_record_after_cutoff')
    return data


def legacy_cutoff(cutoff):
    """Existing injury resolver accepts naive ET, new public API requires timezone."""
    return pd.Timestamp(instant(cutoff)).tz_convert('America/New_York').tz_localize(None).isoformat()


class GraphState(TypedDict,total=False):
    question:str
    plan:QueryPlan
    session:Session
    cutoff:str
    replay:bool
    allow_lookup:bool
    game:dict
    trace:list
    data:dict
    missing:list
    report:dict
    clarification:str
    candidates:list
    generate_judgment:bool
    supplied_judgment:dict


class PregameAgent:
    def __init__(self,predictor=None,planner=None,schedule=None,residual_predictor=None,
                 live_market_root=None,market_fetcher=None,analyst=None,web_searcher=None):
        self.predictor=predictor or Predictor();self.planner=planner or StructuredPlanner()
        if schedule is None:
            if self.predictor.demo_context:schedule=[self.predictor.demo_context['game']]
            else:
                try:schedule=json.loads(latest('future_schedule','games.json').read_text(encoding='utf8'))
                except ValueError:schedule=[]
        self.schedule=schedule
        self.predictor.schedule=schedule
        from .residual_model import optional_residual
        residual_path=self.predictor.runtime_root/'residual/current.json' if self.predictor.runtime_root else None
        self.residual_predictor=residual_predictor or optional_residual(residual_path)
        self.live_market_root=live_market_root
        self.market_fetcher=market_fetcher
        self.analyst=analyst
        self.web_searcher=web_searcher
        from .historical_market import HistoricalMarket,PATH
        self.historical_market=HistoricalMarket() if PATH.is_file() and not self.predictor.demo_context else None
        graph=StateGraph(GraphState)
        for name in ['plan','resolve','retrieve','check','lookup','validate','select_evidence','answer']:
            graph.add_node(name,getattr(self,'_'+name))
        graph.add_edge(START,'plan');graph.add_edge('plan','resolve')
        graph.add_conditional_edges('resolve',lambda s:'answer' if s.get('clarification') else 'retrieve')
        graph.add_edge('retrieve','check')
        graph.add_conditional_edges('check',lambda s:'lookup' if self._should_lookup(s) else 'validate')
        graph.add_edge('lookup','validate');graph.add_edge('validate','select_evidence');graph.add_edge('select_evidence','answer');graph.add_edge('answer',END)
        self.graph=graph.compile()

    def ask(self,question,session=None,*,cutoff=None,plan=None,replay=False,allow_lookup=False,
            generate_judgment=False,analyst_judgment=None):
        default_cutoff=self.predictor.demo_context['cutoff'] if self.predictor.demo_context else now()
        state={'question':question,'session':session or Session(),'cutoff':cutoff or default_cutoff,
               'replay':replay,'allow_lookup':allow_lookup,'trace':[],'data':{},'missing':[],
               'generate_judgment':generate_judgment}
        if plan is not None:state['plan']=QueryPlan.model_validate(plan)
        if analyst_judgment is not None:state['supplied_judgment']=analyst_judgment
        result=self.graph.invoke(state,config={'recursion_limit':16})
        report=Report.model_validate(result['report'])
        folder=new_run('agent_runs');save(folder/'report.json',report.model_dump());save(folder/'session.json',result['session'].model_dump())
        save(folder/'plan.json',result['plan'].model_dump())
        return report.model_dump(),result['session']

    def _plan(self,s):
        plan=s.get('plan') or self.planner.plan(s['question'],s['session'])
        return {'plan':plan,'trace':s['trace']+[{'node':'plan','source':'explicit_structured_plan' if 'plan' in s else 'langchain_structured_llm','intent':plan.intent}]}

    def _resolve(self,s):
        p=s['plan'];old=s['session'];candidates=[]
        if p.intent in ['clarify','unsupported']:
            return {'clarification':p.clarification or '请明确比赛、日期及需要查询的赛前信息。','candidates':[],
                'trace':s['trace']+[{'node':'resolve','status':p.intent}]}
        if any(t not in TEAM_NAMES for t in p.team_ids):raise ValueError('unknown_team')
        if p.date:pd.Timestamp(p.date) # Reject malformed dates before searching.
        if p.inherit_game and old.game and not p.game_id and not p.date and not p.team_ids:
            candidates=[old.game]
        else:
            if s['replay']:
                pool=self.predictor.games
                if p.game_id:pool=pool[pool.game_id==p.game_id]
                if p.date:pool=pool[pool.game_date.dt.strftime('%Y-%m-%d')==p.date]
                for t in p.team_ids:pool=pool[(pool.home_team_id==t)|(pool.away_team_id==t)]
                candidates=[historical_identity(g) for g in pool.to_dict('records')]
            else:
                for g in self.schedule:
                    day=str(pd.Timestamp(g['scheduled_tipoff']).tz_convert('America/New_York').date())
                    if p.game_id and p.game_id!=g['game_id']:continue
                    if p.date and p.date!=day:continue
                    if any(t not in [g['home_team_id'],g['away_team_id']] for t in p.team_ids):continue
                    candidates.append(g)
        if len(candidates)!=1:
            return {'clarification':'请明确比赛日期和对阵；当前找到 '+str(len(candidates))+' 场候选比赛。',
                    'candidates':candidates[:8],'trace':s['trace']+[{'node':'resolve','candidate_count':len(candidates)}]}
        game=candidates[0]
        if instant(s['cutoff'])>=instant(game['scheduled_tipoff']):raise ValueError('cutoff_not_pregame')
        # New scope is reconstructed, never carries a stale snapshot or player.
        session=Session(game=game,team_id=p.team_ids[0] if len(p.team_ids)==1 else None,
                        player=p.player_name,cutoff=s['cutoff'],previous_intent=p.intent)
        return {'game':game,'session':session,'trace':s['trace']+[{'node':'resolve','game_id':game['game_id']}]}

    def _retrieve(self,s):
        p=s['plan'];sections=list(dict.fromkeys(p.sections or ([p.intent] if p.intent!='report' else ['prediction','market','team_form','matchup','availability','provenance'])))
        if p.intent=='report':sections=['prediction','market','team_form','matchup','availability','provenance']
        if p.intent=='report':sections.append('roster_context')
        data={};missing=[];trace=list(s['trace']);computed={}
        def features():
            if 'row' not in computed:
                row,groups,audit,_=self.predictor.prepare_features(s['game'],s['cutoff'],s['replay'])
                computed.update(row=clean(row.iloc[0].to_dict()),audit=audit)
            return computed
        def factual_history(audit):
            result=copy.deepcopy(audit)
            for side in ('home','away'):
                if isinstance(result.get(side),dict):
                    result[side].pop('scheduled_load_game_ids',None)
                    result[side].pop('schedule_assumption',None)
            return result
        def retrieve(name):
            if name=='roster_context':
                from .roster_context import local_roster_context
                return local_roster_context(s['game'],s['cutoff'],getattr(self.predictor,'games',None),
                                            stats=getattr(self.predictor,'stats',None))
            if name=='prediction':return self.predictor.predict(s['game'],s['cutoff'],replay=s['replay'])
            if name=='team_form':
                f=features();return {'perspective':'explicit home and away','values':{k:v for k,v in f['row'].items() if (k.startswith('home_') or k.startswith('away_')) and 'roll' in k},'history':factual_history(f['audit']),'rate_semantics':'V1 rolling per-game means; not pooled series percentages'}
            if name=='matchup':
                f=features();return {'values':{k:v for k,v in f['row'].items() if k.startswith('matchup_')},
                    'history':factual_history(f['audit']),'rate_semantics':'pooled counts in trailing 15 games; interaction is statistical, not causal',
                    'model_usage':'consult snapshot model variant; candidate features may be rejected by ablation'}
            if name=='availability':
                if self.predictor.demo_context:return None
                from v2.agent.historical_injury_lookup import local_report
                from .player_context import local_context
                if not s['replay']:return None
                report=local_report(s['game']['game_id'],legacy_cutoff(s['cutoff']))
                if report:report['key_player_context']=local_context(s['game'],s['cutoff'])
                return report
            if name=='market':return self.market(s['game'],s['cutoff'],s['replay'])
            if name=='provenance':return {'boxscores':'frozen precomputed demo features' if self.predictor.demo_context else 'retrospectively fetched Games.csv and TeamStatistics.csv',
                  'demo_runtime':bool(self.predictor.demo_context),
                  'cutoff':s['cutoff'],'target_excluded':True,'original_historical_capture_proven':False}
        for name in sections:
            # Real LangChain tool invocation, scope bound by trusted closure.
            def make_tool(n):
                @tool(n)
                def read_evidence()->dict:
                    """Read cutoff-bound deterministic evidence for the resolved game."""
                    return {'value':retrieve(n)}
                return read_evidence
            try:value=make_tool(name).invoke({})['value']
            except (ValueError,FileNotFoundError,KeyError) as exc:
                value=None;trace.append({'tool':name,'error_type':type(exc).__name__,'reason':str(exc)[:150]})
            data[name]=value;trace.append({'node':'retrieve','langchain_tool':name,'status':'available' if value is not None else 'missing'})
            if value is None:missing.append({'section':name,'reason':'no eligible local evidence'})
        if 'team_form' in data and data.get('team_form') is not None:
            try:
                from .fact_context import build_fact_context
                feature_data=features()
                data['fact_context']=build_fact_context(
                    s['game'],s['cutoff'],getattr(self.predictor,'games',None),
                    getattr(self.predictor,'stats',None),feature_data['row'],self.schedule,
                )
            except (KeyError,TypeError,ValueError) as exc:
                # Preserve the model row but fail closed for calendar/sample facts.
                data['fact_context']={
                    'schema_version':'pregame-fact-context-v1','game_id':s['game'].get('game_id'),
                    'feature_cutoff':s['cutoff'],'team_form':{},
                    'schedule':{side:{'status':'unavailable','reason':'fact_context_build_failed:'+str(exc)[:120],
                                      'basis':None,'back_to_back':None,'source_refs':[]}
                                for side in ('home','away')},
                }
                missing.append({'section':'fact_context','reason':'metadata_unavailable; original feature values retained'})
        return {'data':data,'missing':missing,'trace':trace}

    def market(self,game,cutoff,replay=False):
        if self.predictor.demo_context:
            from .market import validate_prediction_market_quote
            quote=self.predictor.demo_context.get('market_fixture')
            if quote:
                return {'as_of_cutoff':validate_prediction_market_quote(quote,game,cutoff),
                    'historical_reference':None,'demo_fixture':True,
                    'source':{'role':'synthetic demonstration quote; not a real captured market'}}
        if replay:
            value=self.historical_market.get(game) if self.historical_market else None
            if value:return value
        else:
            from .market import latest_legal_market
            value=latest_legal_market(game,cutoff,root=self.live_market_root)
            if value:return value
        return {'as_of_cutoff':None,'historical_reference':None,
            'reason':'no matching local historical market' if replay else 'no legal future market data available',
            'model_market_disagreement':None}

    def refresh_market(self,game,cutoff):
        from .market import refresh_live_market
        kwargs={'root':self.live_market_root}
        if self.market_fetcher is not None:kwargs['fetcher']=self.market_fetcher
        return refresh_live_market(game,cutoff,**kwargs)

    def _check(self,s):
        missing=list(s['missing'])
        for side,context in (s['data'].get('roster_context') or {}).get('teams',{}).items():
            if not context.get('roster'):
                missing.append({'section':'roster','reason':side+': no eligible captured roster'})
            if context.get('continuity',{}).get('status')!='available':
                missing.append({'section':'roster_continuity','reason':side+': roster/minutes denominator unavailable'})
        if s['data'].get('market') is not None and not (s['data']['market'].get('as_of_cutoff') or s['data']['market'].get('historical_reference')):
            missing.append({'section':'market','reason':s['data']['market']['reason']})
        if not (s['data'].get('availability') or {}).get('key_player_context'):
            missing.append({'section':'key_players','reason':'longitudinal minutes/availability and roster roles not sufficiently verified; no player impact conclusion'})
        return {'missing':missing,'trace':s['trace']+[{'node':'check','missing_sections':[m['section'] for m in missing]}]}

    def _lookup_categories(self,s):
        categories=set()
        if not (s['data'].get('availability') or {}).get('records'):
            categories.add('availability')
        teams=(s['data'].get('roster_context') or {}).get('teams',{}).values()
        teams=list(teams)
        if any(not (team.get('confirmed_starters') or team.get('projected_starters')
                        or (team.get('current_rotation') or {}).get('projected_starters')) for team in teams):
            categories.add('lineup')
        if any((team.get('cold_start') or {}).get('cold_start') for team in teams):
            categories.add('roster_news')
        market=s['data'].get('market') or {}
        if not (market.get('as_of_cutoff') or market.get('historical_reference')):
            categories.add('market')
        return categories

    def _should_lookup(self,s):
        return bool(s.get('allow_lookup') and self._lookup_categories(s))

    def _lookup(self,s):
        from v2.agent.historical_injury_lookup import InjuryResolver
        trace=list(s['trace']);data=dict(s['data'])
        if s['replay']:
            try:
                value=InjuryResolver(BASE/'outputs/injury_cache').resolve(
                    s['game']['game_id'],
                    legacy_cutoff(s['cutoff']),
                    replay=True,
                )
                trace.append({'node':'lookup','status':value['status'],'requests':value['request_count'],'audit':value.get('audit_path')})
                if value['status']=='success':data['availability']=value
            except Exception as exc:trace.append({'node':'lookup','status':'unavailable','error_type':type(exc).__name__})
        categories=self._lookup_categories({**s,'data':data})
        if categories:
            try:
                from .web_search import PregameWebSearch
                searcher=self.web_searcher or PregameWebSearch()
                web=searcher.search(s['game'],s['cutoff'],replay=s['replay'],categories=categories)
                trace.append({'node':'lookup','tool':'pregame_web_search','status':web['status'],
                    'categories':sorted(categories),'records':len(web.get('records') or []),
                    'cutoff_enforced':True})
                if web.get('records'):data['web_context']=web
            except Exception as exc:
                trace.append({'node':'lookup','tool':'pregame_web_search','status':'unavailable',
                    'categories':sorted(categories),'error_type':type(exc).__name__,'cutoff_enforced':True})
        return {'data':data,'trace':trace}

    def _validate(self,s):
        data=dict(s['data']);missing=list(s['missing']);trace=list(s['trace'])
        if data.get('availability'):
            try:
                data['availability']=validate_availability(data['availability'],s['game'],s['cutoff'])
                missing=[m for m in missing if m['section']!='availability']
            except (ValueError,KeyError) as exc:
                data['availability']=None;missing.append({'section':'availability','reason':str(exc)})
                if not any(m['section']=='key_players' for m in missing):missing.append({'section':'key_players','reason':'availability scope validation rejected the attached player context'})
        trace.append({'node':'validate','target_excluded':True,'cutoff':s['cutoff']})
        return {'data':data,'missing':missing,'trace':trace}

    def _select_evidence(self,s):
        evidence=[];sources=[];data=dict(s['data']);missing=list(s['missing']);trace=list(s['trace'])
        from .web_search import select_public_market_reference
        public_market=select_public_market_reference(data.get('web_context'))
        if public_market:
            missing=[
                ({**item,'reason':'结构化市场数据缺失；联网公开盘口仅供展示，未进入正式预测'}
                 if item.get('section')=='market' else item)
                for item in missing
            ]
        market=data.get('market');prediction=data.get('prediction')
        layers=None
        if prediction:
            quote=market.get('as_of_cutoff') if market else None
            layers=self.residual_predictor.predict(prediction['prediction'],quote,
                prediction.get('feature_values'),game=s['game'],cutoff=s['cutoff'])
            if layers['market_usage'].get('reason')=='residual_unavailable':
                missing.append({'section':'residual_model','reason':layers['residual_model']['reason']})
            prediction=copy.deepcopy(prediction)
            prediction['pure_prediction']=layers['pure_prediction']
            prediction['basketball_only_prediction']=layers['pure_prediction']
            prediction['market_benchmark']=layers['market_benchmark']
            prediction['divergence']=layers['divergence']
            prediction['residual_prediction']=layers['residual_prediction']
            prediction['official_numeric_prediction']=layers['official_numeric_prediction']
            prediction['official_market_adjustment']=layers['official_market_adjustment']
            prediction['prediction_method']=layers['prediction_method']
            prediction['residual_model']=layers['residual_model']
            prediction['prediction_layers']=layers
            prediction['market_informed_prediction']=(
                layers['official_numeric_prediction'] if layers['residual_prediction'] else None)
            prediction['market_usage']=layers['market_usage']
            prediction['prediction']=layers['official_numeric_prediction']
            data['prediction']=prediction
            folder=Path(prediction['path'])
            layers_path=folder/'prediction_layers.json'
            save(layers_path,layers)
            save(folder/'prediction_layers_integrity.json',{
                'prediction_layers_sha256':digest(layers_path),
                'quote_hash':layers['market_usage'].get('quote_hash'),
                'residual_model_version':layers['market_usage'].get('residual_model_version')})
            prediction['prediction_layers_path']=str(layers_path)
        if market and market.get('historical_reference') and prediction:
            q=market['historical_reference'];p=prediction['prediction']
            market['model_market_disagreement']={
                'expected_home_margin_difference':p['expected_margin']-q['expected_home_margin'] if q['expected_home_margin'] is not None else None,
                'expected_total_difference':p['expected_total']-q['total'] if q['total'] is not None else None,
                'home_win_probability_difference':p['win_probability']-q['home_probability_no_vig'] if q['home_probability_no_vig'] is not None else None,
                'meaning':'model minus local historical reference; not a time-matched tradable edge'}
        for name,value in data.items():
            if value is None:continue
            if name=='web_context':
                for record in value.get('records') or []:
                    if not record.get('eligible_for_fact'):continue
                    evidence.append({'id':record['id'],'kind':'Fact','section':'web_context',
                        'game_id':s['game']['game_id'],'cutoff':s['cutoff'],'value':clean(record)})
                    sources.append({k:record.get(k) for k in
                        ['id','title','url','published_at','captured_at','category']})
                continue
            # Evidence content is numerical tool output, never a model-generated fact.
            if name=='prediction':value={k:value[k] for k in ['prediction','pure_prediction',
                'market_benchmark','divergence','residual_prediction','official_numeric_prediction',
                'official_market_adjustment','prediction_method','residual_model','prediction_layers','market_usage',
                'snapshot','path','prediction_layers_path']}
            eid=content_hash({'game_id':s['game']['game_id'],'cutoff':s['cutoff'],'section':name,'value':value})
            evidence.append({'id':eid,'kind':'Fact' if name!='prediction' else 'ModelForecast',
                'section':name,'game_id':s['game']['game_id'],'cutoff':s['cutoff'],'value':value})
            if name=='availability':sources.append({k:value.get(k) for k in ['source_url','source_published_at','captured_at','raw_path','raw_sha256']})
            if name=='market' and value.get('source'):sources.append(value['source'])
        for path in self.predictor.source_paths:
            sources.append({'path':str(path),'sha256':digest(path),'role':'frozen pregame demo context' if self.predictor.demo_context else 'retrospectively acquired local boxscores',
                'published_at_per_row':None,'original_capture_before_cutoff_proven':False})
        if s['game'].get('source'):sources.append(s['game']['source'])
        if data.get('prediction'):
            path=Path(data['prediction']['path'])/'model_reference.json'
            sources.append({'path':str(path),'sha256':digest(path),'role':'exact model reference used by this snapshot'})
        session=s['session'].model_copy(deep=True);session.evidence_refs=[e['id'] for e in evidence]
        if data.get('prediction'):
            session.prediction_snapshot=copy.deepcopy(data['prediction']['snapshot'])
            session.prediction_snapshot['market_usage']=data['prediction']['market_usage']
        analyst_context=None;analyst_judgment=None
        if layers:
            from .analyst import EvidenceBoundAnalyst,build_analyst_context,validate_judgment
            analyst_context=build_analyst_context(
                layers,
                evidence,
                missing_data=missing,
                analysis_request=s.get('question'),
                game=s['game'],
            )
            session.analyst_context_hash=analyst_context.context_hash
            try:
                if s.get('supplied_judgment') is not None:
                    analyst_judgment=validate_judgment(s['supplied_judgment'],analyst_context)
                    trace.append({'node':'analyst_judgment','status':'success','source':'supplied_structured_judgment',
                        'context_hash':analyst_context.context_hash})
                elif s.get('generate_judgment'):
                    analyst=self.analyst or EvidenceBoundAnalyst()
                    analyst_judgment,analyst_trace=analyst.judge(analyst_context)
                    trace.append({'node':'analyst_judgment','source':'langchain_structured_analyst',
                        'context_hash':analyst_context.context_hash,**analyst_trace})
            except Exception as exc:
                missing.append({'section':'analyst_judgment','reason':type(exc).__name__})
                trace.append({'node':'analyst_judgment','status':'failed','error_type':type(exc).__name__,
                    'context_hash':analyst_context.context_hash})
            if analyst_judgment is not None:
                session.analyst_judgment=analyst_judgment.model_dump()
        report=Report(game=s['game'],snapshot=session.prediction_snapshot,
           prediction=data.get('prediction',{}).get('prediction') if data.get('prediction') else None,
           pure_prediction=data.get('prediction',{}).get('pure_prediction') if data.get('prediction') else None,
           market_benchmark=data.get('prediction',{}).get('market_benchmark') if data.get('prediction') else None,
           divergence=data.get('prediction',{}).get('divergence') if data.get('prediction') else None,
           residual_prediction=data.get('prediction',{}).get('residual_prediction') if data.get('prediction') else None,
           official_numeric_prediction=data.get('prediction',{}).get('official_numeric_prediction') if data.get('prediction') else None,
           official_market_adjustment=data.get('prediction',{}).get('official_market_adjustment') if data.get('prediction') else None,
           prediction_method=data.get('prediction',{}).get('prediction_method') if data.get('prediction') else None,
           residual_model=data.get('prediction',{}).get('residual_model') if data.get('prediction') else None,
           prediction_layers=data.get('prediction',{}).get('prediction_layers') if data.get('prediction') else None,
           residual_feature_values=data.get('prediction',{}).get('feature_values') if data.get('prediction') else None,
           analyst_context=analyst_context.model_dump() if analyst_context else None,
           analyst_judgment=analyst_judgment.model_dump() if analyst_judgment else None,
           basketball_only_prediction=data.get('prediction',{}).get('basketball_only_prediction') if data.get('prediction') else None,
           market_informed_prediction=data.get('prediction',{}).get('market_informed_prediction') if data.get('prediction') else None,
           market_usage=data.get('prediction',{}).get('market_usage') if data.get('prediction') else None,
           public_market_reference=public_market,
           fact_context=data.get('fact_context'),
            **{k:data.get(k) for k in ['market','team_form','matchup','availability','roster_context','web_context']},
           evidence=evidence,sources=sources,missing_data=missing,
           analysis={'Fact':'数值、身份、来源和时间由受控工具生成，预测值另标 ModelForecast。',
             'Inference':['统计匹配只描述历史关联，不能证明本场战术或因果。','阵容缺口与赛季初漂移使预测具有额外不确定性。'],
             'player_conclusion':None,'unrequested_sections':[k for k in ['prediction','market','team_form','matchup','availability'] if k not in s['data']]},
           trace=trace+[{'node':'select_evidence','evidence_count':len(evidence),
             'analyst_context':bool(analyst_context),'analyst_judgment':bool(analyst_judgment)}])
        return {'report':report.model_dump(),'session':session}

    def _answer(self,s):
        if s.get('clarification'):
            # Keep only an explicit pending candidate set, isolate all old identity.
            session=Session(pending_clarification={'question':s['question'],'candidates':s.get('candidates',[])})
            report=Report(analysis={'status':'clarification','message':s['clarification'],'candidates':s.get('candidates',[])},trace=s['trace'])
            return {'report':report.model_dump(),'session':session}
        report=s['report'];report['trace'].append({'node':'answer','renderer':'deterministic_schema','llm_numeric_generation':False})
        return {'report':report}
