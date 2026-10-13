"""Focused runtime closure tests. No network, model training or original-asset writes."""
import json
import hashlib
import zipfile
import pytest

from .bootstrap_demo import bootstrap,BUNDLE,MANIFEST
from .runtime_assets import RuntimeAssetError,SETUP_COMMAND
from .prediction import Predictor
from .agent import PregameAgent
from .residual_model import ResidualPredictor,optional_residual,UnavailableResidual
from .official_final import validate_report,OfficialFinalizer


@pytest.fixture
def runtime(tmp_path):
    return bootstrap(tmp_path/'runtime')


def ask(root):
    agent=PregameAgent(predictor=Predictor(runtime_root=root))
    return agent,agent.ask('demo',plan={'intent':'report','game_id':agent.schedule[0]['game_id']})[0]


def test_restored_bundle_runs_real_models_and_all_layers(runtime):
    agent,report=ask(runtime)
    assert report['snapshot']['demo_runtime']
    assert report['pure_prediction'] and report['market_benchmark'] and report['residual_prediction']
    assert report['prediction']==report['official_numeric_prediction']
    assert report['prediction_method']=='market_residual_v1'
    assert report['analyst_judgment'] is None
    assert agent.predictor.experiment is None
    with pytest.raises(ValueError,match='offline_demo_cannot_be_official'):
        OfficialFinalizer(agent).finalize(agent.schedule[0]['game_id'])


def test_missing_residual_keeps_market_and_pure(runtime):
    (runtime/'residual/margin_residual.joblib').unlink()
    agent,report=ask(runtime)
    assert isinstance(agent.residual_predictor,UnavailableResidual)
    assert report['prediction']==report['pure_prediction']
    assert report['market_benchmark']
    assert report['residual_prediction'] is None and report['official_market_adjustment'] is None
    assert report['market_informed_prediction'] is None
    assert report['prediction_layers']['data_completeness']['residual_model'] is False
    assert any(item['section']=='residual_model' for item in report['missing_data'])
    # Production validation accepts this capability downgrade, provided this is
    # a real production report. The offline demo itself remains unpublishable.
    report['snapshot'].pop('demo_runtime')
    validate_report(report,agent.schedule[0],report['snapshot']['generated_at'])


def test_missing_pure_gives_bootstrap_command(runtime):
    (runtime/'pure/win_probability.joblib').unlink()
    with pytest.raises(RuntimeAssetError,match='bootstrap_demo') as exc:
        Predictor(runtime_root=runtime)
    assert SETUP_COMMAND in str(exc.value)


def test_missing_entire_runtime_gives_bootstrap_command(tmp_path):
    with pytest.raises(RuntimeAssetError,match='bootstrap_demo'):
        Predictor(runtime_root=tmp_path/'missing')


def test_bad_runtime_context_hash_rejected(runtime):
    path=runtime/'context.json'
    path.write_text('{}',encoding='utf8')
    with pytest.raises(RuntimeAssetError,match='context.json'):
        Predictor(runtime_root=runtime)


@pytest.mark.parametrize('change',['version','bundle','file','path'])
def test_bootstrap_validates_before_writing(tmp_path,change):
    manifest=json.loads(MANIFEST.read_text())
    bundle=BUNDLE
    if change=='version':manifest['version']='wrong'
    elif change=='bundle':manifest['bundle_sha256']='0'*64
    elif change=='file':manifest['files']['context.json']['sha256']='0'*64
    else:
        bundle=tmp_path/'unsafe.zip'
        with zipfile.ZipFile(bundle,'w') as archive:archive.writestr('../outside','bad')
        manifest['bundle_sha256']=hashlib.sha256(bundle.read_bytes()).hexdigest()
        manifest['files']={'../outside':{'size':3,'sha256':hashlib.sha256(b'bad').hexdigest()}}
    source=tmp_path/'manifest.json';source.write_text(json.dumps(manifest))
    destination=tmp_path/'restore'
    with pytest.raises(ValueError):bootstrap(destination,bundle,source)
    assert not destination.exists()


def test_bootstrap_idempotent_and_restores_missing_file(runtime):
    path=runtime/'pure/expected_total.joblib'
    before=path.read_bytes();stamp=path.stat().st_mtime_ns
    bootstrap(runtime)
    assert path.stat().st_mtime_ns==stamp
    path.unlink()
    bootstrap(runtime)
    assert path.read_bytes()==before


def test_optional_factory_does_not_mask_missing_pure_error(tmp_path):
    residual=optional_residual(tmp_path/'missing.json')
    assert isinstance(residual,UnavailableResidual)


def test_residual_metadata_missing_and_corrupt_downgrade(runtime):
    path=runtime/'residual/current.json'
    path.write_text('{}')
    assert isinstance(optional_residual(path),UnavailableResidual)
    path.unlink()
    assert isinstance(optional_residual(path),UnavailableResidual)


def test_frozen_features_not_usable_for_another_game(runtime):
    predictor=Predictor(runtime_root=runtime)
    game=dict(predictor.demo_context['game'],game_id='espn:other')
    with pytest.raises(ValueError,match='demo_snapshot_scope_mismatch'):
        predictor.prepare_features(game,predictor.demo_context['cutoff'])


def test_finalizer_base_snapshot_fallback_with_residual_unavailable(runtime,tmp_path,monkeypatch):
    import copy
    from types import SimpleNamespace
    (runtime/'residual/margin_residual.joblib').unlink()
    agent,report=ask(runtime)
    report['snapshot'].pop('demo_runtime')
    game=agent.schedule[0]
    def fail(*args,**kwargs):raise TimeoutError('isolated test')
    fake=SimpleNamespace(schedule=[game],ask=fail,market=agent.market,
                         residual_predictor=agent.residual_predictor)
    monkeypatch.setitem(OfficialFinalizer.finalize.__globals__,'last_legal_snapshot',
                        lambda *args,**kwargs:copy.deepcopy(report))
    record=OfficialFinalizer(fake,root=tmp_path/'official',
        clock=lambda:'2026-10-20T18:55:00Z').finalize(game['game_id'])
    assert record['simulated_clock'] and record['fallback_used']
    assert record['public_prediction']==record['pure_prediction']
    assert record['market_benchmark'] and record['market_informed_prediction'] is None
