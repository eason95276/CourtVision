"""Real local export and queue, injected unavailable Git and an in-memory GitHub API."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from . import opening_publish
from .github_delivery import PREFIX,canonical
from .publication_deployment import DeploymentChecks
from .test_publication_queue import published,transport,GID,VID


class API:
    def __init__(self):
        self.current='base';self.snapshots={'base':{'public_data/index.json':{'games':[]},'README.md':'human'}}
        self.trees={};self.commits={};self.writes=0;self.race=False;self.lose_ack=False;self.runs=[]
    def head(self):return {'sha':self.current,'commit':{'tree':{'sha':self.current}}}
    def read(self,path,ref):return copy.deepcopy(self.snapshots[ref][path])
    def request(self,method,path,data=None):
        assert path.startswith(PREFIX+'/')
        if method=='GET':return {'workflow_runs':self.runs}
        if path.endswith('/git/trees'):
            tree=copy.deepcopy(self.snapshots[data['base_tree']])
            for entry in data['tree']:
                assert entry['path']in {'public_data/index.json','public_data/games/espn_401909089.json'}
                tree[entry['path']]=json.loads(entry['content'])
            sha='tree'+str(len(self.trees));self.trees[sha]=tree;return {'sha':sha}
        if path.endswith('/git/commits'):
            sha='commit'+str(len(self.commits));self.commits[sha]=data;self.snapshots[sha]=self.trees[data['tree']];return {'sha':sha}
        assert method=='PATCH'and path.endswith('/git/refs/heads/main')and data['force']is False
        if self.race:
            self.race=False;self.snapshots['concurrent']=copy.deepcopy(self.snapshots[self.current]);self.current='concurrent'
            self.snapshots[self.current]['README.md']='concurrent human edit'
            raise RuntimeError('github_delivery_http_422')
        assert self.commits[data['sha']]['parents']==[self.current]
        self.current=data['sha'];self.writes+=1
        if self.lose_ack:self.lose_ack=False;raise RuntimeError('github_delivery_network_unavailable')
        return {'ref':'refs/heads/main'}


def unavailable_git(args,**kwargs):
    if args[:3]==['git','remote','get-url']:
        return subprocess.CompletedProcess(args,0,opening_publish.REMOTE+'\n','')
    if args[:2]==['git','clone']:
        return subprocess.CompletedProcess(args,1,'','injected DNS failure')
    raise AssertionError('unexpected command')


def prepare(transport):
    options,_,source,_=transport
    guard=Path(__file__).resolve().parents[3]/'courtvision-public/scripts/check_public_release.py'
    if not guard.is_file():guard=Path(__file__).resolve().parents[2]/'scripts/check_public_release.py'
    (source/'scripts').mkdir();shutil.copy2(guard,source/'scripts/check_public_release.py')
    return options


def deliver(queue,database,options,api):
    queue.enqueue(database,GID,VID)
    return queue.run(queue.claim(),lambda job:opening_publish.publish_saved(job,**{**options,'remote':opening_publish.REMOTE},
        runner=unavailable_git,api_factory=lambda:api))


def test_existing_queue_falls_back_without_a_model_or_dirty_checkout_write(published,transport):
    database,queue,*_=published;options=prepare(transport);before=database.read_bytes();api=API()
    result=deliver(queue,database,options,api)
    assert result['status']=='succeeded'and result['result']['transport']=='github_api'
    record=api.snapshots[api.current]['public_data/games/espn_401909089.json']
    assert record['public_sha256']==canonical({k:v for k,v in record.items()if k!='public_sha256'})
    assert record['prediction']['version_id']==VID
    assert database.read_bytes()==before
    assert (options['public_root']/'README.md').read_text()=='Uncommitted human work'
    assert (options['public_root']/'design-drafts/keep.txt').read_text()=='keep'
    assert DeploymentChecks(queue).summary()[0]['status']=='pending'


def test_nonforced_api_retry_preserves_a_concurrent_main_edit(published,transport):
    database,queue,*_=published;options=prepare(transport);api=API();api.race=True
    assert deliver(queue,database,options,api)['status']=='succeeded'
    assert api.snapshots[api.current]['README.md']=='concurrent human edit'
    assert api.writes==1


def test_lost_api_ack_reuses_the_saved_remote_record(published,transport):
    database,queue,clock,*_=published;options=prepare(transport);api=API();api.lose_ack=True
    assert deliver(queue,database,options,api)['status']=='retry'
    clock.advance(31)
    result=queue.run(queue.claim(),lambda job:opening_publish.publish_saved(job,**{**options,'remote':opening_publish.REMOTE},
        runner=unavailable_git,api_factory=lambda:api))
    assert result['status']=='succeeded'and result['result']['status']=='already_published'
    assert api.writes==1


def test_factual_failure_does_not_switch_transport(monkeypatch):
    def reject(*args,**kwargs):raise ValueError('public_release_export_hash_changed')
    monkeypatch.setattr(opening_publish,'_publish_saved_git',reject)
    with pytest.raises(ValueError,match='export_hash_changed'):
        opening_publish.publish_saved({'version_id':VID},public_root='unused',queue_root='unused',api_factory=lambda:pytest.fail('must not bypass guard'))


@pytest.mark.parametrize('scenario',['readme_only','stale_site','pages_failed','success'])
def test_website_verification_requires_correct_workflow_and_actual_site(published,transport,scenario):
    database,queue,*_=published;options=prepare(transport);api=API()
    assert deliver(queue,database,options,api)['status']=='succeeded'
    checks=DeploymentChecks(queue);job=checks.claim();record=api.read('public_data/games/espn_401909089.json',api.current)
    site=copy.deepcopy(record)
    api.runs=[{'id':1,'name':"Tomorrow's selected games"if scenario=='readme_only'else'Public product website',
               'head_branch':'main','head_sha':api.current,'status':'completed','conclusion':'failure'if scenario=='pages_failed'else'success'}]
    if scenario in {'stale_site','pages_failed'}:
        site['publication']['exported_at']='older';site['public_sha256']=canonical({k:v for k,v in site.items()if k!='public_sha256'})
    result=checks.run(job,api=api,site_fetch=lambda path:site)
    assert result['status']==('succeeded'if scenario=='success'else'failed'if scenario=='pages_failed'else'pending')
    assert checks.summary()[0]['status']==result['status']


def test_deployment_check_recovers_an_expired_claim_without_republishing(published,transport):
    database,queue,clock,*_=published;options=prepare(transport);api=API()
    assert deliver(queue,database,options,api)['status']=='succeeded'
    old=DeploymentChecks(queue).claim();clock.advance(181)
    checks=DeploymentChecks(queue);new=checks.claim()
    assert new['owner_token']!=old['owner_token'] and new['attempts']==2
    record=api.read('public_data/games/espn_401909089.json',api.current)
    api.runs=[{'id':7,'name':'Public product website','head_branch':'main','head_sha':api.current,
               'status':'completed','conclusion':'success'}]
    with pytest.raises(ValueError,match='claim_lost'):
        checks.run(old,api=api,site_fetch=lambda _:record)
    assert checks.run(new,api=api,site_fetch=lambda _:record)['status']=='succeeded'
    assert api.writes==1


def test_correction_job_is_bound_to_original_hash_and_preserves_prediction(published,tmp_path,monkeypatch):
    from . import publication_queue
    from .publication_queue import saved_publication
    from v2.pregame_system.storage import content_hash
    database,queue,*_=published;before=database.read_bytes()
    folder=tmp_path/'corrections';folder.mkdir();monkeypatch.setattr(publication_queue,'DEFAULT_PORTFOLIO_EDITORIAL_DIR',folder)
    base=saved_publication(database,GID,VID);editorial={'analysis_article':'Explicit factual correction.'}
    digest=content_hash(editorial)
    artifact={'purpose':'factual_correction','base_prediction_version_id':VID,
        'base_prediction_payload_sha256':base['payload_sha256'],'editorial':editorial,'editorial_sha256':digest}
    path=folder/(VID+'.json');path.write_text(json.dumps(artifact),encoding='utf-8')
    source=saved_publication(database,GID,VID+'::editorial::'+digest)
    assert source['prediction_payload_sha256']==base['payload_sha256']
    assert source['version']['judgment']['final_prediction']==base['version']['judgment']['final_prediction']
    assert database.read_bytes()==before
    artifact['base_prediction_payload_sha256']='0'*64;path.write_text(json.dumps(artifact),encoding='utf-8')
    with pytest.raises(ValueError,match='correction_invalid'):
        saved_publication(database,GID,VID+'::editorial::'+digest)
