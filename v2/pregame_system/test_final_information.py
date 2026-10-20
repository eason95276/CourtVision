import copy
import json
from datetime import timedelta
from pathlib import Path
import pytest

from .final_readiness import evaluate_readiness
from .final_information import qualify_claims, research_game
from .test_final_readiness import fixture
from .test_final_delta import early_fixture
from .final_delta import compact_context
from .deepseek_web import DeepSeekWebTransport
from .research_evidence import source_role


def source_fixture():
    game,early,snapshot,at=fixture()
    game['home']['name']='Golden State Warriors';game['away']['name']='Los Angeles Lakers'
    names=snapshot['lineups']['home']['players']
    quote='Golden State Warriors will start '+', '.join(names)+' on October 21, 2026.'
    url='https://www.nbcsports.com/nba/news/current-lineup'
    candidate={'url':url,'title':'Warriors Lakers October 21, 2026 confirmed starters','published_at':at.isoformat()}
    fetched={'url':url,'content':quote+' '+('Current game news. '*10),'captured_at':at.isoformat(),'published_date':at.isoformat()}
    row={'side':'home','kind':'confirmed_starter','players':names,'status':'available','url':url,'quote':quote,
         'game_reference':'October 21, 2026'}
    # The test tip is Oct20 in New York; date identity is deliberately local.
    game['tipoff']=(at+timedelta(hours=5)).isoformat()
    return game,early,[(candidate,fetched)],row,at


def test_qualified_local_media_and_chinese_news_not_community():
    for url in ['https://www.celticsblog.com/boston-celtics-news/a','https://news.zhibo8.com/nba/a','https://voice.hupu.com/nba/a']:
        assert source_role(url)=='professional'
    assert source_role('https://bbs.hupu.com/fixture.html')=='secondary_discovery'
    assert source_role('https://k.sina.cn/article_user.html')=='secondary_discovery'


def test_exact_body_game_and_time_required():
    game,early,sources,row,at=source_fixture()
    claims,pipeline=qualify_claims(game,early,sources,[row],at.isoformat())
    assert len(claims)==1
    wrong={**row,'players':row['players'][:-1]+['Invented Player']}
    assert qualify_claims(game,early,sources,[wrong],at.isoformat())[0]==[]
    assert qualify_claims(game,early,sources,[{**row,'game_reference':'October 19, 2026'}],at.isoformat())[0]==[]
    future=copy.deepcopy(sources);future[0][1]['published_date']=(at+timedelta(minutes=1)).isoformat()
    future[0][0]['published_at']=future[0][1]['published_date']
    assert not qualify_claims(game,early,future,[row],at.isoformat())[0]


def test_projection_never_promoted_and_membership_not_active_list():
    game,early,sources,row,at=source_fixture()
    projected={**row,'quote':'Projected starters: '+', '.join(row['players'])}
    assert not qualify_claims(game,early,sources,[projected],at.isoformat())[0]
    roster={**row,'kind':'eligibility'}
    assert not qualify_claims(game,early,sources,[roster],at.isoformat())[0]


def test_cross_midnight_game_reference_uses_nba_local_date():
    game,early,snapshot,at=fixture()
    assert research_game(game)['scheduled_tipoff'].startswith('2026-10-20T')


def test_chinese_names_resolve_to_current_canonical_players():
    game,early,sources,row,at=source_fixture()
    aliases=[{'canonical_name_en':name,'display_name_zh':'球员·'+zh}for name,zh in zip(row['players'],['甲甲','乙乙','丙丙','丁丁','戊戊'])]
    early['input_snapshot']={'player_display_names':aliases}
    quote='Golden State Warriors确认首发：'+', '.join(r['display_name_zh']for r in aliases)+'，2026-10-21。'
    sources[0][1]['content']=quote+' Current game details.'*10
    row={**row,'quote':quote,'game_reference':'2026-10-21','players':[r['display_name_zh']for r in aliases]}
    rosters={'home':{'players':[{'name':r['canonical_name_en']}for r in aliases]}}
    accepted,_=qualify_claims(game,early,sources,[row],at.isoformat(),rosters=rosters)
    assert accepted[0]['players']==[r['canonical_name_en']for r in aliases]


def test_latest_verified_keeps_unknown_and_expected_in_context():
    game,early,snapshot,at=early_fixture()
    game['policy_config']={'final_information_policy':'latest_verified'}
    for side in ('home','away'):snapshot['lineups'][side]['basis']='agent_expected'
    snapshot['availability']['records']=[]
    before=copy.deepcopy(snapshot)
    ready=evaluate_readiness(game,early,snapshot,at)
    assert ready['ready'] and ready['warnings'] and snapshot==before
    snapshot['information_uncertainty']=['首发尚未确认，出战状态仍未知。']
    context,_,plan,_=compact_context(early,snapshot,at.isoformat())
    assert all(l.basis=='agent_expected'for l in plan.lineups)
    assert not context.evidence_quality['confirmed_lineups']
    assert plan.uncertainty
    assert all(not t['confirmed_starters']for t in context.roster_context['teams'].values())
    assert not any(r.get('value',{}).get('status')in {'Available','available'}for r in context.objective_evidence)


def test_uncertainty_update_runs_bounded_assessment_without_market_leak(tmp_path):
    from .final_delta import run_incremental,numbers
    from .test_final_delta import writer
    from .analyst import EvidenceBoundAnalyst
    game,early,snapshot,at=early_fixture();calls=[]
    for side in ('home','away'):snapshot['lineups'][side]['basis']='agent_expected'
    snapshot['availability']['records']=[]
    def assess(messages,schema):
        payload=json.loads(messages[-1]['content']);calls.append(payload)
        assert payload['basketball_delta']['unresolved_information']
        assert all(r['value'].get('claim_type')!='market_reference'for r in payload['allowed_evidence'])
        return {'prediction':numbers(early),'rationale':'没有新冲突，保留数值并把未确认出战作为条件。',
                'evidence_ids':[payload['allowed_evidence'][0]['id']]},{}
    analyst=EvidenceBoundAnalyst(root=tmp_path/'writer',client=writer,forecast_client=assess,max_calls=4)
    result=run_incremental(early,{'ready':True,'snapshot':snapshot,'checked_at':at.isoformat(),
        'warnings':['home_lineup_not_confirmed','home_availability_unresolved:Example']},analyst,tmp_path/'analysis',client=assess)
    assert len(calls)==1 and result['change_assessment']['decision']=='retain'
    assert result['judgment']['final_prediction']['expected_margin']==early['judgment']['final_prediction']['expected_margin']


@pytest.mark.parametrize('failure',['tipoff','identity','out','market','membership'])
def test_latest_verified_cannot_bypass_hard_boundaries(failure):
    game,early,snapshot,at=fixture();game['policy_config']={'final_information_policy':'latest_verified'}
    if failure=='tipoff':at=at+timedelta(minutes=20)
    if failure=='identity':snapshot['game_identity']['game_id']='espn:999'
    if failure=='out':snapshot['availability']['records'][0]['status']='Out'
    if failure=='market':snapshot['market_collection']={}
    if failure=='membership':snapshot['rosters']['home']['players']=[]
    assert not evaluate_readiness(game,early,snapshot,at)['ready']


def test_deepseek_transport_preserves_body_and_has_no_credentials_in_receipt(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','private-test-key')
    requests=[]
    class Response:
        def __init__(self,data):self.data=data
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(self.data).encode()
    def opener(request,timeout):
        requests.append(request)
        return Response({'results':[{'title':'News','url':'https://www.nba.com/news/a','date':'2026-10-20T18:00:00Z','snippet':'snippet'}]}
            if request.full_url.endswith('/search')else{'title':'News','content':'Exact fetched body'})
    transport=DeepSeekWebTransport(root=tmp_path,opener=opener)
    assert transport.search({'query':'NBA lineup'})['results'][0]['published_date']=='2026-10-20T18:00:00Z'
    assert transport.fetch_many(['https://www.nba.com/news/a'])[0]['content']=='Exact fetched body'
    assert len(requests)==2 and all(r.full_url.startswith('https://api.deepseek.com/v1/tools/')for r in requests)
    assert all('private-test-key'not in p.read_text()for p in tmp_path.glob('*.json'))
    assert transport.fetch_many(['http://127.0.0.1/secret'])[0]['error']=='unsafe_source_url'
