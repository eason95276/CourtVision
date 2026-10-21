import json
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from v2.pregame_system.prediction_versions import PredictionVersionStore
from . import writer_retry

@pytest.mark.parametrize('succeed',[True,False])
def test_retry_survives_restart_and_bounds_calls(tmp_path,monkeypatch,succeed):
    store=PredictionVersionStore(tmp_path/'db.sqlite')
    at=[datetime(2026,10,9,tzinfo=timezone.utc)]
    post={'version_id':'non-lal-postgame','game_id':'espn:401902644'}
    artifact=[None];calls=[]
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda *args,**kwargs:artifact[0])
    def generate(*args,**kwargs):
        calls.append(kwargs['postgame_version_id'])
        if len(calls)==3 and succeed:
            artifact[0]={'artifact_sha256':'a'*64}
            return SimpleNamespace(status='success')
        return SimpleNamespace(status='failed',error_type='TemporaryFailure')
    def attempt():return writer_retry.attempt_postgame_writer(store,post,generate,clock=lambda:at[0],output_root=tmp_path)
    assert attempt()['editorial_status']=='failed'
    assert attempt()['editorial_status']=='waiting'
    at[0]+=timedelta(minutes=5)
    store=PredictionVersionStore(tmp_path/'db.sqlite')
    assert attempt()['editorial_attempt']==2
    at[0]+=timedelta(minutes=9)
    assert attempt()['editorial_status']=='waiting'
    at[0]+=timedelta(minutes=1)
    assert attempt()['editorial_status']==('success' if succeed else 'failed')
    at[0]+=timedelta(days=3)
    assert attempt()['editorial_status']==('already_exists' if succeed else 'exhausted')
    assert len(calls)==3

def test_returned_success_without_artifact_is_not_success(tmp_path,monkeypatch):
    store=PredictionVersionStore(tmp_path/'db.sqlite')
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda *args,**kwargs:None)
    result=writer_retry.attempt_postgame_writer(store,{'version_id':'test','game_id':'espn:test'},
        lambda *args,**kwargs:SimpleNamespace(status='success'),output_root=tmp_path,
        clock=lambda:datetime(2026,10,21,0,tzinfo=timezone.utc))
    assert result['editorial_status']=='failed'
    assert 'without_verified_artifact' in result['editorial_error_type']


def test_peak_defers_article_without_consuming_attempt_or_blocking_record(tmp_path,monkeypatch):
    store=PredictionVersionStore(tmp_path/'db.sqlite')
    at=[datetime(2026,10,21,2,tzinfo=timezone.utc)]  # Wednesday 10:00 Beijing
    post={'version_id':'postgame-peak','game_id':'espn:123'}
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda *args,**kwargs:None)
    calls=[]
    def generate(*args,**kwargs):
        calls.append(1);return SimpleNamespace(status='failed',error_type='temporary')
    result=writer_retry.attempt_postgame_writer(store,post,generate,clock=lambda:at[0],output_root=tmp_path)
    assert result['editorial_status']=='waiting_off_peak' and calls==[]
    with store.connect() as db:assert db.execute('SELECT attempt_count FROM postgame_writer_attempts').fetchone()[0]==0
    at[0]=datetime(2026,10,21,4,tzinfo=timezone.utc)  # 12:00 Beijing
    result=writer_retry.attempt_postgame_writer(store,post,generate,clock=lambda:at[0],output_root=tmp_path)
    assert calls==[1] and result['editorial_attempt']==1

@pytest.mark.parametrize('winner_correct,spread,total,expected',[
    (True,'loss','win','胜负看对，让分看错，大小分看对。'),
    (False,'win','loss','胜负看错，让分看对，大小分看错。'),
    (True,'push','push','胜负看对，让分走盘，大小分走盘。'),
    (False,None,None,'胜负看错。')])
def test_dynamic_settlement_summary(winner_correct,spread,total,expected):
    from .editorial import settlement_summary
    ctx={'prediction':{'winner_correct':winner_correct},'public_market_reference_review':{
        'status':'available','spread':{'settlement':spread},'total':{'settlement':total}}}
    assert settlement_summary(ctx)==expected

@pytest.mark.parametrize('home_spread,margin,side,line',[(-4.5,3,'away',4.5),(-5,8,'home',-5),(6,-9,'away',-6),(6,2,'home',6)])
def test_market_side_line_keeps_true_signed_spread(home_spread,margin,side,line):
    from .editorial import _public_market_reference_review
    quote={'home_spread':home_spread,'away_spread':-home_spread,'total':220.5}
    result=_public_market_reference_review(quote,{'expected_home_margin':margin,'expected_total':222},
        {'actual_home_margin':5,'actual_total':220})
    assert result['spread']['lean_side']==side and result['spread']['lean_line']==line
