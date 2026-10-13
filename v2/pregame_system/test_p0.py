"""P0 decision-change tests only. No collection, tuning, or real LLM calls."""
import copy
import json
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
import pytest
from .agent import PregameAgent,Session
from .historical_market import HistoricalMarket
from .official_final import OfficialFinalizer,last_legal_snapshot
from .prediction import historical_identity
from .storage import BASE,save,digest,now,instant,content_hash


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a,**kw):raise AssertionError('P0 tests must not invoke external services')
    monkeypatch.setattr('requests.Session.request',blocked)
    monkeypatch.setattr('urllib.request.OpenerDirector.open',blocked)


@pytest.fixture(scope='module')
def agent():return PregameAgent()


@pytest.fixture(scope='module')
def audit(agent):
    # Reuse the saved, genuinely model-produced QueryPlan. Do not call the LLM again.
    response=json.loads((BASE/'outputs/llm_calls/05/response.json').read_text(encoding='utf8'))
    report,session=agent.ask('saved real QueryPlan P0 execution',plan=response['plan'])
    assert report['prediction'] and report['snapshot']['purpose']=='audit_eval'
    return report


def test_real_queryplan_graph_and_single_audit_mode(audit):
    assert audit['game']['game_id']=='espn:401909088'
    assert 'stage' not in audit['snapshot'] and not audit['snapshot']['official_prediction']
    assert any(t.get('langchain_tool')=='prediction' for t in audit['trace'])
    assert any(t.get('node')=='select_evidence' for t in audit['trace'])
    assert audit['prediction_method']=='pure_basketball_fallback'
    assert audit['pure_prediction']==audit['official_numeric_prediction']==audit['prediction']
    assert audit['market_benchmark'] is None and audit['residual_prediction'] is None
    assert audit['analyst_context']['official_numeric_prediction']==audit['prediction']


def test_local_market_actual_sample_and_missing_moneyline(agent):
    g=historical_identity(agent.predictor.games[agent.predictor.games.game_id=='0022500564'].iloc[0].to_dict())
    m=agent.market(g,'2026-01-13T19:00:00-05:00',True);q=m['historical_reference']
    assert q['spread']==1.5 and q['expected_home_margin']==1.5 and q['total']==233
    assert q['home_moneyline'] is None and q['home_probability_no_vig'] is None
    assert m['as_of_cutoff'] is None and not m['exact_quote_time_verified']
    assert not any('score' in k or k.startswith('h2_') for k in q)


def test_home_away_and_moneyline_reuse(agent):
    market=agent.historical_market
    row=market.frame[(market.frame.whos_favored=='away')&market.frame.moneyline_home.notna()&market.frame.moneyline_away.notna()].iloc[-1]
    from courtvision.data import TEAM_ID_TO_ABBR
    ids={v:k for k,v in TEAM_ID_TO_ABBR.items()}
    game={'game_id':'fixture','scheduled_tipoff':row.date.tz_localize('America/New_York').isoformat(),
          'home_team_id':ids[row.home],'away_team_id':ids[row.away]}
    q=market.get(game)['historical_reference']
    assert q['expected_home_margin']==-q['spread']
    assert 0<q['home_probability_no_vig']<1
    game['home_team_id'],game['away_team_id']=game['away_team_id'],game['home_team_id']
    assert market.get(game) is None


def test_market_disagreement_deterministic_and_forecast_untouched(agent):
    # Reuse already fitted replay model if available, avoiding a repeated training run.
    model_paths=sorted((BASE/'outputs/replay_models').glob('*/model.json'))
    for p in reversed(model_paths):
        meta=json.loads(p.read_text(encoding='utf8'))
        if meta.get('training_cutoff')=='2026-01-13T12:00:00-05:00':
            agent.predictor._replays['2026-01-13 00:00:00']=meta;break
    report,_=agent.ask('local market reference',cutoff='2026-01-13T19:00:00-05:00',replay=True,
        plan={'intent':'report','game_id':'0022500564'})
    diff=report['market']['model_market_disagreement']
    assert diff['expected_home_margin_difference']==pytest.approx(report['prediction']['expected_margin']-1.5)
    assert diff['expected_total_difference']==pytest.approx(report['prediction']['expected_total']-233)
    assert diff['home_win_probability_difference'] is None
    assert len(report['availability']['records'])==10
    assert report['snapshot']['generation_context']['availability_adjustment'] is None


def finalizer(tmp_path,audit,*,failure=False,clock_values=None):
    game=audit['game'];at=instant(game['scheduled_tipoff'])-timedelta(minutes=5)
    def ask(*args,**kwargs):
        if failure:raise TimeoutError('test retrieval failure')
        r=copy.deepcopy(audit);r['snapshot']['cutoff']=at.isoformat();r['snapshot']['generated_at']=at.isoformat()
        return r,Session()
    fake=SimpleNamespace(schedule=[game],ask=ask)
    values=iter(clock_values) if clock_values else None
    clock=(lambda:next(values)) if values else (lambda:at.isoformat())
    return OfficialFinalizer(fake,root=tmp_path/'official',clock=clock),at


def test_one_official_result_is_idempotent(tmp_path,audit):
    f,at=finalizer(tmp_path,audit);first=f.finalize(audit['game']['game_id'])
    assert first['status']=='official_pregame_final' and first['simulated_clock']
    assert first['seconds_before_tipoff']==300
    files=list((tmp_path/'official').glob('*.json'));before=digest(files[0])
    f.agent.ask=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('must reuse official result'))
    assert f.finalize(audit['game']['game_id'])==first and digest(files[0])==before


def test_not_due_cannot_create_an_official_result(tmp_path,audit):
    f,at=finalizer(tmp_path,audit);f.clock=lambda:(at-timedelta(seconds=1)).isoformat()
    assert f.finalize(audit['game']['game_id'])['status']=='not_due'
    assert not list((tmp_path/'official').glob('*.json'))


def test_failure_falls_back_to_real_last_success_with_data_times(tmp_path,audit):
    f,_=finalizer(tmp_path,audit,failure=True);r=f.finalize(audit['game']['game_id'])
    assert r['fallback_used'] and r['fetch_failure_type']=='TimeoutError'
    assert r['actual_data_times']['latest_observed_team_game']
    assert r['report']['prediction'] and r['report']['missing_data']


def test_no_legal_data_means_no_official_prediction(tmp_path,audit):
    f,_=finalizer(tmp_path,audit,failure=True);f.snapshots=tmp_path/'empty'
    r=f.finalize(audit['game']['game_id'])
    assert r['status']=='unavailable' and not list((tmp_path/'official').glob('*.json'))


def test_generation_crossing_tipoff_cannot_publish(tmp_path,audit):
    at=instant(audit['game']['scheduled_tipoff'])-timedelta(minutes=5)
    f,_=finalizer(tmp_path,audit,clock_values=[at.isoformat(),at.isoformat(),audit['game']['scheduled_tipoff']])
    with pytest.raises(ValueError,match='crossed_tipoff'):f.finalize(audit['game']['game_id'])
    assert not list((tmp_path/'official').glob('*.json'))


def test_fallback_rejects_tampered_prediction(tmp_path,audit):
    import shutil
    source=BASE/'outputs/snapshots'/audit['snapshot']['snapshot_id'];folder=tmp_path/'snapshots'/'one'
    shutil.copytree(source,folder)
    p=json.loads((folder/'prediction.json').read_text());p['expected_margin']=999
    (folder/'prediction.json').write_text(json.dumps(p))
    assert last_legal_snapshot(audit['game'],now(),tmp_path/'snapshots') is None


def test_public_cli_has_no_product_stage_option():
    text=(BASE/'__main__.py').read_text(encoding='utf8')
    assert "'--stage'" not in text and "'--official-final'" in text


def test_production_reuses_frozen_selection_and_excludes_future(monkeypatch):
    import pandas as pd
    from . import production
    games=pd.DataFrame({'game_id':['past','future'],'game_date':pd.to_datetime(['2020-01-01','2026-10-20'])})
    stats=pd.DataFrame({'game_id':['past','future']});captured={}
    monkeypatch.setattr(production,'now',lambda:'2026-09-13T12:00:00Z')
    monkeypatch.setattr(production,'history',lambda:(games,stats))
    def build(g,s):
        assert list(g.game_id)==list(s.game_id)==['past']
        return g,{'baseline':['x'],'matchup':[],'schedule':[],'cold':[]},[]
    monkeypatch.setattr(production,'dataset',build)
    def train(frame,configs,selection,metadata):
        captured.update(selection=selection,metadata=metadata);return 'isolated_training_stub'
    monkeypatch.setattr(production,'train_production',train)
    assert production.run()=='isolated_training_stub'
    assert set(captured['selection'].values())=={'baseline'}
    assert captured['metadata']['frozen_selection_reference']
