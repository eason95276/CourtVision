import copy
import pytest
from .test_final_readiness import fixture
from .final_readiness import snapshot_from_espn,verified_provider_ids,FinalReadinessStore
from .prediction_versions import PredictionVersionStore,VersionStoreError
from .generation_tasks import GenerationTaskStore


def summary(game):
    return {'header':{'id':'123','competitions':[{'id':'123','date':game['tipoff'],'status':{'type':{'state':'pre'}},
        'competitors':[{'homeAway':'home','team':{'id':'9','abbreviation':'GS'}},
                       {'homeAway':'away','team':{'id':'13','abbreviation':'LAL'}}]}]}}


def test_registering_a_display_snapshot_preserves_provider_identity(tmp_path):
    game,early,snapshot,at=fixture();store=PredictionVersionStore(tmp_path/'v.sqlite',clock=lambda:at)
    tasks=GenerationTaskStore(store);tasks.register_game(game)
    display=copy.deepcopy(game)
    for side in ('home','away'):display[side].pop('espn_team_id')
    tasks.ensure_schedule(display)
    assert tasks.registered_game(game['id'])['home']['espn_team_id']=='9'
    display['home']['espn_team_id']='13'
    with pytest.raises(VersionStoreError,match='provider_identity_changed'):tasks.ensure_schedule(display)


def test_verified_summary_recovers_missing_provider_ids_without_guessing():
    game,early,snapshot,at=fixture();data=summary(game)
    for side in ('home','away'):game[side].pop('espn_team_id')
    assert verified_provider_ids(game,data)=={'home':'9','away':'13'}
    assert snapshot_from_espn(game,data,{'url':'https://example.org/summary','captured_at':at.isoformat()})['game_state']=='pre'
    data['header']['competitions'][0]['competitors'][0]['team']['abbreviation']='BOS'
    with pytest.raises(ValueError,match='team_direction_mismatch'):verified_provider_ids(game,data)


def test_collector_programming_failure_is_diagnostic_and_never_ready(tmp_path):
    game,early,snapshot,at=fixture();store=PredictionVersionStore(tmp_path/'v.sqlite',clock=lambda:at)
    def broken(*args):raise KeyError('espn_team_id')
    result=FinalReadinessStore(store,broken).check(game,early)
    assert not result['ready']and 'source_collection_failed'in result['reasons']
    assert result['snapshot']['source_error_field']=='espn_team_id'
    assert result['snapshot']['source_error_frames'][-1]['function']=='broken'
