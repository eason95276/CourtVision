import copy
from collections import defaultdict
from pathlib import Path
import pytest
from asset_scanner import canonical_hash
from closed_book_data import build_context,past
from closed_book_anonymize import anonymize,identity_leaks
from validate_closed_book_context import validate

@pytest.fixture
def sample():
    class Fake:pass
    ledger=Fake();ledger.source_hashes={};ledger.teams=defaultdict(list);ledger.players=defaultdict(list);ledger.games={};ledger.season_counts={}
    for id,day,pa,pb in [('prior','2025-10-20',110,100),('target','2025-10-22',80,140),('future','2025-10-23',150,50)]:
        ledger.games[id]={'game_id':id,'day':day,'season':2025,'kind':'regular_season','home_id':1,'away_id':2,'home_points':pa,'away_points':pb,'source_ref':'fixture:'+id}
        for team,opp,pf,ag in [(1,2,pa,pb),(2,1,pb,pa)]:
            ledger.teams[team].append({'game_id':id,'day':day,'season':2025,'kind':'regular_season','team_id':team,'opponent_id':opp,'points_for':pf,'points_against':ag,'source_ref':'fixture:'+id})
            ledger.players[team].append({'game_id':id,'day':day,'team_id':team,'player_id':str(team)+'99','player_name':'Alice Alpha' if team==1 else 'Bob Beta',
                'participation_status':'played','minutes_seconds':1800 if id=='prior' else 9999,'starter':1,'points':20 if id=='prior' else 90,
                'assists':2,'rebounds':4,'turnovers':1,'steals':1,'blocks':0,'source_ref':'fixture:player:'+id})
    target={'game_id':'target','game_date':'2025-10-22','game_type':'regular_season','home_team_id':1,'away_team_id':2,'home_team':'Alpha City','away_team':'Beta City'}
    return build_context(ledger,target)

def test_builder_excludes_target_and_future(sample):
    c,p=sample
    assert c['team_a']['record']['wins_before_game']==1
    assert c['team_a']['recent_form']['recent_5']['sample_games']==1
    assert c['team_a']['player_history']['players'][0]['points_per_appearance']['value']==20
    assert c['team_a']['recent_rotation_candidates']['players'][0]['minutes_per_appearance']==30
    assert c['head_to_head']['actual_meetings']==1
    assert validate(c,p)==[]

def test_same_day_excluded():
    assert past([{'game_id':'earlier_same_day','day':'2025-10-22'}],'2025-10-22','target')==[]

def test_same_day_provenance_is_leakage(sample):
    c,p=copy.deepcopy(sample);p['fields']['recent_form']['source_events'][0]['event_time']='2025-10-22'
    assert any('future_source_event' in issue for issue in validate(c,p))

def test_null_player_minutes_are_not_zero(sample):
    from closed_book_data import player_summary
    rows=[{'player_id':'x','player_name':'Player Unknown','participation_status':'played','minutes_seconds':None,'starter':None,
        **{k:None for k in ('points','assists','rebounds','turnovers','steals','blocks')}}]
    result=player_summary(rows)[0]
    assert result['minutes_per_appearance'] is None and result['starter_frequency'] is None

def test_wrong_provenance_max_fails(sample):
    c,p=copy.deepcopy(sample);p['fields']['h2h']['max_source_event_time']='2025-10-23'
    assert any('provenance_max_mismatch' in issue for issue in validate(c,p))

def test_wrong_target_identity_fails(sample):
    c,p=copy.deepcopy(sample);c['matchup']['target_game_id']='other_game'
    assert 'target_identity_mismatch' in validate(c,p)

def test_wrong_home_away_fails(sample):
    c,p=copy.deepcopy(sample);c['matchup']['team_a_role']='away'
    assert 'home_away_role_mismatch' in validate(c,p)

def test_unexpected_hidden_payload_cannot_enter_root(sample):
    c,p=copy.deepcopy(sample);c['debug_blob']={'answer':123}
    assert 'unexpected_model_context_root' in validate(c,p)

def test_sdk_adapter_refuses_unfilled_model_output(sample):
    from future_eval_adapters import deepeval_case
    from export_closed_book import model_input
    with pytest.raises(ValueError):deepeval_case(model_input(sample[0]),None)

def test_sdk_adapter_only_constructs_case_no_evaluation(sample):
    from future_eval_adapters import deepeval_case
    from export_closed_book import model_input
    case=deepeval_case(model_input(sample[0]),'UNIT TEST FIXTURE ONLY, not a generated benchmark answer',allow_unfrozen_fixture=True)
    assert case.expected_output is None and len(case.context)==1
    assert 'closed_book_ground_truth' not in case.input

@pytest.mark.parametrize('key',['final_score','winner','actual_margin','actual_total','target_boxscore','target_minutes','target_starters','hidden_labels'])
def test_forbidden_target_fields_fail(sample,key):
    c,p=copy.deepcopy(sample);c[key]=123
    assert validate(c,p)

def test_target_recent_form_fails(sample):
    c,p=copy.deepcopy(sample);c['team_a']['recent_form']['recent_5']['games'][0]['game_ref']='target'
    assert any('target_game_in_history' in s for s in validate(c,p))

def test_future_player_provenance_fails_even_if_claimed_zero(sample):
    c,p=copy.deepcopy(sample);p['fields']['player_history']['source_events'][0]['event_time']='2025-10-23'
    assert any('future_source_event' in s for s in validate(c,p))

def test_future_visible_date_fails(sample):
    c,p=copy.deepcopy(sample);c['head_to_head']['meetings'][0]['event_date']='2025-10-23'
    assert any('future_or_target_event' in s for s in validate(c,p))

def test_hidden_reference_fails(sample):
    c,p=copy.deepcopy(sample);c['link']='datasets/closed_book_ground_truth/answer.json'
    assert any('hidden_or_external_reference' in s for s in validate(c,p))

def test_gt_same_directory_fails(sample,tmp_path):
    c,p=sample
    assert 'ground_truth_not_physically_isolated' in validate(c,p,context_path=tmp_path/'c.json',truth_path=tmp_path/'gt.json')

def test_wrong_aggregate_hash_fails(sample):
    c,p=copy.deepcopy(sample);c['team_a']['record']['wins_before_game']=999
    assert any('field_hash_mismatch' in s for s in validate(c,p))

def test_anonymization_stable_complete(sample):
    c,p=sample;anon,m=anonymize(c);again,m2=anonymize(c)
    assert anon==again and m==m2
    assert not identity_leaks(anon,m)
    assert validate(anon,p,m)==[]
    assert anon['team_a']['team_name']=='Team A'
    assert anon['team_a']['player_history']['players'][0]['player_name']==anon['team_a']['recent_rotation_candidates']['players'][0]['player_name']

@pytest.mark.parametrize('leak',['Alpha City','Alice Alpha','2025-10-22'])
def test_anonymous_leaky_fixture_fails(sample,leak):
    c,p=sample;anon,m=anonymize(c);anon['note']=leak
    assert validate(anon,p,m)

def test_anonymous_id_field_fails(sample):
    c,p=sample;anon,m=anonymize(c);anon['espn_id']='123'
    assert any('identity_key' in s for s in validate(anon,p,m))

def test_market_and_injury_remain_missing(sample):
    c,p=sample
    assert c['market']['status']=='unavailable' and c['injury']['status']=='unavailable'

def test_counterfactual_only_selected_factor_and_explicit_synthetic(sample):
    from counterfactual import record,apply
    c,p=sample;mutation=record('sample','market','/market',c['market'],{'home_line':-8},'Synthetic, not historical quote')
    altered=apply(c,mutation)
    assert altered['source_type']=='synthetic_counterfactual'
    assert altered['team_a']==c['team_a'] and altered['head_to_head']==c['head_to_head']
    assert c['market']['status']=='unavailable' and mutation['expected_relation_if_defined'] is None

def test_counterfactual_cannot_mutate_target_answer():
    from counterfactual import record
    with pytest.raises(ValueError):record('sample','market','/actual_total',None,240)

def test_export_null_outputs_no_gt(sample):
    from export_closed_book import model_input
    c,p=sample;export=model_input(c)
    assert export['actual_output'] is None and export['expected_output'] is None
    assert 'ground_truth' not in export and 'ground_truth_path' not in export['metadata']

def test_counterfactual_schema_accepts_no_expected_answer(sample):
    from counterfactual import record
    from jsonschema import Draft202012Validator
    from asset_scanner import EVAL,read_json
    m=record('sample','availability','/injury',{'status':'unavailable'},{'status':'out'},'Synthetic controlled factor')
    Draft202012Validator(read_json(EVAL/'datasets/counterfactual-mutation.schema.json')).validate(m)
