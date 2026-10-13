import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run as r
from test_contracts import output,context

def wire(obj,**changes):
    choice={'finish_reason':'stop','message':{'content':json.dumps(obj)}};choice.update(changes)
    return json.dumps({'choices':[choice],'model':'fake','usage':{'total_tokens':1}})

def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(r,'BASE',tmp_path);monkeypatch.setattr(r,'ROOT',tmp_path)
    monkeypatch.setattr(r,'verify',lambda:{})
    monkeypatch.setattr(r.b,'context_for',lambda s:context())
    monkeypatch.setattr(r,'messages',lambda stage,ctx,*a,**kw:[{'role':'system','content':'JSON'},{'role':'user','content':json.dumps({'context':ctx})}])
    monkeypatch.setitem(sys.modules,'trace_candidate',type('T',(),{'emit':staticmethod(lambda *a:None)}))
    return {'sample_id':'synthetic','target_game_id':'test:1','game_date':'2025-10-29','context_sha256':'f'*64}

def test_persisted_forecast_writer_once(monkeypatch,tmp_path):
    s=setup(monkeypatch,tmp_path);calls=[]
    def transport(req):
        calls.append(req)
        if len(calls)==1:return wire(output())
        assert (tmp_path/'samples/synthetic/accepted-forecast.json').exists()
        return wire({'title':'Title','lead':'Lead','article':'Article','watch_themes':[],'confidence':'low','player_analysis':[]})
    record=r.execute_sample(s,transport)
    assert record['chain_status']=='SUCCEEDED' and len(calls)==2
    with pytest.raises(ValueError):r.execute_sample(s,transport)
    assert len(calls)==2

def test_no_parse_retry_no_writer(monkeypatch,tmp_path):
    s=setup(monkeypatch,tmp_path);calls=[]
    def transport(req):calls.append(req);return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"x":'}}]})
    record=r.execute_sample(s,transport)
    assert len(calls)==1 and record['editorial_status']=='NOT_RUN'

def test_one_bounded_repair_content_change_fails(monkeypatch,tmp_path):
    s=setup(monkeypatch,tmp_path);o=output();o['analysis_core']['central_thesis']=o['analysis_core']['central_thesis']['statement'];calls=[]
    def transport(req):
        calls.append(req)
        if len(calls)==1:return wire(o)
        changed=output();changed['prediction']['expected_total']=225.0;return wire(changed)
    record=r.execute_sample(s,transport)
    assert len(calls)==2 and record['repair_triggered'] and record['repair_status']=='FAILED' and record['editorial_status']=='NOT_RUN'

@pytest.mark.parametrize('mutation',['tool','length'])
def test_protocol_failure_no_writer(monkeypatch,tmp_path,mutation):
    s=setup(monkeypatch,tmp_path);calls=[]
    def transport(req):
        calls.append(req)
        return wire(output(),**({'message':{'content':json.dumps(output()),'tool_calls':[{}]}} if mutation=='tool' else {'finish_reason':'length'}))
    record=r.execute_sample(s,transport)
    assert len(calls)==1 and record['chain_status']=='FAILED'
