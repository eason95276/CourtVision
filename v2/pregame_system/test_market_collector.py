import copy
import json
from pathlib import Path
import pytest
from .market_collector import MarketCollector,normalize_book_market
from .forecasting_workflow import run_two_stage,IndependentForecast
from .test_forecasting_workflow import packet,prepare_pilot,output


EVENT={'game_id':'espn:499999900','event_id':'499999900','home_team_id':1610612761,'away_team_id':1610612748,
    'home_espn_id':'28','away_espn_id':'14','scheduled_tipoff':'2026-10-04T23:00:00Z'}
RECEIPT={'status':'success','captured_at':'2026-10-04T22:55:01Z','url':'https://sports.core.api.espn.com/odds'}


def odds():
    return {'items':[{'provider':{'id':'100','name':'Draft Kings'},'current':{'total':{'american':'227.5'}},
        'homeTeamOdds':{'team':{'$ref':'https://sports.core.api.espn.com/teams/28'},'current':{'pointSpread':{'american':'+2.5'},'moneyLine':{'american':'+120'}}},
        'awayTeamOdds':{'team':{'$ref':'https://sports.core.api.espn.com/teams/14'},'current':{'pointSpread':{'american':'-2.5'},'moneyLine':{'american':'-140'}}}}]}


def test_explicit_team_handicap_sign_and_all_market_types():
    q=normalize_book_market(odds(),EVENT,RECEIPT,'2026-10-04T22:55:02Z')
    assert q['home_spread']==2.5 and q['expected_home_margin']==-2.5
    assert q['away_spread']==-2.5 and q['total']==227.5 and q['home_moneyline']==120
    assert q['published_at']is None and q['observed_at']==RECEIPT['captured_at']


@pytest.mark.parametrize('bad',['provider','team','after_cutoff','post_tipoff'])
def test_qualifications_fail_closed(bad):
    data=odds();receipt=copy.deepcopy(RECEIPT);cutoff='2026-10-04T22:55:02Z'
    if bad=='provider':data['items'][0]['provider']={'id':'100','name':'accuscore'}
    if bad=='team':data['items'][0]['homeTeamOdds']['team']['$ref']='https://sports.core.api.espn.com/teams/20'
    if bad=='after_cutoff':receipt['captured_at']='2026-10-04T22:56:00Z'
    if bad=='post_tipoff':cutoff='2026-10-04T23:00:00Z'
    assert normalize_book_market(data,EVENT,receipt,cutoff)is None


def test_ambiguous_root_spread_is_not_a_home_handicap_but_total_survives():
    data=odds();item=data['items'][0];item['spread']=5.5
    item['homeTeamOdds']['current'].pop('pointSpread');item['awayTeamOdds']['current'].pop('pointSpread')
    q=normalize_book_market(data,EVENT,RECEIPT,'2026-10-04T22:55:02Z')
    assert q['home_spread']is None and q['total']==227.5


def test_collector_cannot_fetch_ended_heat_or_modify_its_history(tmp_path):
    def forbidden(*args):pytest.fail('no post-tipoff lookup')
    collector=MarketCollector(fetcher=forbidden,clock=lambda:'2026-10-04T01:00:00Z',root=tmp_path)
    game={'game_id':'espn:401902644','scheduled_tipoff':'2026-10-03T23:00:00Z','home':{'id':1610612761},'away':{'id':1610612748}}
    result=collector.collect(game)
    assert result['status']=='unavailable' and result['reason']=='pregame_market_window_closed'


@pytest.mark.parametrize('available',[True,False])
def test_fixed_market_step_runs_only_after_durable_independent_and_before_review(tmp_path,available):
    basketball,market,_=prepare_pilot(packet());market={'evidence':[]};order=[];root=tmp_path/'run'
    def client(messages,schema):
        if schema is IndependentForecast:
            order.append('independent');assert 'market_context'not in messages[1]['content'];return output(),{}
        order.append('review');return output(refs=['model.pure','fixed.market'],review=True),{}
    def collect(game):
        order.append('collector');assert (root/'independent.json').exists()
        if not available:raise RuntimeError('offline_market')
        return {'status':'available','finished_at':'2026-01-01T00:00:00Z','sources':[],
            'evidence':[{'id':'fixed.market','section':'market','value':{'total':228.5}}]}
    result=run_two_stage(basketball,market,root,client=client,skill_loader=lambda:'FORECAST',market_collector=collect)
    assert order==(['independent','collector','review']if available else ['independent','collector'])
    assert result['final_prediction']==result['independent']['prediction']
    assert result['market_collection']['status']==('available'if available else'unavailable')


def test_real_collector_adapter_normalizes_complete_structured_fixture(tmp_path):
    g={'game_id':EVENT['game_id'],'home':{'id':EVENT['home_team_id'],'name':'Toronto Raptors'},
        'away':{'id':EVENT['away_team_id'],'name':'Miami Heat'},'scheduled_tipoff':EVENT['scheduled_tipoff']}
    summary={'header':{'id':EVENT['event_id'],'competitions':[{'id':EVENT['event_id'],'date':EVENT['scheduled_tipoff'],
        'status':{'type':{'state':'pre'}},'competitors':[{'homeAway':'home','team':{'id':'28','abbreviation':'TOR','displayName':'Toronto Raptors'}},
        {'homeAway':'away','team':{'id':'14','abbreviation':'MIA','displayName':'Miami Heat'}}]}]}}
    calls=[]
    def fetch(url):
        calls.append(url);return (summary if 'summary?'in url else odds()),{**RECEIPT,'url':url}
    result=MarketCollector(fetcher=fetch,clock=lambda:'2026-10-04T22:55:02Z',root=tmp_path).collect(g)
    assert len(calls)==2 and result['status']=='available'
    assert {r['market_type']for r in result['records']}=={'spread','total','moneyline'}
    assert result['public_market_reference']['expected_home_margin']==-2.5
    assert len(result['evidence'])==1 and result['evidence'][0]['game_id']==g['game_id']


def test_search_snippet_is_never_upgraded_when_full_body_fetch_fails(tmp_path):
    from types import SimpleNamespace
    g={'game_id':EVENT['game_id'],'home':{'id':EVENT['home_team_id'],'name':'Toronto Raptors'},
        'away':{'id':EVENT['away_team_id'],'name':'Miami Heat'},'scheduled_tipoff':EVENT['scheduled_tipoff']}
    url='https://www.fanduel.com/research/heat-raptors'
    search=SimpleNamespace(search=lambda *a,**kw:{'records':[{'url':url,'title':'NBA Odds','snippet':'Heat -2.5 Raptors +2.5 total227.5'}]},
        fetch_bodies=lambda *a,**kw:[{'url':url,'content':''}])
    result=MarketCollector(searcher=search,fetcher=lambda url:(None,{'status':'unavailable'}),
        clock=lambda:'2026-10-04T22:55:02Z',root=tmp_path).collect(g)
    assert result['status']=='unavailable' and result['evidence']==[]


def test_complete_page_quote_with_unknown_publication_uses_observation_not_fake_publication(tmp_path):
    from types import SimpleNamespace
    g={'game_id':EVENT['game_id'],'home':{'id':EVENT['home_team_id'],'name':'Toronto Raptors'},
        'away':{'id':EVENT['away_team_id'],'name':'Miami Heat'},'scheduled_tipoff':EVENT['scheduled_tipoff']}
    url='https://www.espn.com/nba/odds/heat-raptors'
    title='Miami Heat vs Toronto Raptors NBA Odds 2026-10-04'
    search=SimpleNamespace(search=lambda *a,**kw:{'records':[{'url':url,'title':title}]},
        fetch_bodies=lambda *a,**kw:[{'url':url,'captured_at':'2026-10-04T22:55:01Z','published_at':None,
            'content':'Miami Heat +2.5\nToronto Raptors -2.5\nTotal: 227.5\n2026-10-04\nCurrent NBA game matchup odds reference.'}])
    result=MarketCollector(searcher=search,fetcher=lambda url:(None,{'status':'unavailable'}),
        clock=lambda:'2026-10-04T22:55:02Z',root=tmp_path).collect(g)
    assert result['status']=='available'
    q=result['public_market_reference'];assert q['expected_home_margin']==2.5
    assert q['published_at']is None and q['observed_at']=='2026-10-04T22:55:01Z'
    assert q['publication_precision']=='unknown' and q['latest_quote_verified']is False


def test_collected_market_reaches_writer_and_same_publication_receipt(tmp_path,monkeypatch):
    from .analyst import EvidenceBoundAnalyst
    from .test_pregame_estimates import inputs,forecast
    from .editorial_contract import verify_publication_receipt
    from .editorial_pipeline import EditorialDraft
    context=inputs();pure=copy.deepcopy(context.pure_prediction);official=copy.deepcopy(context.official_numeric_prediction)
    order=[]
    def forecasting(messages,schema):
        value=forecast()
        if schema is IndependentForecast:
            order.append('independent');assert 'market_context'not in messages[1]['content']
        else:
            order.append('review');value.update(decision='keep',market_assessment='challenges',rationale='Basketball evidence supports the independent direction.')
            value['factors'][0]['evidence_ids'].append('fixed-market')
        return value,{}
    def writer(messages,schema):
        order.append('writer');assert schema is EditorialDraft
        return {'title':'前场组合','lead':'我更看好客队。','article':'我预计轮换会改变比赛节奏，倾向客队。',
            'watch_themes':[{'title':'球权','statement':'观察持球分工。'},{'title':'轮换','statement':'观察替补竞争。'}],'confidence':'low'},{}
    analyst=EvidenceBoundAnalyst(root=tmp_path,client=writer,forecast_client=forecasting,include_forecasting_skill=True,max_calls=6)
    def collect(game):
        order.append('collector')
        return {'status':'available','finished_at':'2026-10-03T16:01:00Z','sources':[],
            'evidence':[{'id':'fixed-market','section':'web_context','kind':'MarketSnapshot','game_id':game['game_id'],
                'value':{'category':'market','claim_type':'market_reference','market_reference':{'total':235.5,'home_spread':-1.5}}}]}
    analyst.market_collector=collect
    import sys
    monkeypatch.setattr(sys.modules[EvidenceBoundAnalyst.__module__],'now',lambda:'2026-10-03T16:02:00Z')
    result,trace=analyst.judge(context)
    assert order==['independent','collector','review','writer']
    assert context.game['cutoff']=='2026-10-03T16:02:00Z' and 'fixed-market'in context.evidence_index
    assert context.pure_prediction==pure and context.official_numeric_prediction==official
    verify_publication_receipt({'game_id':context.game['game_id'],'judgment':result.model_dump(),
        'prediction':result.final_prediction.model_dump(),'analyst_context':context.model_dump(),
        'editorial_projection':trace['editorial_projection'],'editorial_material':trace['editorial_material'],
        'publication_receipt':trace['publication_receipt'],'quality_review_input_sha256':trace.get('quality_review_input_sha256')})


def test_market_review_failure_retains_valid_independent_in_product_path(tmp_path):
    b,_,_=prepare_pilot(packet())
    def client(messages,schema):
        if schema is IndependentForecast:return output(),{}
        raise TimeoutError('market review timed out')
    collection=lambda _:{'status':'available','evidence':[{'id':'fixed.market','section':'market','value':{'total':228.5}}]}
    result=run_two_stage(b,{'evidence':[]},tmp_path/'run',client=client,skill_loader=lambda:'FORECAST',market_collector=collection)
    assert result['final_prediction']==result['independent']['prediction'] and result['review']['decision']=='unavailable'
    assert (tmp_path/'run'/'market_review_failure.json').exists()


def test_market_timeout_is_unavailable_not_generation_error(tmp_path,monkeypatch):
    from .market_collector import BoundedMarketCollector
    import subprocess
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('market',1)))
    g={'game_id':EVENT['game_id'],'home':{'id':1610612761},'away':{'id':1610612748},'scheduled_tipoff':'2099-10-04T23:00:00Z'}
    result=BoundedMarketCollector().collect(g)
    assert result['status']=='unavailable' and result['reason']=='TimeoutExpired'


def test_official_started_status_is_not_an_ordinary_missing_market_fallback(tmp_path):
    b,_,_=prepare_pilot(packet())
    with pytest.raises(RuntimeError,match='pregame_generation_closed'):
        run_two_stage(b,{'evidence':[]},tmp_path/'run',client=lambda *a:(output(),{}),skill_loader=lambda:'FORECAST',
            market_collector=lambda _:{'status':'unavailable','evidence':[],'observed_game_already_started':True})
    assert (tmp_path/'run'/'independent.json').exists() and not (tmp_path/'run'/'final.json').exists()
