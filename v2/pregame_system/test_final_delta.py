import copy,json
from datetime import timedelta
import pytest
from .final_delta import construct_delta,market_delta,run_incremental
from .test_final_readiness import fixture
from .test_analyst import LAYERS,EVIDENCE
from .analyst import build_analyst_context,EvidenceBoundAnalyst
from .storage import content_hash

def early_fixture():
    game,early,snapshot,at=fixture();official=copy.deepcopy(LAYERS['official_numeric_prediction']);game['relationship']={'final':official}
    early['input_snapshot']=copy.deepcopy(game)
    context=build_analyst_context(LAYERS,EVIDENCE,missing_data=[],game={'game_id':game['id'],'home':game['home'],'away':game['away'],'scheduled_tipoff':game['tipoff'],'cutoff':(at-timedelta(days=7)).isoformat()},analysis_request='第25秒赛前分析')
    context.roster_context={'game_id':game['id'],'cutoff':context.game['cutoff'],'teams':{s:{'official_team_roster':{'status':'available','players':copy.deepcopy(r['players'])}}for s,r in snapshot['rosters'].items()}}
    early.update(analyst_context=context.model_dump(),context_hash=context.context_hash,
        game_identity=snapshot['game_identity'],official_numeric_prediction=official,model_layers=copy.deepcopy(LAYERS),
        public_market_reference=copy.deepcopy(snapshot['market_collection']['public_market_reference']),sources=[],payload_sha256='a'*64,
        judgment={'final_prediction':{'winner_side':'home','win_probability':.55,'expected_margin':3.0,'expected_total':223.5,'evidence_ids':[]},'core_question':'半场持球对位','subjective_rationale':'主要观察双方半场持球与协防的相互作用。','analysis_article_evidence_ids':[]})
    # The unchanged fixture has an explicit EARLY status baseline. A newly
    # confirmed status is tested separately, not silently equated to no delta.
    previous=copy.deepcopy(snapshot['availability']['records'])
    for row in previous:row['observed_at']=context.game['cutoff']
    early['analyst_context']['objective_evidence'].append({'id':'early-availability','section':'availability','kind':'Fact',
        'value':{'records':previous},'cutoff':context.game['cutoff']})
    early['analyst_context']['evidence_index']['early-availability']={'section':'availability','source_type':'Fact'}
    plan=early['editorial_material'][0]['pregame_plan'];plan.update(player_usage=[],key_matchup='半场持球对位',uncertainty=[])
    for r in plan['lineups']:r.update(basis='agent_expected',evidence_ids=['fixture-roster'],rationale='测试中的预计首发')
    return game,early,snapshot,at

def writer(messages,schema):
    text=('本场我更关注两队怎样回应彼此的持球选择。进攻方需要在突破和分球之间作出判断，防守方也要权衡上提与回收。'
        '如果协防提前移动，弱侧接应便可能成为下一步选择；如果防守维持原位，持球者就需要寻找直接创造机会的办法。'
        '这些是基于给定材料的篮球判断，并不是对已经发生比赛过程的描述。')*4
    return {'title':'持球与协防的相互作用','lead':'沿用原判断，观察临场确认条件下的持球对位。','article':text,'watch_themes':[],
            'confidence':'medium','player_analysis':[]},{'usage':{}}

def test_ordinary_half_point_move_is_not_significant():
    r=market_delta({'home_spread':-3.0,'total':220},{'home_spread':-3.5,'total':220})
    assert not r['significant_move']and not r['numeric_threshold_used']

def test_direction_and_basketball_link_are_material():
    assert market_delta({'home_spread':-3.0},{'home_spread':1.5})['significant_move']
    assert market_delta({'home_spread':-3.0},{'home_spread':-3.5},True)['significant_move']

def test_first_market_requires_review_without_fake_volatility():
    r=market_delta({}, {'home_spread':-3.5,'total':221})
    assert r['review_required']and not r['historical_volatility_established']


def test_market_crossing_baseline_direction_is_review_signal():
    r=market_delta({'home_spread':-2.5,'total':220},{'home_spread':-3.5,'total':224},baseline={'expected_home_margin':3.0,'expected_total':223.5})
    assert r['significant_move']and r['reason']=='reference_direction_crossed'


def test_unverified_or_duplicate_books_not_consensus():
    move={'sportsbook':'one','direction':'home','source_verified':True,'temporal_verified':True}
    assert not market_delta({'home_spread':-3},{'home_spread':-3.5,'qualified_book_movements':[move,move]})['significant_move']
    other={**move,'sportsbook':'two','source_verified':False}
    assert not market_delta({'home_spread':-3},{'home_spread':-3.5,'qualified_book_movements':[move,other]})['significant_move']

def test_confirming_same_lineup_not_major_change():
    game,early,snapshot,at=early_fixture();r=construct_delta(early,snapshot)
    assert not r['material_change']and all(x['new_confirmation']for x in r['lineups'])


def test_first_key_availability_confirmation_is_assessed_not_auto_ignored():
    game,early,snapshot,at=early_fixture()
    early['analyst_context']['objective_evidence']=[r for r in early['analyst_context']['objective_evidence']if r['section']!='availability']
    delta=construct_delta(early,snapshot)
    assert delta['new_key_availability_confirmation']and delta['requires_change_assessment']
    assert not delta['material_basketball_change'] # Confirmation is not proof that numbers must change.


def test_first_confirmation_can_assess_and_retain_all_numbers(tmp_path):
    from .final_delta import numbers
    game,early,snapshot,at=early_fixture();calls=[]
    early['analyst_context']['objective_evidence']=[r for r in early['analyst_context']['objective_evidence']if r['section']!='availability']
    def assess(messages,schema):
        value=json.loads(messages[1]['content']);calls.append(value)
        return {'prediction':numbers(early),'rationale':'确认出战与原来的可用阵容预期一致，保留数字。',
            'evidence_ids':[value['allowed_evidence'][0]['id']]},{}
    analyst=EvidenceBoundAnalyst(root=tmp_path/'writer',client=writer,forecast_client=assess,max_calls=4)
    result=run_incremental(early,{'ready':True,'snapshot':snapshot,'checked_at':at.isoformat()},analyst,tmp_path/'final',client=assess)
    assert len(calls)==1 and result['change_assessment']['decision']=='retain'
    for field in ('winner_side','win_probability','expected_margin','expected_total'):
        assert result['judgment']['final_prediction'][field]==early['judgment']['final_prediction'][field]

def test_injury_change_or_different_starter_can_reassess():
    game,early,snapshot,at=early_fixture();snapshot['availability']['records'][0]['status']='Out'
    assert construct_delta(early,snapshot)['material_basketball_change']
    snapshot['lineups']['home']['players'][-1]='Verified Reserve'
    assert construct_delta(early,snapshot)['material_basketball_change']

def test_no_material_change_does_not_run_forecast_or_research(tmp_path):
    game,early,snapshot,at=early_fixture();original=content_hash(early)
    def forbidden(*args):raise AssertionError('Unchanged FINAL must not call a forecast/Research')
    analyst=EvidenceBoundAnalyst(root=tmp_path/'analyst',client=writer,forecast_client=forbidden,include_forecasting_skill=True,max_calls=4)
    result=run_incremental(early,{'ready':True,'snapshot':snapshot,'checked_at':at.isoformat()},analyst,tmp_path/'final',client=forbidden)
    assert result['judgment']['final_prediction']['expected_margin']==3.0
    assert result['judgment']['final_prediction']['win_probability']==.55
    assert result['official_numeric_prediction']==early['official_numeric_prediction']
    assert content_hash(early)==original and result['search']['llm_research_calls']==0
    assert result['change_assessment']['model_called']is False
    assert all(x['pregame_plan']['lineups'][0]['basis']=='confirmed'for x in result['editorial_material']if x.get('pregame_plan'))

def test_not_ready_stops_before_any_model(tmp_path):
    game,early,snapshot,at=early_fixture()
    with pytest.raises(Exception,match='final_not_ready'):
        run_incremental(early,{'ready':False},None,tmp_path/'final')


def test_material_lineup_update_uses_delta_not_full_research(tmp_path):
    game,early,snapshot,at=early_fixture();snapshot=copy.deepcopy(snapshot);calls=[]
    snapshot['lineups']['home']['players'][-1]='Verified Reserve'
    snapshot['rosters']['home']['players'].append({'name':'Verified Reserve'})
    snapshot['availability']['records'].append({'side':'home','player_name':'Verified Reserve','status':'Available',
        'source_url':'https://site.api.espn.com/game','observed_at':at.isoformat()})
    def assess(messages,schema):
        payload=json.loads(messages[1]['content']);calls.append(payload)
        assert 'market'not in payload and 'basketball_delta'in payload
        ref=payload['allowed_evidence'][0]['id']
        return {'prediction':{'winner_side':'home','win_probability':.6,'expected_home_margin':4.0,'expected_total':223.5},
            'rationale':'确认阵容变化后仅修正受影响的判断。','evidence_ids':[ref]},{}
    analyst=EvidenceBoundAnalyst(root=tmp_path/'writer',client=writer,forecast_client=assess,max_calls=4)
    result=run_incremental(early,{'ready':True,'snapshot':snapshot,'checked_at':at.isoformat()},analyst,tmp_path/'final',client=assess)
    assert len(calls)==1 and result['change_assessment']['decision']=='update'
    assert result['judgment']['final_prediction']['expected_margin']==4.0
    assert result['official_numeric_prediction']==early['official_numeric_prediction']
    assert result['search']['llm_research_calls']==0


def test_future_or_unqualified_minutes_do_not_enter_delta():
    game,early,snapshot,at=early_fixture()
    row={'player_name':'home Player 0','claim_type':'minutes_restriction','minutes_max':24,
        'eligible_for_fact':True,'source_role':'primary','source_url':'https://official.nba.com/report',
        'published_at':(at+timedelta(minutes=1)).isoformat()}
    snapshot['minutes_restrictions']=[row]
    assert construct_delta(early,snapshot)['minutes_restrictions']==[]
    row['published_at']=at.isoformat()
    assert len(construct_delta(early,snapshot)['minutes_restrictions'])==1


def test_final_confirmation_survives_writer_projection_and_ids_not_repurposed():
    from .final_delta import compact_context
    from .editorial_contract import build_editorial_projection
    game,early,snapshot,at=early_fixture()
    original={'id':'frozen-roster','section':'roster_context','kind':'Fact','value':copy.deepcopy(early['analyst_context']['roster_context'])}
    early['analyst_context']['objective_evidence'].append(original)
    early['analyst_context']['evidence_index']['frozen-roster']={'section':'roster_context','source_type':'Fact'}
    old={r['id']:copy.deepcopy(r)for r in early['analyst_context']['objective_evidence']if r['section']=='roster_context'}
    assert old
    c,g,plan,sources=compact_context(early,snapshot,at.isoformat())
    projection=build_editorial_projection(c,early['judgment']['final_prediction'],[])
    live=[r for r in projection['facts']if r['kind']=='LIVE'and r['game_specific']]
    assert len(live)>=12
    current=[r for r in projection['facts']if r['kind']=='CURRENT'and r['value'].get('roster_snapshot')]
    assert {r['value']['team_id']for r in current}=={game['home']['id'],game['away']['id']}
    for ident,item in old.items():
        baseline=next(r for r in c.objective_evidence if r['id']==ident)
        assert baseline['value']==item['value']and baseline['section']=='early_assumption'
    assert any(r['id'].startswith('final-roster-')for r in c.objective_evidence)
