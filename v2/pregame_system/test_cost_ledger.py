from datetime import datetime,timezone
from decimal import Decimal
import json,pytest
from .cost_ledger import calculate,next_off_peak,normalize_usage,production_scope,component,record_call,CostLedger,metered_deepseek

def test_actual_cached_usage_and_alias():
    row=calculate('deepseek-v4-flash',{'prompt_tokens':1000,'prompt_cache_hit_tokens':800,'prompt_cache_miss_tokens':200,'completion_tokens':100},'2026-10-14T12:00:00+00:00')
    assert row['billing_model']=='deepseek-flash' and Decimal(row['calculated_cost'])==Decimal('.000616')
    assert Decimal(row['cache_savings'])==Decimal('.000784')

def test_peak_rates_and_holiday():
    usage={'input_tokens':1000,'output_tokens':100,'input_token_details':{'cache_read':0}}
    assert Decimal(calculate('deepseek-flash',usage,'2026-10-14T02:00:00Z')['calculated_cost'])==Decimal('.0028')
    assert calculate('deepseek-flash',usage,'2026-10-01T02:00:00Z',['2026-10-01'])['price_tier']=='off_peak'

def test_provider_pro_not_judge_inferred():
    row=calculate('deepseek-v4-pro',{'prompt_tokens':100,'prompt_cache_hit_tokens':0,'completion_tokens':100},'2026-10-14T12:00:00Z')
    assert row['unit_price']['output']=='13.5'

def test_cache_unknown_not_free_or_fabricated():
    row=calculate('deepseek-flash',{'prompt_tokens':100,'completion_tokens':20},'2026-10-14T12:00:00Z')
    assert row['calculated_cost']is None and row['cost_status']=='cache_breakdown_unknown'
    with pytest.raises(ValueError):normalize_usage({'prompt_tokens':10,'prompt_cache_hit_tokens':11})

def test_off_peak_planning_deadline_wins():
    assert next_off_peak('2026-10-14T02:00:00Z','2026-10-15T02:00:00Z').hour==4
    assert next_off_peak('2026-10-14T02:00:00Z','2026-10-14T02:10:00Z').hour==2

def test_no_evaluation_scope_no_ledger(tmp_path):
    assert record_call(provider='DeepSeek',model='deepseek-flash')is None
    assert not list(tmp_path.iterdir())

def test_early_final_repair_and_failures(tmp_path):
    path=tmp_path/'cost.sqlite';usage={'prompt_tokens':1000,'prompt_cache_hit_tokens':0,'completion_tokens':100}
    for stage in ['EARLY_PREVIEW','FINAL_PREGAME']:
        with production_scope(path,'espn:1',stage,'run'):
            with component('Forecast'):record_call(provider='DeepSeek',model='deepseek-flash',usage=usage,at='2026-10-14T12:00:00Z')
            with component('Repair',repair=True):record_call(provider='DeepSeek',model='deepseek-flash',usage=usage,at='2026-10-14T12:00:00Z',success=False)
    report=CostLedger(path).summary('espn:1')['espn:1']
    assert set(report)=={'EARLY_PREVIEW','FINAL_PREGAME'}
    assert report['EARLY_PREVIEW']['calls']==2
    assert Decimal(report['EARLY_PREVIEW']['repair_retry_cost_cny'])==Decimal('.0014')

def test_transport_preserves_bytes_and_captures_usage(tmp_path):
    class Fake:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self):return json.dumps({'model':'deepseek-flash','usage':{'prompt_tokens':100,'prompt_cache_hit_tokens':50,'completion_tokens':20},'choices':[{'message':{'content':'unchanged'}}]}).encode()
    def invoke(opener):
        with opener(None,timeout=1)as r:return r.read()
    with production_scope(tmp_path/'cost.sqlite','espn:1','EARLY_PREVIEW','r'):
        original=metered_deepseek(invoke,'deepseek-v4-flash',lambda *a,**k:Fake())
    assert json.loads(original)['choices'][0]['message']['content']=='unchanged'
    rows=CostLedger(tmp_path/'cost.sqlite').rows()
    assert len(rows)==1 and rows[0]['cached_input_tokens']==50


def test_search_fee_unknown_until_actual_free_plan_receipt(tmp_path):
    ledger=CostLedger(tmp_path/'cost.sqlite')
    with production_scope(ledger.path,'espn:1','EARLY_PREVIEW','r'):
        record_call(provider='Tavily',model=None,usage=None,at='2026-10-14T12:00:00Z')
    assert ledger.rows()[0]['calculated_cost']is None
    ledger.save_search_account({'current_plan':'Researcher','plan_usage':500,'plan_limit':1000,'paygo_usage':0},'2026-10-14T13:00:00Z')
    assert ledger.rows()[0]['calculated_cost']=='0'
    assert ledger.rows()[0]['cost_status']=='verified_free_plan_no_paygo'


def test_paid_or_wrong_month_account_not_free(tmp_path):
    ledger=CostLedger(tmp_path/'cost.sqlite')
    with production_scope(ledger.path,'espn:1','EARLY_PREVIEW','r'):
        record_call(provider='Tavily',model=None,at='2026-10-14T12:00:00Z')
    ledger.save_search_account({'current_plan':'Researcher','plan_usage':1000,'plan_limit':1000,'paygo_usage':1},'2026-10-14T13:00:00Z')
    ledger.save_search_account({'current_plan':'Researcher','plan_usage':0,'plan_limit':1000,'paygo_usage':0},'2026-11-01T13:00:00Z')
    assert ledger.rows()[0]['calculated_cost']is None
