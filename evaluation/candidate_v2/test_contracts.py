import copy,json,sys
from pathlib import Path
import pytest,jsonschema
sys.path.insert(0,str(Path(__file__).resolve().parent))
import contracts as c

def context():
    result={k:{} for k in ('matchup','team_a','team_b','head_to_head','schedule_context','injury','known_missing_information')}
    for side,name in [('team_a','Home Player'),('team_b','Away Player')]:
        result[side]={'player_history':{'players':[{'player_name':name,'recorded_appearances':2}]},'recent_rotation_candidates':{'players':[]}}
    return result

def output():
    return {'prediction':{'winner_side':'home','win_probability':.55,'expected_home_margin':3.0,'expected_total':223.5},
        'factors':[{'classification':'inference','evidence_ids':['context.team_a'],'effect':'home','summary':'A mechanism'}],
        'uncertainties':['Current identity is not confirmed'],
        'analysis_core':{'central_question':'Question','central_thesis':{'statement':'A thesis','classification':'inference','evidence_ids':['context.team_a'],'confidence':'low'},
            'matchup_status':'analytical','key_matchups':[{'subject':'Home Player','opponent_or_scheme':'Away scheme','why_it_matters':'Mechanism','expected_effect':'Possibility','classification':'inference','evidence_ids':['context.team_a'],'confidence':'low'}],
            'expected_game_script':'A conditional script','flip_conditions':[]},
        'pregame_plan':{'lineups':[{'side':'home','basis':'agent_expected','identity_scope':'historical_candidate','players':['Home Player'],'evidence_ids':['context.team_a'],'rationale':'Historical only; unconfirmed'},
             {'side':'away','basis':'unavailable','identity_scope':'unavailable','players':[],'evidence_ids':[],'rationale':'Not enough information'}],
             'player_usage':[],'key_matchup':'A possible matchup','uncertainty':[]}}

def test_historical_partial_positive():
    o=output();c.validate_historical(o,context());jsonschema.validate(o,c.schema())

@pytest.mark.parametrize('basis',['confirmed','source_expected','source_confirmed'])
def test_no_current_identity_upgrade(basis):
    o=output();o['pregame_plan']['lineups'][0]['basis']=basis
    with pytest.raises(ValueError):c.validate_historical(o,context())

@pytest.mark.parametrize('name',['Away Player','Unsupported Player'])
def test_wrong_side_and_unsupported_rejected(name):
    o=output();o['pregame_plan']['lineups'][0]['players']=[name]
    with pytest.raises(ValueError):c.validate_historical(o,context())

def test_wrong_reference_rejected():
    o=output();o['pregame_plan']['lineups'][0]['evidence_ids']=['context.team_b']
    with pytest.raises(ValueError):c.validate_historical(o,context())

def test_missing_evidence_copy_receipt():
    o=output();del o['pregame_plan']['lineups'][0]['evidence_ids'];original=copy.deepcopy(o)
    with pytest.raises(ValueError):c.validate_historical(o,context())
    fixed,ops=c.normalize_structure(o,context());c.validate_historical(fixed,context())
    assert ops and o==original and fixed['pregame_plan']['lineups'][0]['evidence_ids'][0].startswith('historical.home.')

def test_matchup_state_machine():
    o=output();o['analysis_core']['matchup_status']='no_clear_matchup'
    with pytest.raises(ValueError):c.validate_historical(o,context())
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(o,c.schema())
    fixed,ops=c.normalize_structure(o,context());assert fixed['analysis_core']['key_matchups']==o['analysis_core']['key_matchups']
    c.validate_historical(fixed,context());assert ops
    o['analysis_core']['key_matchups']=[];c.validate_historical(o,context())

def test_analytical_requires_inference():
    o=output();o['analysis_core']['key_matchups'][0]['classification']='source_fact'
    with pytest.raises(ValueError):c.validate_historical(o,context())

def test_semantic_migration_and_deletion():
    o=output();o['analysis_core']['uncertainties']=o.pop('uncertainties')
    fixed,ops=c.normalize_structure(o,context());assert c.preserve_semantics(o,fixed,context())
    fixed['uncertainties']=[]
    with pytest.raises(ValueError):c.preserve_semantics(o,fixed,context())

@pytest.mark.parametrize('field',['winner_side','win_probability','expected_home_margin','expected_total'])
def test_prediction_lock(field):
    o=output();r=copy.deepcopy(o);r['prediction'][field]={'winner_side':'away','win_probability':.56,'expected_home_margin':4.0,'expected_total':224.0}[field]
    with pytest.raises(ValueError):c.preserve_semantics(o,r,context())

@pytest.mark.parametrize('mutation',['delete','rewrite','add','empty_list'])
def test_content_mutants_killed(mutation):
    o=output();r=copy.deepcopy(o)
    if mutation=='delete':del r['analysis_core']['expected_game_script']
    elif mutation=='rewrite':r['analysis_core']['central_thesis']['statement']='Different basketball'
    elif mutation=='add':r['new_basketball_conclusion']='New'
    else:r['analysis_core']['key_matchups']=[]
    with pytest.raises(ValueError):c.preserve_semantics(o,r,context())

def test_unknown_evidence_rejected():
    o=output();o['factors'][0]['evidence_ids']=['invented']
    with pytest.raises(ValueError):c.validate_historical(o,context())

def test_unavailable_cannot_hide_names():
    o=output();o['pregame_plan']['lineups'][1]['players']=['Away Player']
    with pytest.raises(ValueError):c.validate_historical(o,context())

def test_malformed_json_not_regex_repaired():
    with pytest.raises(json.JSONDecodeError):json.loads('{"prediction":')

def test_baseline_artifacts_unchanged():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
    import run_baseline_v1 as b
    before=json.loads((b.EVAL/'reports/overnight-protection-before.json').read_text(encoding='utf-8'))
    assert b.canonical_hash(b.root_protection())==b.canonical_hash(before['production_and_frozen'])
    for group in ('baseline_files','baseline_reports','first_attempt_tree'):
        for path,h in before[group].items():assert b.sha(b.ROOT/path)==h
