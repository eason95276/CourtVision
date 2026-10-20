"""Incremental FINAL from an immutable EARLY judgment, never a full rerun."""
from __future__ import annotations
import copy,json
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field
from .storage import content_hash,now,save,clean
from .forecasting_workflow import ForecastNumbers,AnalysisCore,ForecastFactor,prepare_analyst
from .pregame_estimates import PregamePlan,validate_pregame_plan
from .analyst import AnalystContext
from .ats import build_spread_comparison
from .cost_ledger import component
from .prediction_versions import game_identity,VersionStoreError,_instant

class DeltaAssessment(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prediction:ForecastNumbers
    rationale:str=Field(min_length=1,max_length=900,description='只用150至250个汉字说明这次是否调整，不重复analysis_core，不写审计长文。')
    evidence_ids:list[str]=Field(min_length=1,max_length=8)
    analysis_core:AnalysisCore|None=None

def numbers(early):
    p=early['judgment']['final_prediction']
    return ForecastNumbers(winner_side=p['winner_side'],win_probability=p['win_probability'],
        expected_home_margin=p['expected_margin']*(1 if p['winner_side']=='home' else-1),expected_total=p['expected_total']).model_dump()

def early_plan(early):
    return copy.deepcopy(next((r['pregame_plan']for r in early.get('editorial_material',[])if r.get('pregame_plan')),{}))

def market_delta(previous,current,basketball_changed=False,baseline=None):
    previous=previous or{};current=current or{};changes={}
    for k in ('home_spread','total','home_moneyline','away_moneyline'):
        if previous.get(k)!=current.get(k):changes[k]={'before':previous.get(k),'after':current.get(k)}
    a,b=previous.get('home_spread'),current.get('home_spread')
    reversal=isinstance(a,(int,float))and isinstance(b,(int,float))and a*b<0
    consensus=current.get('qualified_book_movements')or[]
    qualified=[r for r in consensus if r.get('source_verified')and r.get('temporal_verified')and r.get('sportsbook')]
    directions={r.get('direction')for r in qualified}
    multi=len({r['sportsbook']for r in qualified})>=2 and len(directions)==1 and None not in directions
    baseline=baseline or{}
    crossed=False
    for field,predicted in (('home_spread',-baseline['expected_home_margin']if 'expected_home_margin'in baseline else None),('total',baseline.get('expected_total'))):
        before,after=previous.get(field),current.get(field)
        if all(isinstance(x,(int,float))for x in (before,after,predicted))and(before-predicted)*(after-predicted)<0:crossed=True
    first=previous.get('home_spread')is None and current.get('home_spread')is not None
    reason='direction_changed'if reversal else'qualified_book_consensus'if multi else'reference_direction_crossed'if crossed else'basketball_event_linked'if changes and basketball_changed else'ordinary_price_change'if changes else'unchanged'
    return {'changes':changes,'significant_move':reversal or multi or crossed or bool(changes and basketball_changed),'review_required':first,
        'reason':reason,'numeric_threshold_used':False,'historical_volatility_established':False}

def construct_delta(early,snapshot):
    old_plan=early_plan(early);old_lineups={r['side']:r.get('players',[])for r in old_plan.get('lineups',[])}
    lineups=[]
    for side,row in snapshot.get('lineups',{}).items():
        before=old_lineups.get(side,[]);after=row.get('players',[])
        lineups.append({'side':side,'before':before,'after':after,'basis':row.get('basis'),
            'material':not before or set(before)!=set(after),'new_confirmation':row.get('basis')=='confirmed','source_url':row.get('source_url')})
    old_states={}
    for e in early.get('analyst_context',{}).get('objective_evidence',[]):
        v=e.get('value')or{}
        rows=v.get('records',[])if e.get('section')=='availability'else[v]if v.get('claim_type')=='availability'else[]
        for r in rows:
            if r.get('player_name'):old_states[str(r.get('player_name')).casefold()]=str(r.get('status')or'unknown').casefold()
    availability=[]
    for r in snapshot.get('availability',{}).get('records',[]):
        before=old_states.get(r['player_name'].casefold(),'unknown');after=r['status'].casefold()
        if before!=after:availability.append({**r,'before':before,'after':after,
            'material':after in {'out','inactive'} or before in {'out','inactive','questionable','doubtful','probable'}})
    old_usage={r['player_name']:r for r in old_plan.get('player_usage',[])if r.get('basis')=='source_confirmed'}
    def qualified_update(row):
        from .prediction_versions import VersionStoreError
        if row.get('eligible_for_fact')is not True or not row.get('source_url')or row.get('source_role')not in {'primary','professional'}:return False
        try:
            stamp=_instant(row.get('published_at')or row.get('observed_at'),'update_time')
            captured=_instant(snapshot['captured_at'],'snapshot_time')
            if row.get('claim_type')in {'minutes_restriction','availability'}and (captured-stamp).total_seconds()>3600:return False
            return stamp<=captured
        except (ValueError,TypeError,VersionStoreError):return False
    restrictions=[r for r in snapshot.get('minutes_restrictions',[])if qualified_update(r)
        and r.get('minutes_max')!=old_usage.get(r.get('player_name'),{}).get('minutes_max')]
    news=[r for r in snapshot.get('news',[])if r.get('eligible_for_fact')and r.get('source_url')and
          qualified_update(r)and r.get('published_at')and _instant(r['published_at'],'news_time')>_instant(early['input_cutoff_at'],'early_cutoff')]
    basketball_changed=any(r['material']for r in lineups+availability)or bool(restrictions)or bool(news)
    from .final_readiness import important_players
    core_names=important_players(early)
    first_confirmation=any(r['before']=='unknown'and r['after']in {'available','active'}
        and r['player_name']in core_names.get(r['side'],[])for r in availability)
    m=market_delta(early.get('public_market_reference'),snapshot.get('market_collection',{}).get('public_market_reference'),basketball_changed,numbers(early))
    uncertainty=list(snapshot.get('information_uncertainty')or[])
    value={'lineups':lineups,'availability':availability,'minutes_restrictions':restrictions,'reliable_news':news,
        'unresolved_information':uncertainty,'uncertainty_updated':bool(uncertainty),
        'new_key_availability_confirmation':first_confirmation,'requires_change_assessment':basketball_changed or first_confirmation or bool(uncertainty),
        'market':m,'material_basketball_change':basketball_changed,'material_change':basketball_changed or m['significant_move'],
        'early_version_id':early['version_id'],'early_context_hash':early['context_hash']}
    value['delta_hash']=content_hash(value);return value

def _refs(value):
    out=set()
    if isinstance(value,dict):
        for k,v in value.items():
            if k.endswith(('evidence_ids','fact_ids'))and isinstance(v,list):out.update(x for x in v if isinstance(x,str))
            else:out.update(_refs(v))
    elif isinstance(value,list):
        for v in value:out.update(_refs(v))
    return out

def compact_context(early,snapshot,cutoff):
    c=copy.deepcopy(early['analyst_context']);game=copy.deepcopy(early['input_snapshot'])
    aliases=copy.deepcopy(snapshot.get('player_display_names')or game.get('player_display_names')or[])
    if aliases:c['game']['player_display_names']=aliases;game['player_display_names']=aliases
    c['game'].update(cutoff=cutoff,stage='FINAL_PREGAME')
    # 'stage' is task metadata, not a new AnalystContext schema field.
    c['analysis_request']='生成本场最终赛前分析；数值判断已锁定。使用此前判断与本次已核实的增量事实，不重复研究静态资料。预计首发仍是预计，未知出战仍是未知，不能把最终版写成所有信息均已确认。'
    plan=early_plan(early)or{'lineups':[],'player_usage':[],'key_matchup':early['judgment'].get('core_question')or'本场关键对位','uncertainty':[]}
    plan['lineups']=[];sources=[];evidence=[]
    for side in ('home','away'):
        lineup=snapshot['lineups'][side];team_id=game[side]['id'];names=lineup['players']
        basis=lineup.get('basis','confirmed');confirmed=basis=='confirmed'
        ref='final-lineup-'+content_hash({'side':side,'lineup':lineup})[:20]
        evidence.append({'id':ref,'kind':'SourceFact'if basis!='agent_expected'else'AnalystEstimate','section':'web_context','game_id':game['id'],'cutoff':cutoff,
            'value':{'claim_type':'confirmed_starter'if confirmed else'projected_starters','category':'lineup','team_id':team_id,'eligible_for_fact':basis!='agent_expected',
                'assertion_kind':'confirmed_fact'if confirmed else'reported_projection'if basis=='source_expected'else'analyst_estimate','temporal_class':'LIVE'if confirmed else'CURRENT','game_id':game['id'],
                'temporal_scope':'current_pregame','source_role':lineup.get('source_role','primary'),
                'quote':lineup.get('quote')or('Confirmed starters: 'if confirmed else'Expected lineup (not confirmed): ')+', '.join(names),
                'url':lineup['source_url'],'published_at':lineup.get('published_at'),'captured_at':lineup.get('observed_at')or snapshot['captured_at']}})
        sources.append({'id':ref,'url':lineup['source_url'],'captured_at':lineup.get('observed_at')or snapshot['captured_at'],'source_type':'confirmed_game_lineup'if confirmed else'expected_game_lineup'})
        plan['lineups'].append({'side':side,'basis':basis,'players':names,'evidence_ids':[ref],
            'rationale':'本场可靠比赛数据源已确认首发。'if confirmed else'沿用已发布首发预期，并核对当前归属及明确缺阵信息；本次尚未取得确认首发。'if basis=='agent_expected'else'可靠赛前报道提供的预计首发，尚未确认。'})
        team=c.setdefault('roster_context',{}).setdefault('teams',{}).setdefault(side,{})
        roster=copy.deepcopy(snapshot['rosters'][side]);roster['status']='available'
        roster['source']={'source_url':roster.get('source_url')or lineup['source_url'],'captured_at':roster.get('observed_at')or snapshot['captured_at'],
            'source_type':roster.get('roster_basis')or'confirmed_game_participants'}
        team.update(team_id=team_id,official_team_roster=roster,
            confirmed_starters=[{'name':name,'team_id':team_id,'evidence_ids':[ref]}for name in names]if confirmed else[],
            projected_starters=[]if confirmed else[{'name':name,'team_id':team_id,'evidence_ids':[ref]}for name in names],
            confirmed_starters_evidence_ids=[ref]if confirmed else[])
        game.setdefault('roster_context',{}).setdefault('teams',{}).setdefault(side,{}).update(copy.deepcopy(team))
    current_out=set()
    for r in snapshot['availability']['records']:
        ref='final-availability-'+content_hash(r)[:20];side=r['side']
        evidence.append({'id':ref,'kind':'SourceFact','section':'web_context','game_id':game['id'],'cutoff':cutoff,
            'value':{'claim_type':'availability','category':'availability','eligible_for_fact':True,'team_id':game[side]['id'],
                'assertion_kind':'confirmed_fact','temporal_class':'LIVE','game_id':game['id'],
                'player_name':r['player_name'],'status':r['status'],'quote':r['player_name']+' '+r['status'],
                'temporal_scope':'current_pregame','url':r['source_url'],'captured_at':r.get('observed_at')or snapshot['captured_at'],
                'published_at':r.get('published_at'),'source_role':r.get('source_role','primary')}})
        sources.append({'id':ref,'url':r['source_url'],'captured_at':r.get('observed_at')or snapshot['captured_at'],'source_type':'game_availability'})
        if r['status'].casefold()in {'out','inactive'}:current_out.add(r['player_name'])
    plan['player_usage']=[r for r in plan.get('player_usage',[])if r['player_name']not in current_out]
    plan['uncertainty']=list(snapshot.get('information_uncertainty')or[])[:6]
    delta=construct_delta(early,snapshot)
    for row in delta['minutes_restrictions']+delta['reliable_news']:
        if not row.get('quote')or row.get('claim_type')not in {'minutes_restriction','personnel_change','role_background','long_term_injury','availability'}:
            continue
        team_ids={game[s]['id']for s in ('home','away')}
        if row.get('team_id')not in team_ids:continue
        ref='final-update-'+content_hash(row)[:20]
        value={**copy.deepcopy(row),'url':row['source_url'],'captured_at':snapshot['captured_at'],
               'temporal_scope':row.get('temporal_scope','current_pregame')}
        evidence.append({'id':ref,'kind':'SourceFact','section':'web_context','game_id':game['id'],'cutoff':cutoff,'value':value})
        sources.append({'id':ref,'url':row['source_url'],'published_at':row.get('published_at'),
                        'captured_at':snapshot['captured_at'],'source_type':'qualified_incremental_update'})
        if row.get('claim_type')=='minutes_restriction':
            # Do not carry forward a now-conflicting EARLY estimated range.
            plan['player_usage']=[u for u in plan['player_usage']if u['player_name']!=row.get('player_name')]
    quote=snapshot['market_collection'].get('public_market_reference')
    if quote:
        ref='final-market-'+content_hash(quote)[:20]
        evidence.append({'id':ref,'kind':'MarketSnapshot','section':'web_context','game_id':game['id'],'cutoff':cutoff,
            'value':{'claim_type':'market_reference','category':'market','eligible_for_fact':True,'market_reference':copy.deepcopy(quote),
                'quote':'Current source snapshot: home spread '+str(quote.get('home_spread'))+'; total '+str(quote.get('total')),
                'url':quote['source_url'],'captured_at':quote['captured_at']}})
        sources.append({'id':ref,'url':quote['source_url'],'captured_at':quote['captured_at'],'source_type':'qualified_market_snapshot'})
    needed=_refs(early['judgment'])|_refs(early.get('editorial_material',[]))
    for old in c.get('objective_evidence',[]):
        if old['id']not in needed and old.get('section')not in {'roster_context','season_phase','fact_context'}:continue
        old=copy.deepcopy(old);v=old.get('value')or{}
        if old.get('section')in {'availability','public_market_reference','roster_context'}or v.get('claim_type')in {'availability','projected_starters','confirmed_starter','minutes_restriction','market_reference','current_roster'}:
            old['section']='early_assumption';old['kind']='BaselineContext'
            old['eligible_for_fact']=False
        evidence.append(old)
    c['roster_context']['cutoff']=cutoff
    roster_value=copy.deepcopy(c['roster_context'])
    roster_ref='final-roster-'+content_hash(roster_value)[:20]
    evidence.append({'id':roster_ref,'kind':'SourceFact','section':'roster_context','game_id':game['id'],
        'cutoff':cutoff,'value':roster_value})
    for usage in plan.get('player_usage',[]):
        if usage.get('basis')=='agent_estimate':
            # The frozen EARLY estimate is retained as an estimate. Its old
            # membership citation has been superseded by the current roster.
            usage['evidence_ids']=[roster_ref]
            usage['rationale']='沿用此前负荷估计，并核对当前归属与缺阵消息；该范围不是本场确认安排。'
    c['objective_evidence']=evidence
    c['evidence_index']={r['id']:{'section':r['section'],'source_type':r.get('kind','Context')}for r in evidence}
    if not snapshot.get('information_uncertainty'):
        c['missing_data']=[r for r in c.get('missing_data',[])if r.get('section')not in {'availability','projected_starters','confirmed_starters'}]
    c['roster_context']['cutoff']=cutoff
    c['evidence_quality'].update(final_ready=True,confirmed_lineups=all(r.get('basis')=='confirmed'for r in snapshot['lineups'].values()),
        availability_unconfirmed=bool(snapshot.get('availability_unconfirmed')),
        information_uncertainty=copy.deepcopy(snapshot.get('information_uncertainty')or[]))
    c['context_hash']=content_hash({k:v for k,v in c.items()if k!='context_hash'})
    game['official_injury_report']=copy.deepcopy(snapshot['injury']);game['public_market_reference']=copy.deepcopy(snapshot['market_collection'].get('public_market_reference'))
    game['roster_context']['cutoff']=cutoff
    return AnalystContext.model_validate(c),game,PregamePlan.model_validate(plan),sources

def _baseline_forecast(early,plan,context,folder):
    factors=[{'classification':'inference','evidence_ids':[i for i in row.get('evidence_ids',[])if i in context.evidence_index],
        'effect':row.get('effect')or'uncertainty','summary':row['summary']}
        for row in early.get('editorial_material',[])if row.get('summary')and not row.get('pregame_plan')and row.get('effect')in {'home','away','higher_total','lower_total','uncertainty','no_change'}]
    if not factors:factors=[{'classification':'inference','evidence_ids':plan.lineups[0].evidence_ids,'effect':'no_change','summary':'沿用EARLY已形成的主要判断，临场确认信息未改变核心条件。'}]
    provenance=next((r.get('value',{})for r in early.get('editorial_material',[])if r.get('_contract_item')=='forecast_provenance'),{})
    core=provenance.get('analysis_core')
    result={'schema_version':'two-stage-forecast-v2'if core else'two-stage-forecast-v1',
        'independent':{'prediction':provenance.get('independent_prediction')or numbers(early),'recomputed':False,
                       'basis':'frozen_early_independent'if provenance.get('independent_prediction')else'early_final_baseline_not_a_new_independent_call'},
        'incremental_baseline_prediction':numbers(early),
        'review':{'prediction':numbers(early),'factors':factors,'pregame_plan':plan.model_dump()},
        'final_prediction':numbers(early),'audit_path':str(folder),'skill_hashes':{},'calls':0}
    if core:result['final_analysis_core']=core
    return result

def _call_update(payload,schema,folder,stage,client=None):
    from .forecasting_skill import load_forecasting_skill
    from .skill_provenance import prompt_skill_hashes
    from .editorial_pipeline import call_recorded
    skill=load_forecasting_skill()
    messages=[{'role':'system','content':skill+'\n这是FINAL增量更新，不从零预测。EARLY是已经发布的基线。只根据新增可靠事实重新权衡受影响部分。普通价格微动不得单独推翻胜负方向。不改Official数字，不编事实。只引用本次允许的证据ID。'
        '如果有unresolved_information，必须在analysis_core中反映当前未知，不沿用“核心齐整”“全员健康”“确认会打”等未核实断言。'
        '未知不是伤病事实；没有新的实际冲突时优先保留既有数值，更新假设措辞与不确定性。已有early_core时返回完整analysis_core。'
        'analysis_core与rationale用简洁篮球语言，不复述历史统计具体数字、效率值或并排数字比较；这些数字保留在原证据表，判断引用证据ID。不要用一大串球员名字代替分析。'},
              {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
    with component(stage):value,meta=call_recorded(folder,stage,messages,schema,client,max_tokens=2600)
    return value,prompt_skill_hashes(skill),meta

def run_incremental(early,readiness,analyst,folder,*,before_writer=None,client=None,input_cutoff=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    if not readiness.get('ready'):raise VersionStoreError('final_not_ready')
    snapshot=copy.deepcopy(readiness['snapshot']);cutoff=input_cutoff or readiness['checked_at']
    if readiness.get('warnings'):
        warnings=readiness['warnings'];unknown=sum('availability_'in r for r in warnings)
        snapshot['availability_unconfirmed']=bool(unknown or'injury_coverage_incomplete'in warnings)
        snapshot['information_uncertainty']=(['首发仍有预计项，尚未取得完整确认。']if any('lineup_not_confirmed'in r for r in warnings)else[])+(
            ['部分球员本场出战状态尚未明确，负荷和出场安排只能作条件分析。']if unknown else[])+(
            ['官方伤病报告尚有未提交部分，不能把未列出的球员当作确认可出战。']if'injury_coverage_incomplete'in warnings else[])
    if snapshot['game_identity']!=early['game_identity']:raise VersionStoreError('final_baseline_identity_mismatch')
    if _instant(cutoff,'cutoff')>=_instant(early['game_identity']['scheduled_tipoff'],'tipoff'):raise VersionStoreError('pregame_generation_closed')
    context,game,plan,sources=compact_context(early,snapshot,cutoff)
    basketball,market,removed=prepare_analyst(context.model_dump());validate_pregame_plan(plan,basketball)
    delta=construct_delta(early,snapshot);forecast=_baseline_forecast(early,plan,context,folder)
    save(folder/'early_baseline.json',{'version_id':early['version_id'],'payload_sha256':early.get('payload_sha256'),'context_hash':early['context_hash'],'prediction':numbers(early)})
    save(folder/'delta.json',delta);save(folder/'final_context.json',context.model_dump())
    assessment={'decision':'retain','rationale':'新增首发和出战确认没有改变EARLY的重要篮球条件。','model_called':False}
    if delta['requires_change_assessment']:
        value,hashes,meta=_call_update({'early_prediction':numbers(early),'early_analysis':early['judgment'].get('subjective_rationale'),
            'early_core':forecast.get('final_analysis_core'),'basketball_delta':{k:v for k,v in delta.items()if k!='market'},
            'rationale_length_target':'150至250个汉字，只写本次判断摘要',
            'allowed_evidence':[{k:r[k]for k in ('id','section','value')}for r in basketball['evidence']
                if isinstance(r.get('value'),dict)and r.get('section')!='early_assumption'and r['value'].get('claim_type')!='market_reference']},DeltaAssessment,folder,'Change Assessment',client)
        if not set(value['evidence_ids'])<=set(context.evidence_index):raise ValueError('final_delta_unknown_evidence')
        prediction=ForecastNumbers.model_validate(value['prediction']).model_dump();forecast['final_prediction']=prediction;forecast['review']['prediction']=prediction
        if delta['uncertainty_updated']and forecast.get('final_analysis_core')and not value.get('analysis_core'):
            raise ValueError('final_uncertainty_requires_current_analysis_core')
        forecast['review']['factors'].append({'classification':'inference','evidence_ids':value['evidence_ids'],'effect':'uncertainty','summary':value['rationale']})
        if value.get('analysis_core'):forecast['final_analysis_core']=value['analysis_core']
        forecast['skill_hashes']=hashes;forecast['calls']+=1;assessment={**value,'decision':'update'if prediction!=numbers(early)else'retain','model_called':True}
    collection=copy.deepcopy(snapshot['market_collection'])
    comparison=build_spread_comparison(context.game,collection,numbers(early),forecast['final_prediction'])
    tie=comparison.get('final',{}).get('ats_lean')=='no_edge'
    review_needed=delta['market']['significant_move']or delta['market']['review_required']or tie or forecast['final_prediction']!=numbers(early)
    if review_needed:
        value,hashes,meta=_call_update({'early_prediction':numbers(early),'updated_prediction':forecast['final_prediction'],
            'change_assessment':assessment,'market':collection.get('public_market_reference'),'market_delta':delta['market'],
            'basketball_delta':{k:v for k,v in delta.items()if k!='market'},'early_analysis_core':forecast.get('final_analysis_core'),
            'early_basis':early.get('editorial_material',[]),'deterministic_spread_comparison':comparison,
            'tie_break_required':tie,'allowed_evidence_ids':list(context.evidence_index)},DeltaAssessment,folder,'Market Review',client)
        if not set(value['evidence_ids'])<=set(context.evidence_index):raise ValueError('final_market_unknown_evidence')
        forecast['final_prediction']=ForecastNumbers.model_validate(value['prediction']).model_dump();forecast['review']['prediction']=forecast['final_prediction']
        forecast['review']['factors'].append({'classification':'inference','evidence_ids':value['evidence_ids'],'effect':'uncertainty','summary':value['rationale']})
        if value.get('analysis_core'):forecast['final_analysis_core']=value['analysis_core']
        forecast['skill_hashes']=hashes;forecast['calls']+=1
    forecast['spread_comparison']=build_spread_comparison(context.game,collection,numbers(early),forecast['final_prediction'])
    if forecast['spread_comparison'].get('final',{}).get('ats_lean')=='no_edge':raise ValueError('final_market_spread_edge_zero')
    if forecast.get('schema_version')=='two-stage-forecast-v2':
        forecast['revision_delta']={'incremental_final':True,'baseline_version_id':early['version_id'],'delta_hash':delta['delta_hash']}
        forecast['market_provenance']={'timing_status':'observed_current_snapshot','market_signal_reviewed':review_needed,'original_information_overlap':'unknown'}
    save(folder/'change_assessment.json',assessment);save(folder/'final.json',forecast)
    if before_writer:before_writer()
    if not delta['material_basketball_change']:
        forecast['same_game_baseline_title']={'game_id':early['game_identity']['game_id'],
            'version_id':early['version_id'],'title':(early['judgment'].get('pregame_story')or{}).get('title')}
    judgment,trace=analyst.judge(context,_locked_forecast=forecast)
    from v2.frontend.analysis_service import analysis_base_fingerprint,prediction_layers,judgment_evidence_ids
    result={'status':'success','analysis_contract_version':'two-stage-forecast-v1','game_id':game['id'],'game_identity':game_identity(game),
        'cutoff':cutoff,'input_frozen_at':cutoff,'generated_at':now(),'base_input_hash':analysis_base_fingerprint(game,cutoff),
        'judgment':judgment.model_dump(),'context_hash':context.context_hash,'evidence_index':context.evidence_index,
        'input_snapshot':clean(game),'model_layers':early['model_layers'],'official_numeric_prediction':early['official_numeric_prediction'],
        'analyst_context':context.model_dump(),'research':{'mode':'early_reused_plus_qualified_delta','baseline_version_id':early['version_id'],'evidence':context.objective_evidence},
        'public_market_reference':collection.get('public_market_reference'),'market_collection':collection,'spread_comparison':forecast['spread_comparison'],
        'sources':early.get('sources',[])+sources,'cited_sources':sources,'search':{'mode':'incremental_game_information_only','llm_research_calls':(snapshot.get('information_acquisition')or{}).get('model_calls',0)},
        'analyst_trace':trace,'provenance':{'incremental':True,'baseline_version_id':early['version_id'],'baseline_payload_sha256':early.get('payload_sha256'),'early_context_hash':early['context_hash'],'final_delta_hash':delta['delta_hash']},
        'baseline_version_id':early['version_id'],'final_delta':delta,'final_readiness':readiness,'change_assessment':assessment}
    for k in ('publication_receipt','editorial_projection','editorial_material','quality_review','quality_review_input_sha256'):result[k]=trace.get(k)
    save(folder/'complete_analysis.json',result);return result
