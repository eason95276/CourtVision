"""Exact historical evidence resolution, unchanged limits, conservative semantic lock."""
import copy,re,jsonschema
from evaluation.candidate_v2 import contracts as old

def schema(context):
    result=old.schema();legal=sorted(e['id'] for e in old.catalog(context)['entries'])
    def visit(obj):
        if isinstance(obj,dict):
            for key,value in obj.items():
                if key in ('evidence_ids','fact_ids') and isinstance(value,dict) and value.get('type')=='array':value['items']={'type':'string','enum':legal}
                visit(value)
        elif isinstance(obj,list):
            for x in obj:visit(x)
    visit(result);return result

def normalize(output,context):
    obj,ops=old.normalize_structure(output,context)
    if not isinstance(obj,dict):return obj,ops
    index=old.catalog(context);allowed={e['id'] for e in index['entries']}
    plan=obj.get('pregame_plan')
    if isinstance(plan,dict):
        for item in plan.get('lineups',[])+plan.get('player_usage',[]):
            if not isinstance(item,dict) or item.get('basis')=='unavailable':continue
            side=item.get('side');names=item.get('players',[])+item.get('candidates',[]) if 'players' in item or 'candidates' in item else [item.get('player_name')]
            refs=item.get('evidence_ids',[])
            if not isinstance(refs,list) or not set(refs)<=allowed:raise ValueError('unknown_evidence_not_resolved')
            resolved=[]
            for name in names:
                if not isinstance(name,str):raise ValueError('invalid_player_name')
                member=index['names'].get(side,{}).get(old.name_key(name))
                if not member:raise ValueError('unsupported_or_wrong_side_historical_candidate:'+name)
                resolved.append(member)
            group='context.team_a' if side=='home' else 'context.team_b'
            for member in resolved:
                if group not in refs and member['id'] not in refs:
                    refs.append(member['id']);ops.append({'operation':'append_exact_existing_player_reference','side':side,'name':member['name'],'id':member['id'],'pointers':member['paths']})
            item['evidence_ids']=refs
    # Lossless formatting only: no text paraphrase, no truncation, no word removal.
    text_keys={'statement','summary','central_question','expected_game_script','subject','opponent_or_scheme','why_it_matters','expected_effect','condition','judgment_effect','rationale','key_matchup'}
    def format_text(v,path='root'):
        if isinstance(v,dict):
            for key,value in v.items():
                if key in text_keys and isinstance(value,str):
                    compact=re.sub(r'\s+',' ',value).strip()
                    if compact!=value:
                        v[key]=compact;ops.append({'operation':'lossless_whitespace_format','path':path+'.'+key,'before':value,'after':compact})
                else:format_text(value,path+'.'+key)
        elif isinstance(v,list):
            for i,x in enumerate(v):format_text(x,path+f'[{i}]')
    format_text(obj);return obj,ops

def validate(output,context):
    jsonschema.validate(output,schema(context))
    return old.validate_historical(output,context)

def canonical_semantics(output,context):
    obj,_=normalize(output,context)
    if not isinstance(obj,dict):raise ValueError('not_json_object')
    return obj

def preserve(original,repaired,context):
    # Canonical representation retains all lexical text, numbers and list membership.
    a=canonical_semantics(original,context);z=canonical_semantics(repaired,context)
    old.preserve_semantics(a,z,context);validate(z,context);return True
