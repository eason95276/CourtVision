import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import human_workspace as h

def test_next_locked_until_freeze(monkeypatch,tmp_path):
    monkeypatch.setattr(h,'LEDGER',tmp_path);monkeypatch.setattr(h.r.b,'selection',lambda:[{'sample_id':'first'},{'sample_id':'later'}])
    assert h.current()['sample_id']=='first'
    (tmp_path/'first').mkdir();(tmp_path/'first/freeze.json').write_text('{}')
    with pytest.raises(ValueError):h.current()

def test_failure_has_no_soft_metric_controls(monkeypatch):
    monkeypatch.setattr(h,'load',lambda s:({}, {}, None))
    xml=h.xml_for({})
    assert 'EXECUTION_FAILURE_NO_CONTENT' in xml and 'Rating' not in xml

def test_final_only_positive_projection(monkeypatch):
    ctx={'matchup':{'home_team':'Home','away_team':'Away','game_date':'2025-10-29'}}
    editorial={'title':'Title','article':'Article'}
    monkeypatch.setattr(h,'load',lambda s:({'manifest_hash':'a'},ctx,editorial))
    monkeypatch.setattr(h,'context_summary',lambda ctx:'summary')
    monkeypatch.setattr(h,'read_json',lambda path:{'pregame':{}})
    task=h.task_for({'sample_id':'first','context_sha256':'f'})
    assert task['data']['model_output']==editorial
    assert 'locked_forecast' not in json.dumps(task) and 'hidden_outcome_bucket' not in json.dumps(task)

def test_freeze_requires_real_label(monkeypatch,tmp_path):
    monkeypatch.setattr(h,'LEDGER',tmp_path);sample={'sample_id':'first'};monkeypatch.setattr(h,'current',lambda:sample)
    wanted={'data':{},'meta':{}};monkeypatch.setattr(h,'task_for',lambda s:wanted)
    ident={'task_hash':h.canonical_hash(wanted),'xml_hash':h.canonical_hash('xml'),'project_id':1,'task_id':2}
    (tmp_path/'first').mkdir();(tmp_path/'first/identity.json').write_text(json.dumps(ident))
    monkeypatch.setattr(h,'api',lambda s,url: {'label_config':'xml'} if 'export' not in url else [{'id':2,'data':{},'meta':{},'annotations':[]}])
    with pytest.raises(ValueError,match='real completed'):h.freeze_current('test')
