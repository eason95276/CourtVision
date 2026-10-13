import csv,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import prepare_auxiliary as a

def test_proportional_quotas():
    q=a.quotas({'a':4,'b':2,'c':1},20)
    assert sum(q.values())==20 and q=={'a':11,'b':6,'c':3}

def test_300_selection_boundaries():
    folder=a.EVAL/'benchmark/v1'
    if not folder.exists():pytest.skip('optional300 not prepared')
    manifest=a.read_json(folder/'selection_manifest.json');rows=list(csv.DictReader((folder/'selection.csv').open(encoding='utf-8')))
    assert len(rows)==300 and len({r['sample_id'] for r in rows})==300
    assert manifest['selection_csv_sha256']==a.sha(folder/'selection.csv')
    assert manifest['model_calls']==0 and len(manifest['team_counts'])==30
    assert 'hidden_outcome_bucket' not in rows[0] and 'actual_margin' not in rows[0]
    assert {r['season'] for r in manifest['samples']}=={'2025-26'}
    assert {r['game_type'] for r in manifest['samples']}=={'regular_season'}
    body={k:v for k,v in manifest.items() if k not in ('selection_hash','revision')}
    assert a.canonical_hash(body)==manifest['selection_hash']

def test_synthetic_25_single_factor():
    folder=a.EVAL/'datasets/counterfactual_candidate_v1'
    if not folder.exists():pytest.skip('optional synthetic pairs not prepared')
    m=a.read_json(folder/'manifest.json');assert m['count']==25 and m['llm_calls']==0
    for row in m['cases']:
        file=folder/(row['mutation_id']+'.json');assert a.sha(file)==row['sha256']
        case=a.read_json(file);assert a.apply(case['control_context'],case['mutation'])==case['mutated_context']
        assert case['mutated_context']['source_type']=='synthetic_counterfactual'
        assert case['mutation']['expected_relation_if_defined']
