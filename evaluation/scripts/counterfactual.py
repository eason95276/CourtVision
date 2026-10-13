"""Explicit one-factor scaffolding. No bulk set, answer, rubric or model call."""
import copy
from asset_scanner import canonical_hash

ALLOWED={'availability':('/team_a/availability','/team_b/availability','/injury'),
    'rest':('/schedule_context/team_a','/schedule_context/team_b'),
    'market':('/market',),'venue':('/matchup/venue',),'recent_form':('/team_a/recent_form','/team_b/recent_form')}

def record(base_sample_id,mutation_type,target_pointer,original_value,mutated_value,notes='',expected_relation_if_defined=None):
    if mutation_type not in ALLOWED:raise ValueError('unsupported mutation type')
    if not any(target_pointer==p or target_pointer.startswith(p+'/') for p in ALLOWED[mutation_type]):raise ValueError('mutation path not in selected factor')
    body={'base_sample_id':base_sample_id,'mutation_type':mutation_type,'target_pointer':target_pointer,'original_value':original_value,
        'mutated_value':mutated_value,'expected_relation_if_defined':expected_relation_if_defined,'notes':notes,'source_type':'synthetic_counterfactual'}
    return {**body,'mutation_id':'mutation-'+canonical_hash(body)[:20]}

def apply(context,mutation):
    if mutation.get('source_type')!='synthetic_counterfactual':raise ValueError('synthetic source declaration required')
    checked=record(**{k:mutation[k] for k in ('base_sample_id','mutation_type','target_pointer','original_value','mutated_value','notes','expected_relation_if_defined')})
    if checked['mutation_id']!=mutation['mutation_id']:raise ValueError('mutation record mismatch')
    result=copy.deepcopy(context);keys=mutation['target_pointer'].strip('/').split('/');parent=result
    for key in keys[:-1]:parent=parent[key]
    if parent.get(keys[-1])!=mutation['original_value']:raise ValueError('original factor no longer matches')
    parent[keys[-1]]=copy.deepcopy(mutation['mutated_value']);result['source_type']='synthetic_counterfactual'
    return result
