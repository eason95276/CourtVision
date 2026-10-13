import copy,json
import pytest
from . import contracts as c
from .common import ROOT,check_visible,digest,write,read
from .data import samples,market_rows
from .run import interpretation

@pytest.fixture
def context():return read(ROOT/'evaluation/datasets/closed_book_contexts/cb-10c3baf5823776e51d36.json')
@pytest.fixture
def forecast():
    p=ROOT/'evaluation/execution/candidates/golden-pregame-system-candidate-v4/samples/cb-10c3baf5823776e51d36/accepted-forecast.json'
    return read(p)['forecast']
def test_golden_identity_only_projection():
    items=samples();assert len(items)==20 and len({s['sample_id'] for s in items})==20
    for s in items:check_visible(s);assert s['cutoff_at']<s['scheduled_tipoff']
def test_market_identity_and_all20():
    m=market_rows(samples());assert len(m)==20
    assert all(q['identity_verified'] and q['reference_type']=='historical_pregame_market_reference' for q in m.values())
def test_target_private_fields_rejected():
    for k in ('ground_truth','target_final_score','target_winner','hidden_outcome_bucket','postgame'):
        with pytest.raises(ValueError):check_visible({'nested':{k:123}})
def test_independent_market_prices_removed(context):
    context['market']={'home_spread':-4.5,'total':220.5}
    result=c.independent_context(context);assert 'market' not in result
    assert 'home_spread' not in json.dumps(result)
def test_valid_same_side_forecast(context,forecast):assert c.accept(forecast,context)['prediction']==forecast['prediction']
def test_wrong_side_rejected(context,forecast):
    forecast['pregame_plan']={'lineups':[{'side':'home','basis':'agent_expected','identity_scope':'historical_candidate','players':['LeBron James'],'evidence_ids':['context.team_a'],'rationale':'hypothesis'},
      {'side':'away','basis':'unavailable','identity_scope':'unavailable','players':[],'rationale':'unknown'}],'player_usage':[],'key_matchup':'test','uncertainty':[]}
    with pytest.raises(ValueError,match='wrong_side'):c.accept(forecast,context)
def test_repair_locks_numbers(forecast):
    modified=copy.deepcopy(forecast);modified['prediction']['expected_total']+=0.5
    with pytest.raises(ValueError,match='locked_prediction'):c.preserve(forecast,modified)
def test_repair_metadata_order_not_semantics(forecast):
    modified=copy.deepcopy(forecast);modified['factors'][0]['evidence_ids'].reverse()
    assert c.preserve(forecast,modified)
def test_schema_annotations_not_basketball(forecast):
    forecast['analysis_core']['flip_conditions'].append({'condition':'x','judgment_effect':'y','evidence_ids':['context.team_a'],'maxItems':4})
    norm,ops=c.normalize(forecast);assert norm['analysis_core']['flip_conditions'][-1].get('maxItems') is None and ops
def test_uncertainties_copy_no_overwrite(forecast):
    forecast['analysis_core']['uncertainties']=forecast.pop('uncertainties')
    norm,ops=c.normalize(forecast);assert 'uncertainties' in norm and 'uncertainties' not in norm['analysis_core'] and ops
def test_legal_decimal_056(context,forecast):
    forecast['prediction']['win_probability']=0.56;assert c.accept(forecast,context)
def test_wrong_evidence_rejected(context,forecast):
    forecast['factors'][0]['evidence_ids']=['other_sample.truth']
    with pytest.raises(Exception):c.accept(forecast,context)
def test_atomic_no_overwrite(tmp_path):
    p=tmp_path/'artifact.json';write(p,{'a':1})
    with pytest.raises(FileExistsError):write(p,{'a':2})
    assert read(p)=={'a':1}
def test_market_math_winner_ats_different():
    pred={'expected_home_margin':3.0,'expected_total':223.5};ctx={'market':{'reference':{'home_spread':-4.5,'total':220.5}}}
    out=interpretation(pred,ctx);assert out['ats_lean']=='away' and out['spread_edge']==-1.5 and out['total_lean']=='over' and out['total_edge']==3
