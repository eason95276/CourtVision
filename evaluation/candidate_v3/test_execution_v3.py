import copy,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import execution_v3 as r
from evaluation.candidate_v2.test_contracts import context,output

def envelope(obj):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(obj)}}],'model':'fake','usage':{'total_tokens':1}})
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(r,'BASE',tmp_path);monkeypatch.setattr(r,'ROOT',tmp_path);monkeypatch.setattr(r,'verify',lambda:{'skill_hashes':{}})
    monkeypatch.setattr(r,'preflight',lambda *a:{'status':'PASS'});monkeypatch.setattr(r.v2.b,'context_for',lambda s:context());monkeypatch.setattr(r,'emit_trace',lambda *a:{'fake':True})
    sample={'sample_id':'fixture','target_game_id':'test:1','game_date':'2025-10-29','context_sha256':r.canonical_hash(context())}
    return sample

def test_bound_chain_and_no_reexecution(monkeypatch,tmp_path):
    s=setup(monkeypatch,tmp_path);calls=[]
    def transport(req):
        calls.append(req)
        if len(calls)==1:return envelope(output())
        data=json.loads(req['messages'][1]['content']);assert data['locked_forecast']==r.read_json(tmp_path/'samples/fixture/accepted-forecast.json')['forecast']
        assert data['closed_book_context']==context()
        return envelope({'title':'Title','lead':'Lead','article':'Article','watch_themes':[],'confidence':'low','player_analysis':[]})
    rec=r.execute_sample(s,transport);assert len(calls)==2 and rec['chain_status']=='SUCCEEDED'
    with pytest.raises(ValueError):r.execute_sample(s,transport)

def test_repair_cannot_change_core(monkeypatch,tmp_path):
    s=setup(monkeypatch,tmp_path);o=output();o['analysis_core']['central_question']='X'*181;calls=[]
    def transport(req):
        calls.append(req);return envelope(o if len(calls)==1 else output())
    rec=r.execute_sample(s,transport);assert rec['repair_status']=='FAILED' and len(calls)==2 and rec['editorial_status']=='NOT_RUN'

def test_model_input_exact_closedbook_and_no_other_sample():
    ctx=context();msg=r.messages('forecast',ctx);user=json.loads(msg[1]['content'])
    assert set(user)=={'closed_book_context','evidence_catalog'} and user['closed_book_context']==ctx
    assert 'ground_truth' not in json.dumps(user) and 'Other Sample' not in json.dumps(user)
    assert [m['role'] for m in msg]==['system','user']

def test_v1_v2_and_same20_protected():
    from evaluation.candidate_v3.audit_v2 import protection
    before=r.read_json(r.EVAL/'reports/mega-protection-before.json')
    assert r.canonical_hash(before)==r.canonical_hash(protection())
    assert len(r.v2.b.selection())==20 and r.v2.verify()['sample_ids']==[s['sample_id'] for s in r.v2.b.selection()]
