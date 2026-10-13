import copy,json,sys,xml.etree.ElementTree as ET
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import final_workspace as h,metamorphic as m

def test_readable_article_is_original_not_rewritten():
    e={'title':'Title','lead':'Lead','article':'Paragraph1\n\nParagraph2','watch_themes':[],'player_analysis':[],'confidence':'low'}
    assert e['article'] in h.readable(e) and '\\n' not in h.readable(e)

def test_failure_controls_have_no_soft_scores(monkeypatch):
    monkeypatch.setattr(h,'task_for',lambda s:{'data':{'model_output_status':'NO_CONTENT'}})
    xml=h.xml_for({});assert 'execution_failure' in xml and 'context_grounding' not in xml

def test_rubric_only_display_heading_changes():
    original=(h.EVAL/'golden_set/v1/pregame_config.xml').read_text(encoding='utf-8');changed=original.replace('value="Model Output"','value="最终赛前文章 · 请先读正文，再评分"')
    def controls(xml):return [ET.tostring(x) for x in ET.fromstring(xml) if x.tag in ('Choices','TextArea')]
    assert controls(original)==controls(changed)

def test_market_never_reaches_independent_synthetic_projection():
    case={'control_context':{'market':{'line':3},'other':'same'},'mutated_context':{'market':{'line':8},'other':'same'}}
    assert m.projected_context(case,'control_context','market')==m.projected_context(case,'mutated_context','market')

def test_synthetic_does_not_modify_original():
    case={'control_context':{'market':{'status':'unavailable'}},'mutated_context':{'market':{'status':'unavailable'}}};saved=copy.deepcopy(case)
    m.projected_context(case,'mutated_context','rest');assert case==saved
