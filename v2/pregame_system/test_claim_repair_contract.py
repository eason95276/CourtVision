"""Cross-stage cardinality and bounded repairs, using synthetic local inputs."""
import copy
import json

import pytest
from pydantic import ValidationError

from .analyst import AnalystContext, CentralThesisClaim, KeyMatchupClaim
from .editorial_pipeline import EditorialDraft, EditorialRepair, run_editorial
from .forecasting_workflow import ForecastClaim


def inputs(count=8):
    refs = [f'fixture-{i}' for i in range(count)]
    evidence = [{'id': ref, 'section': 'fixture_context', 'value': {'note': 'Synthetic comparison'}} for ref in refs]
    context = AnalystContext(pure_prediction={'win_probability': .6}, market_benchmark=None,
        divergence=None, residual_prediction=None, official_numeric_prediction={'win_probability': .6},
        objective_evidence=evidence, evidence_index={ref: {'section': 'fixture_context', 'source_type': 'fixture'} for ref in refs},
        missing_data=[], evidence_quality={}, domain_analysis_perspectives=[], classification_legend={},
        context_hash='synthetic-claim-contract', game={'game_id': 'fixture-contract',
            'home': {'id': 1, 'name': 'Toronto Raptors', 'name_zh': '猛龙'},
            'away': {'id': 2, 'name': 'Miami Heat', 'name_zh': '热火'},
            'venue': {'fullName': 'Fixture Arena', 'address': {'city': 'Quebec City'}}},
        roster_context={'teams': {'home': {'team_id': 1, 'official_team_roster': {
            'status': 'available', 'team_id': 1, 'players': [{'name': 'Fixture Guard'}]}}}})
    forecast = {'schema_version': 'two-stage-forecast-v2',
        'final_prediction': {'winner_side': 'home', 'win_probability': .6,
                             'expected_home_margin': 3.0, 'expected_total': 220.0},
        'independent': {'prediction': {'winner_side': 'home', 'win_probability': .6,
                                       'expected_home_margin': 3.0, 'expected_total': 220.0}},
        'review': {'factors': [{'classification': 'inference', 'effect': 'home', 'summary': 'Synthetic matchup', 'evidence_ids': refs}]},
        'final_analysis_core': {'central_question': 'Which side controls the first action?',
            'central_thesis': {'statement': 'Home has a route to pressure the first defender.',
                'classification': 'inference', 'evidence_ids': refs, 'confidence': 'medium'},
            'key_matchups': [{'subject': 'Home handler', 'opponent_or_scheme': 'Away switch',
                'why_it_matters': 'It creates a second-side decision.', 'expected_effect': 'Help has to recover.',
                'classification': 'inference', 'evidence_ids': refs, 'confidence': 'medium'}],
            'expected_game_script': 'Home probes the switch while Away recovers.'}}
    return context, forecast


def draft():
    return {'title': '挡拆的第二选择', 'lead': '我更关注挡拆之后的第二次选择。',
        'article': '持球人能否找到弱侧，是比赛值得观察的地方。', 'confidence': 'low',
        'watch_themes': [], 'player_analysis': []}


@pytest.mark.parametrize('count', [7, 8])
def test_upstream_references_survive_the_complete_editorial_projection(tmp_path, count):
    context, forecast = inputs(count)
    original = copy.deepcopy(forecast)
    ForecastClaim.model_validate(forecast['final_analysis_core']['central_thesis'])
    value, trace = run_editorial(context, forecast, tmp_path/'run',
        writer_client=lambda *_: (draft(), {'fixture': True}), require_poem_title=False)
    assert value['central_thesis']['evidence_ids'] == list(context.evidence_index)
    assert value['key_matchup_variables'][0]['evidence_ids'] == list(context.evidence_index)
    assert forecast == original
    assert trace['publication_receipt']['status'] == 'eligible'


@pytest.mark.parametrize('model', [ForecastClaim, CentralThesisClaim, KeyMatchupClaim])
def test_nine_references_still_exceed_the_shared_bound(model):
    value = {'statement': 'Synthetic claim', 'classification': 'inference' if model is ForecastClaim else 'subjective_analysis',
             'evidence_ids': [str(i) for i in range(9)]}
    if model is ForecastClaim: value['confidence'] = 'low'
    with pytest.raises(ValidationError): model.model_validate(value)


@pytest.mark.parametrize('all_noop', [False, True])
def test_residual_noops_cannot_publish_or_discard_a_meaningful_fix(tmp_path, all_noop):
    context, forecast = inputs(2)
    calls = []
    def client(messages, schema):
        calls.append(schema)
        if schema is EditorialDraft:
            value = draft(); value['article'] = 'Fixture Guard 本场缺阵。比赛在多伦多举行。'
            return value, {'fixture': True}
        assert schema is EditorialRepair
        payload = json.loads(messages[-1]['content'])
        targets = payload['affected_text']
        if len(calls) == 2:
            return {'patches': [{'unit_id': u['id'], 'replacement':
                'Fixture Guard 今晚已经确认缺阵。' if 'Fixture Guard' in u['text'] else '比赛改在多伦多举行。'} for u in targets]}, {'fixture': True}
        assert len(calls) == 3
        assert all('今晚已经确认' in u['text'] or '改在多伦多' in u['text'] for u in targets)
        return {'patches': [{'unit_id': u['id'], 'replacement':
            '如果Fixture Guard无法出战，球队需要重分持球任务。'
            if not all_noop and 'Fixture Guard' in u['text'] else u['text']} for u in targets]}, {'fixture': True}
    error = 'editorial_second_repair_invalid' if all_noop else 'publication_hard_fact_blocker'
    with pytest.raises(ValueError, match=error):
        run_editorial(context, forecast, tmp_path/'run', writer_client=client, max_calls=3, require_poem_title=False)
    assert len(calls) == 3
    assert not (tmp_path/'run/publication_receipt.json').exists()
    audit = json.loads((tmp_path/'run/repair2_validation.json').read_text(encoding='utf-8'))
    if not all_noop:
        assert audit['deferred_no_op_patches']
        saved = json.loads((tmp_path/'run/draft2.json').read_text(encoding='utf-8'))
        assert '如果Fixture Guard' in saved['judgment']['analysis_article']
        assert saved['judgment']['final_prediction']['expected_total'] == 220.0
