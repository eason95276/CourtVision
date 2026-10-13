"""Candidate-only historical contract. Live/Baseline validators are untouched."""
import copy,json,sys,hashlib,unicodedata
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from v2.pregame_system.forecasting_workflow import ForecastNumbers,ForecastFactor,ForecastClaim,KeyMatchup,FlipCondition


class HistoricalLineup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    side:Literal['home','away']
    basis:Literal['agent_expected','unavailable']
    identity_scope:Literal['historical_candidate','unavailable']
    players:list[str]=Field(default_factory=list,max_length=5)
    candidates:list[str]=Field(default_factory=list,max_length=5)
    evidence_ids:list[str]=Field(default_factory=list,max_length=12)
    rationale:str=Field(min_length=1,max_length=500)
    @model_validator(mode='after')
    def coherent(self):
        if len(set(self.players))!=len(self.players) or len(set(self.candidates))!=len(self.candidates):raise ValueError('duplicate_historical_candidate')
        if self.basis=='unavailable':
            if self.players or self.candidates or self.identity_scope!='unavailable':raise ValueError('unavailable_lineup_has_candidates')
        elif self.identity_scope!='historical_candidate' or not (self.players or self.candidates) or not self.evidence_ids:
            raise ValueError('historical_candidate_requires_scope_names_evidence')
        return self


class HistoricalUsage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    side:Literal['home','away']
    player_name:str=Field(min_length=1,max_length=100)
    basis:Literal['agent_estimate']
    identity_scope:Literal['historical_candidate']
    minutes_min:int=Field(ge=0,le=48)
    minutes_max:int=Field(ge=0,le=48)
    evidence_ids:list[str]=Field(min_length=1,max_length=12)
    rationale:str=Field(min_length=1,max_length=500)
    @model_validator(mode='after')
    def ordered(self):
        if self.minutes_min>self.minutes_max:raise ValueError('minutes_range_reversed')
        return self


class HistoricalPlan(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lineups:list[HistoricalLineup]=Field(min_length=2,max_length=2)
    player_usage:list[HistoricalUsage]=Field(default_factory=list,max_length=8)
    key_matchup:str=Field(min_length=1,max_length=700)
    uncertainty:list[str]=Field(default_factory=list,max_length=6)
    @model_validator(mode='after')
    def sides(self):
        if {x.side for x in self.lineups}!={'home','away'}:raise ValueError('lineup_sides_incomplete')
        return self


class CandidateCore(BaseModel):
    model_config=ConfigDict(extra='forbid')
    central_question:str=Field(min_length=1,max_length=180)
    central_thesis:ForecastClaim
    matchup_status:Literal['identified','analytical','no_clear_matchup']
    key_matchups:list[KeyMatchup]=Field(default_factory=list,max_length=3)
    expected_game_script:str=Field(min_length=1,max_length=480)
    flip_conditions:list[FlipCondition]=Field(default_factory=list,max_length=4)
    @model_validator(mode='after')
    def status_list(self):
        if self.matchup_status=='no_clear_matchup' and self.key_matchups:raise ValueError('no_clear_requires_empty_list')
        if self.matchup_status in ('identified','analytical') and not self.key_matchups:raise ValueError('matchup_status_requires_list')
        if self.matchup_status=='analytical' and any(x.classification!='inference' for x in self.key_matchups):raise ValueError('analytical_matchup_must_be_inference')
        return self


class CandidateForecast(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prediction:ForecastNumbers
    factors:list[ForecastFactor]=Field(min_length=1,max_length=6)
    uncertainties:list[str]=Field(default_factory=list,max_length=5)
    pregame_plan:HistoricalPlan|None=None
    analysis_core:CandidateCore


def name_key(name):return ' '.join(unicodedata.normalize('NFKC',name).casefold().split())


def catalog(context):
    result={'entries':[],'names':{'home':{},'away':{}}}
    for field in ('matchup','team_a','team_b','head_to_head','schedule_context','injury','known_missing_information'):
        result['entries'].append({'id':'context.'+field,'context_pointer':'/'+field,'scope':'frozen_context','value':context[field]})
    for side,key in [('home','team_a'),('away','team_b')]:
        for group in ('player_history','recent_rotation_candidates'):
            for i,p in enumerate(context[key][group].get('players',[])):
                if p.get('recorded_appearances',0)<=0 or not p.get('player_name'):continue
                n=name_key(p['player_name']);id='historical.'+side+'.'+hashlib.sha256(p['player_name'].encode()).hexdigest()[:12]
                path=f'/{key}/{group}/players/{i}'
                if n not in result['names'][side]:
                    result['names'][side][n]={'id':id,'name':p['player_name'],'paths':[]}
                    result['entries'].append({'id':id,'side':side,'player_name':p['player_name'],'scope':'historically_observed_not_current','context_pointer':path})
                result['names'][side][n]['paths'].append(path)
    return result


def schema():
    s=CandidateForecast.model_json_schema();core=s['$defs']['CandidateCore'];line=s['$defs']['HistoricalLineup']
    core['allOf']=[
        {'if':{'properties':{'matchup_status':{'const':'no_clear_matchup'}},'required':['matchup_status']},'then':{'properties':{'key_matchups':{'maxItems':0}}}},
        {'if':{'properties':{'matchup_status':{'enum':['identified','analytical']}},'required':['matchup_status']},'then':{'properties':{'key_matchups':{'minItems':1}}}}]
    line['allOf']=[
        {'if':{'properties':{'basis':{'const':'unavailable'}},'required':['basis']},'then':{'properties':{'identity_scope':{'const':'unavailable'},'players':{'maxItems':0},'candidates':{'maxItems':0}}}},
        {'if':{'properties':{'basis':{'const':'agent_expected'}},'required':['basis']},'then':{'required':['evidence_ids'],'properties':{'identity_scope':{'const':'historical_candidate'},'evidence_ids':{'minItems':1}},'anyOf':[{'properties':{'players':{'minItems':1}},'required':['players']},{'properties':{'candidates':{'minItems':1}},'required':['candidates']}]}}]
    return s


def validate_historical(output,context):
    parsed=CandidateForecast.model_validate(output);index=catalog(context);allowed={e['id'] for e in index['entries']}
    refs=set()
    for f in parsed.factors:refs.update(f.evidence_ids)
    refs.update(parsed.analysis_core.central_thesis.evidence_ids)
    for m in parsed.analysis_core.key_matchups:refs.update(m.evidence_ids)
    for f in parsed.analysis_core.flip_conditions:refs.update(f.evidence_ids)
    if not refs<=allowed:raise ValueError('unknown_candidate_evidence_id')
    plan=parsed.pregame_plan
    if plan:
        for lineup in plan.lineups:
            if lineup.basis not in ('agent_expected','unavailable'):raise ValueError('historical_identity_cannot_confirm_starter')
            if not set(lineup.evidence_ids)<=allowed:raise ValueError('unknown_lineup_evidence')
            side=lineup.side;group='context.team_a' if side=='home' else 'context.team_b'
            for name in lineup.players+lineup.candidates:
                member=index['names'][side].get(name_key(name))
                if not member:raise ValueError('unsupported_or_wrong_side_historical_candidate:'+name)
                if group not in lineup.evidence_ids and member['id'] not in lineup.evidence_ids:
                    raise ValueError('historical_player_not_grounded_by_reference:'+name)
        for usage in plan.player_usage:
            member=index['names'][usage.side].get(name_key(usage.player_name));group='context.team_a' if usage.side=='home' else 'context.team_b'
            if not member:raise ValueError('unsupported_historical_usage_player')
            if usage.basis!='agent_estimate':raise ValueError('historical_minutes_cannot_be_confirmed')
            if not set(usage.evidence_ids)<=allowed or group not in usage.evidence_ids and member['id'] not in usage.evidence_ids:
                raise ValueError('historical_usage_evidence_mismatch')
    return parsed


def normalize_structure(output,context):
    """Explicit lossless operations on a COPY; original model artifact untouched."""
    obj=copy.deepcopy(output);ops=[]
    if not isinstance(obj,dict):return obj,ops
    core=obj.get('analysis_core')
    if isinstance(core,dict) and 'uncertainties' in core:
        value=core['uncertainties']
        if 'uncertainties' in obj and obj['uncertainties']!=value:raise ValueError('conflicting_uncertainty_locations')
        obj['uncertainties']=copy.deepcopy(value);del core['uncertainties'];ops.append('migrate analysis_core.uncertainties -> root.uncertainties unchanged')
    if isinstance(core,dict) and core.get('matchup_status')=='no_clear_matchup' and core.get('key_matchups'):
        if all(isinstance(m,dict) and m.get('classification')=='inference' for m in core['key_matchups']):
            core['matchup_status']='analytical';ops.append('tag non-empty inference-only matchups analytical; list/text unchanged')
    index=catalog(context);plan=obj.get('pregame_plan')
    if isinstance(plan,dict):
        for item in plan.get('lineups',[])+plan.get('player_usage',[]):
            if not isinstance(item,dict):continue
            is_unavailable=item.get('basis')=='unavailable'
            if 'identity_scope' not in item:
                item['identity_scope']='unavailable' if is_unavailable else 'historical_candidate';ops.append('attach historical identity scope metadata')
            if not item.get('evidence_ids') and not is_unavailable:
                side=item.get('side');names=item.get('players',[])+item.get('candidates',[]) if 'players' in item or 'candidates' in item else [item.get('player_name')]
                found=[index['names'].get(side,{}).get(name_key(n)) for n in names if isinstance(n,str)]
                if found and len(found)==len(names) and all(found):
                    item['evidence_ids']=list(dict.fromkeys(x['id'] for x in found));ops.append('derive missing citation metadata from exact frozen same-side observations')
    return obj,ops


def preserve_semantics(original,repaired,context):
    """Numbers/text/list membership remain locked; only named structure/metadata may change."""
    a,_=normalize_structure(original,context);b,_=normalize_structure(repaired,context)
    if ForecastNumbers.model_validate(a.get('prediction')).model_dump()!=ForecastNumbers.model_validate(b.get('prediction')).model_dump():
        raise ValueError('semantic_repair_changed_prediction')
    def walk(before,after,path):
        if isinstance(before,dict):
            if not isinstance(after,dict):raise ValueError('semantic_repair_changed_object:'+path)
            for key,value in before.items():
                if key not in after:raise ValueError('semantic_repair_deleted_field:'+path+'.'+key)
                walk(value,after[key],path+'.'+key)
            added=set(after)-set(before)
            for key in added:
                if key in ('evidence_ids','identity_scope'):continue
                if key=='classification' and after[key]=='inference':continue
                if key=='confidence' and after[key]=='low':continue
                if key in ('uncertainties','key_matchups','flip_conditions','player_usage','candidates') and after[key]==[]:continue
                if key=='pregame_plan' and after[key] is None:continue
                raise ValueError('semantic_repair_added_content:'+path+'.'+key)
        elif isinstance(before,list):
            if not isinstance(after,list) or len(before)!=len(after):raise ValueError('semantic_repair_changed_list:'+path)
            for i,(x,y) in enumerate(zip(before,after)):walk(x,y,path+f'[{i}]')
        elif before!=after:raise ValueError('semantic_repair_changed_existing_value:'+path)
    # String thesis -> object with identical statement and valid metadata is structural.
    for obj in (a,b):
        core=obj.get('analysis_core',{})
        if isinstance(core.get('central_thesis'),str):core['central_thesis']={'statement':core['central_thesis']}
    walk(a,b,'root');validate_historical(b,context)
    return True
