import copy,json,sys
from pathlib import Path
import pytest,jsonschema
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import contracts_v3 as c
from evaluation.candidate_v2.test_contracts import context,output

def two_players():
    ctx=context();ctx['team_a']['player_history']['players'].append({'player_name':'Second Home','recorded_appearances':3});return ctx

def test_partial_reference_resolution_does_not_choose_players():
    ctx=two_players();o=output();line=o['pregame_plan']['lineups'][0];line['players'].append('Second Home')
    line['evidence_ids']=[c.old.catalog(ctx)['names']['home'][c.old.name_key('Home Player')]['id']]
    raw=copy.deepcopy(o);fixed,ops=c.normalize(o,ctx);c.validate(fixed,ctx)
    assert ops and o==raw and fixed['pregame_plan']['lineups'][0]['players']==raw['pregame_plan']['lineups'][0]['players']
    assert len(fixed['pregame_plan']['lineups'][0]['evidence_ids'])==2

@pytest.mark.parametrize('name',['Away Player','Invented Player'])
def test_invalid_resolution_fails(name):
    o=output();o['pregame_plan']['lineups'][0]['players']=[name]
    with pytest.raises(ValueError):c.normalize(o,context())

def test_section_references_all_members_without_repetition():
    ctx=two_players();o=output();o['pregame_plan']['lineups'][0]['players'].append('Second Home')
    fixed,_=c.normalize(o,ctx);assert fixed['pregame_plan']['lineups'][0]['evidence_ids']==['context.team_a'];c.validate(fixed,ctx)

def test_unknown_id_not_guessed_and_enum_enforced():
    o=output();o['factors'][0]['evidence_ids']=['context.home']
    with pytest.raises(jsonschema.ValidationError):c.validate(o,context())
    o=output();o['pregame_plan']['lineups'][0]['evidence_ids']=['historical.home.typo']
    with pytest.raises(ValueError):c.normalize(o,context())

def test_length_boundary_unchanged():
    o=output();o['analysis_core']['central_question']='字'*180;c.validate(o,context())
    o['analysis_core']['central_question']='字'*181
    with pytest.raises((ValueError,jsonschema.ValidationError)):c.validate(o,context())

def test_lossless_shortening_vs_semantic_rewrite():
    o=output();o['analysis_core']['central_thesis']['statement']='  A    thesis   '
    fixed,_=c.normalize(o,context());assert fixed['analysis_core']['central_thesis']['statement']=='A thesis'
    assert c.preserve(o,fixed,context())
    fixed['analysis_core']['central_thesis']['statement']='Different thesis'
    with pytest.raises(ValueError):c.preserve(o,fixed,context())

def test_field_migration_not_deletion():
    o=output();o['analysis_core']['uncertainties']=o.pop('uncertainties');fixed,_=c.normalize(o,context());assert c.preserve(o,fixed,context())
    fixed['uncertainties']=[]
    with pytest.raises(ValueError):c.preserve(o,fixed,context())

@pytest.mark.parametrize('mutation',['prediction','matchup','confirmed','game_script','uncertainty','cross_sample'])
def test_semantic_mutants_rejected(mutation):
    o=output();z=copy.deepcopy(o)
    if mutation=='prediction':z['prediction']['expected_total']=225.0
    elif mutation=='matchup':z['analysis_core']['key_matchups']=[]
    elif mutation=='confirmed':z['pregame_plan']['lineups'][0]['basis']='confirmed'
    elif mutation=='game_script':z['analysis_core']['expected_game_script']='Reversed conclusion'
    elif mutation=='uncertainty':z['uncertainties']=[]
    else:z['pregame_plan']['lineups'][0]['players']=['Other Sample Star']
    with pytest.raises(ValueError):c.preserve(o,z,context())

def test_enum_is_per_sample_not_global():
    a=context();z=two_players();sa=json.dumps(c.schema(a));sz=json.dumps(c.schema(z))
    ident=c.old.catalog(z)['names']['home'][c.old.name_key('Second Home')]['id']
    assert ident not in sa and ident in sz
