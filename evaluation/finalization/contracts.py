"""Final candidate: strict identity/numbers, structural metadata is not prose."""
import copy,re,hashlib
from evaluation.candidate_v2.contracts import CandidateForecast,name_key
from evaluation.candidate_v4.contracts_v4 import ExactValidator
from v2.pregame_system.forecasting_workflow import ForecastNumbers
from .common import check_visible

def catalog(ctx,include_market=False):
    entries=[{'id':'context.'+k,'pointer':'/'+k} for k in ctx if include_market or k!='market']
    names={'home':{},'away':{}}
    for side,key in (('home','team_a'),('away','team_b')):
        for p in ctx[key]['player_history']['players']:
            names[side][name_key(p['player_name'])]=p['player_name']
            entries.append({'id':'historical.'+side+'.'+hashlib.sha256(p['player_name'].encode()).hexdigest()[:12],
                            'pointer':'/'+key+'/player_history','player_name':p['player_name'],'scope':'prior_observation'})
        for p in ctx.get('roster',{}).get(side,{}).get('players',[]):
            names[side][name_key(p['player_name'])]=p['player_name']
    return entries,names

def schema(ctx):
    s=CandidateForecast.model_json_schema();legal=[e['id'] for e in catalog(ctx)[0]]
    def visit(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if k=='evidence_ids' and isinstance(v,dict):v['items']={'type':'string','enum':legal}
                visit(v)
        elif isinstance(x,list):
            for v in x:visit(v)
    visit(s)
    return s

def normalize(obj):
    value=copy.deepcopy(obj);ops=[]
    if isinstance(value,dict) and isinstance(value.get('analysis_core'),dict) and 'uncertainties' in value['analysis_core']:
        u=value['analysis_core']['uncertainties']
        if 'uncertainties' in value and value['uncertainties']!=u:raise ValueError('uncertainty_conflict')
        value['uncertainties']=value['analysis_core'].pop('uncertainties');ops.append('move_unchanged_uncertainties_to_root')
    # Obvious schema annotations are never basketball analysis content.
    def clean(x):
        if isinstance(x,dict):
            for k in list(x):
                if k in {'maxItems','minItems','maxLength','minLength','$schema','$defs'}:
                    x.pop(k);ops.append('remove_schema_annotation:'+k)
                else:clean(x[k])
        elif isinstance(x,list):
            for item in x:clean(item)
    clean(value);return value,ops

def accept(obj,ctx):
    ExactValidator(schema(ctx)).validate(obj)
    value=CandidateForecast.model_validate(obj);allowed={e['id'] for e in catalog(ctx)[0]};names=catalog(ctx)[1]
    def refs(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if k=='evidence_ids' and not set(v)<=allowed:raise ValueError('unknown_evidence')
                refs(v)
        elif isinstance(x,list):
            for v in x:refs(v)
    refs(obj)
    if value.pregame_plan:
        for line in value.pregame_plan.lineups:
            for n in line.players+line.candidates:
                if name_key(n) not in names[line.side]:raise ValueError('unsupported_or_wrong_side_player:'+n)
        for item in value.pregame_plan.player_usage:
            if name_key(item.player_name) not in names[item.side]:raise ValueError('wrong_side_usage_player')
    return value.model_dump()

def preserve(original,repaired):
    if ForecastNumbers.model_validate(original['prediction']).model_dump()!=ForecastNumbers.model_validate(repaired['prediction']).model_dump():raise ValueError('repair_changed_locked_prediction')
    # Preserve actual player selections and minute ranges; list ordering and
    # citation metadata may be corrected. Prose compression is not called lossless.
    def plan_identity(o):
        p=o.get('pregame_plan') or {}
        lines=sorted((x.get('side'),tuple(sorted(x.get('players',[]))),tuple(sorted(x.get('candidates',[])))) for x in p.get('lineups',[]))
        usage=sorted((x.get('side'),x.get('player_name'),x.get('minutes_min'),x.get('minutes_max')) for x in p.get('player_usage',[]))
        return lines,usage
    if plan_identity(original)!=plan_identity(repaired):raise ValueError('repair_changed_player_selection_or_minutes')
    return True

def independent_context(ctx):
    from v2.pregame_system.forecasting_workflow import _basketball_only
    stripped=_basketball_only(copy.deepcopy(ctx),[])
    check_visible(stripped)
    return stripped
