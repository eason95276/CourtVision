import copy,json,sys,random
from pathlib import Path
from datetime import date,timedelta
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from attacks import cases,base
from asset_scanner import canonical_hash,sha
from validate_closed_book_context import validate
from closed_book_anonymize import anonymize,identity_leaks
from evaluation_integrity import verify_manifest,inside
from closed_book_data import past

@pytest.mark.parametrize('case',cases(),ids=lambda c:c['name'])
def test_temporal_attack(case):
    kwargs={'expected_context_hash':case['expected_hash']} if case['trusted_hash_required'] else {}
    assert validate(case['context'],case['provenance'],**kwargs)

@pytest.mark.parametrize('value',['LAL','Los Angeles','Chase Center','https://example.test/identity','<local-configured-path>','2025-10-21T19:00:00Z','NBA:0022500001'])
def test_identity_layer(value):
    c,p=base();a,m=anonymize(c);a['task_mode']=value
    assert identity_leaks(a,m)

@pytest.mark.parametrize('key',['abbreviation','city','arena','source_metadata','filename','ordering','debug','comments','espn_id','player_id'])
def test_identity_key_layer(key):
    c,p=base();a,m=anonymize(c);a[key]='opaque'
    assert identity_leaks(a,m)

def test_identity_case_and_punctuation():
    c,p=base();a,m=anonymize(c);name=next(iter(m['player_names']))
    a['task_mode']='-'.join(name.swapcase())
    assert identity_leaks(a,m)

def fixture_manifest(root):
    p=root/'datasets/closed_book_contexts/a.json';p.parent.mkdir(parents=True);p.write_text('{"a":1,"b":2}',encoding='utf-8')
    body={'schema_version':'controlled-closed-book-freeze-v1','files':{str(p.relative_to(root)):sha(p)},'builder_files':{},'visibility':'test-only'}
    body['dataset_id']='controlled-cb-v1-'+canonical_hash(body)[:20];return body,p

@pytest.mark.parametrize('attack',['value','addition','deletion','newline','key_order','manifest','manifest_metadata','path_traversal'])
def test_freeze_mutations(tmp_path,attack):
    m,p=fixture_manifest(tmp_path);expected=m['dataset_id'];assert verify_manifest(tmp_path,m,expected)
    if attack=='value':p.write_text('{"a":999}')
    elif attack=='addition':(p.parent/'extra.json').write_text('{}')
    elif attack=='deletion':p.unlink()
    elif attack=='newline':p.write_text('{"a":1,"b":2}\n')
    elif attack=='key_order':p.write_text('{"b":2,"a":1}')
    elif attack=='manifest':m['files'][str(p.relative_to(tmp_path))]='0'*64
    elif attack=='manifest_metadata':m['new_metadata']='hidden'
    else:m['files']['../outside.json']='0'*64
    with pytest.raises(ValueError):verify_manifest(tmp_path,m,expected)

def test_relative_path_denied(tmp_path):
    with pytest.raises(ValueError):inside(tmp_path,'../ground_truth.json')

def test_symlink_parent_denied(tmp_path):
    outside=tmp_path/'outside';outside.mkdir();link=tmp_path/'link'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('Windows symlink privilege unavailable; traversal checks still executed')
    with pytest.raises(ValueError):inside(tmp_path,'link/file.json')

def test_randomized_strict_past_property():
    rng=random.Random(20261010);target=date(2025,12,1)
    for _ in range(200):
        rows=[{'game_id':'target' if rng.random()<0.1 else str(n),'day':(target+timedelta(days=rng.randint(-15,15))).isoformat()} for n in range(60)]
        actual=past(rows,target.isoformat(),'target')
        assert all(r['day']<target.isoformat() and r['game_id']!='target' for r in actual)

def test_rescheduled_target_old_date_never_enters():
    assert not past([{'game_id':'target','day':'2025-11-20'}],'2025-12-01','target')

def test_field_hash_layer_not_masked_by_counts():
    c,p=base();c['team_a']['recent_form']['recent_5']['points_for_per_game']+=1
    assert any(i.startswith('field_hash_mismatch:') for i in validate(c,p))

def test_serializer_no_shared_objects_or_metadata():
    from safe_model_input import serialize_context
    c,p=base();encoded=serialize_context(c);c['team_a']['record']['debug']='ground truth'
    assert 'ground truth' not in encoded
    with pytest.raises(ValueError):serialize_context(c)

def test_placeholder_team_zero_does_not_destroy_boxscore_coverage(tmp_path):
    from asset_scanner import Assets
    p=tmp_path/'team-fixture.csv';p.write_text('gameId,teamId,teamScore,opponentScore\n22500652,1610612749,123,99\n22500652,1610612742,99,123\n22500652,0,0,0\n')
    a=Assets();a.scan_csv([p]);assert 'nba:0022500652' in a.team_box
