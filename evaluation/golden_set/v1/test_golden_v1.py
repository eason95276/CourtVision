import copy,json,sys
from pathlib import Path
from collections import Counter
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from select_samples import HERE,candidates,select,risk_audit
from asset_scanner import EVAL,read_json,canonical_hash
from validate_closed_book_context import validate
from export_annotations import normalize
from generate_outputs import exclusive_json

@pytest.fixture(scope='module')
def m():return read_json(HERE/'selection_manifest.json')

def test_exact_regular_unique_20(m):
    assert len(m['samples'])==20 and len(set(m['selected_sample_ids']))==20
    assert all(r['season']=='2025-26' and r['game_type']=='regular_season' for r in m['samples'])

def test_phase_team_constraints(m):
    reps=[r for r in m['samples'] if r['selection_bucket']=='representative']
    assert len(reps)==12 and dict(Counter(r['season_phase'] for r in reps))=={'early':3,'mid':6,'late':3}
    assert all(not r['missing_fields'] for r in reps)
    teams=Counter(str(r[s+'_team_id']) for r in m['samples'] for s in ('home','away'))
    assert len(teams)>=24 and max(teams.values())<=3

def test_risk_no_numeric_error_anchor(m):
    assert len(m['risk_anchor_audit'])==40
    assert all(not r['eligible'] for r in m['risk_anchor_audit'])
    assert sum(r['selection_bucket']=='challenge' for r in m['samples'])==8

def test_frozen_manifest_hash(m):
    body={k:v for k,v in m.items() if k not in ('golden_set_revision','selection_manifest_hash','created_at')}
    assert canonical_hash(body)==m['selection_manifest_hash']
    assert m['golden_set_revision']=='golden-set-v1-'+m['selection_manifest_hash'][:20]

def test_pregame_no_gt_strata_or_fake_outputs(m):
    imports=read_json(HERE/'pregame_import.json')
    assert len(imports)==20
    for t in imports:
        raw=json.dumps(t,ensure_ascii=False)
        for key in ('ground_truth','hidden_outcome_bucket','selection_bucket','actual_margin','actual_total','bad_case_anchor_if_any','postgame_evidence'):
            assert key not in raw
        assert t['data']['model_output'] is None
        assert t.get('annotations') is None and t.get('predictions') is None

def test_postgame_gt_boxscore_same_sample(m):
    samples={r['sample_id']:r for r in m['samples']}
    for t in read_json(HERE/'postgame_import.json'):
        r=samples[t['meta']['sample_id']];g=t['data']['ground_truth']
        assert g['target_game_id']==r['target_game_id']
        assert len({p['team_id'] for p in t['data']['postgame_evidence']['players']})==2
        assert t['data']['model_output'] is None and t['data']['frozen_pregame_prediction'] is None

def test_both_xml_configs_parse_and_no_default_score():
    import xml.etree.ElementTree as ET
    for stage in ('pregame','postgame'):
        root=ET.parse(HERE/(stage+'_config.xml')).getroot()
        assert len(root.findall('.//Choices'))==12
        assert all(c.get('selected') is None for c in root.findall('.//Choice'))

def test_guides_every_metric_has_anchors_and_exclusion():
    r=read_json(HERE/'rubric.json')
    for stage in ('pregame','postgame'):
        for item in r[stage]['soft_metrics'].values():assert all(item[k] for k in ('measures','not','1','3','5','example'))
    assert r['overall_pass_threshold'] is None

def test_no_fake_annotations_export(m):
    labels,disagreements=normalize(read_json(HERE/'pregame_import.json'),m,read_json(HERE/'rubric.json'))
    assert labels==[] and disagreements==[]

def test_scores_without_output_rejected(m):
    t=copy.deepcopy(read_json(HERE/'pregame_import.json')[0]);t['annotations']=[{'result':[]}]
    with pytest.raises(ValueError,match='awaiting_generation'):normalize([t],m,read_json(HERE/'rubric.json'))

def test_generation_dry_run_no_network_or_calls():
    report=read_json(HERE/'generation_dry_run.json')
    assert report['jobs_validated']==40 and report['model_calls']==0 and report['outputs_written']==0
    for job in report['jobs']:
        assert job['tools']==[] and job['network_allowed'] is False and job['research_enabled'] is False
        if job['stage']=='pregame':assert 'ground_truth' not in job['model_inputs']

def test_output_cannot_overwrite(tmp_path):
    p=tmp_path/'unit-fixture.json';exclusive_json(p,{'unit_fixture':True})
    with pytest.raises(FileExistsError):exclusive_json(p,{'replace':True})

def test_selection_binding_and_leakage(m):
    for r in m['samples']:
        c=read_json(EVAL/r['context_path']);p=read_json(EVAL/r['provenance_path'])
        assert canonical_hash(c)==r['context_sha256'] and not validate(c,p)

def test_actual_import_contracts(m):
    pre=read_json(HERE/'pregame_import.json');post=read_json(HERE/'postgame_import.json')
    assert {t['meta']['sample_id'] for t in pre}==set(m['selected_sample_ids'])=={t['meta']['sample_id'] for t in post}
    assert all(t['meta']['golden_set_revision']==m['golden_set_revision'] for t in pre+post)

def test_generation_job_json_schema():
    from jsonschema import Draft202012Validator
    validator=Draft202012Validator(read_json(HERE/'generation_job.schema.json'))
    for job in read_json(HERE/'generation_dry_run.json')['jobs']:validator.validate(job)

def test_output_schema_cannot_accept_placeholder():
    from jsonschema import Draft202012Validator
    schema=read_json(HERE/'generation_output.schema.json')
    assert list(Draft202012Validator(schema).iter_errors({'output':None}))

def test_annotation_schema_has_no_mandatory_second_annotator():
    schema=read_json(HERE/'annotation.schema.json')
    assert 'annotator_a' in schema['required'] and 'annotator_b' not in schema['required']

def test_fixed_skill_snapshot_matches_declared_hash():
    from asset_scanner import sha
    profile=read_json(HERE/'generation_profile.json')
    assert 'pregame_research' not in profile['skill_snapshots']
    for record in profile['skill_snapshots'].values():assert sha(HERE/record['path'])==record['sha256']
