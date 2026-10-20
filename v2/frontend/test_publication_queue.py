"""Offline delivery tests: isolated SQLite fixtures, real local Git and a GET-only server."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .opening_publish import publish_saved
from .publication_queue import PublicationQueue, saved_publication

GID = 'espn:401909089'
VID = 'offline-publication-version'


class Clock:
    def __init__(self): self.at = datetime(2026,10,20,14,tzinfo=timezone.utc)
    def __call__(self): return self.at
    def advance(self, seconds): self.at += timedelta(seconds=seconds)


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def published(tmp_path):
    game = {'id':GID,'home':{'id':1610612752,'name_zh':'尼克斯'},
        'away':{'id':1610612755,'name_zh':'76人'},'tipoff':'2026-10-20T23:00:00+00:00','status':'scheduled'}
    identity = {'game_id':GID,'home_team_id':1610612752,'away_team_id':1610612755,'scheduled_tipoff':game['tipoff']}
    version = {'game_id':GID,'version_id':VID,'game_identity':identity,'stage':'FINAL_PREGAME',
        'input_cutoff_at':'2026-10-20T13:00:00+00:00','generated_at':'2026-10-20T13:01:00+00:00',
        'published_at':'2026-10-20T13:02:00+00:00','input_snapshot':game,
        'judgment':{'analysis_article':'Offline fixture, not a real prediction.',
            'final_prediction':{'winner_side':'home','win_probability':.6,'expected_margin':3,'expected_total':220}},
        'analyst_context':{'objective_evidence':[]},'sources':[]}
    raw = json.dumps(version, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    database = tmp_path/'versions.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE prediction_versions(game_id TEXT,version_id TEXT,payload_json TEXT,payload_sha256 TEXT)')
        db.execute('CREATE TABLE game_prediction_state(game_id TEXT,current_version_id TEXT,locked_version_id TEXT,registered_game_json TEXT,policy_config_json TEXT)')
        db.execute('INSERT INTO prediction_versions VALUES(?,?,?,?)',(GID,VID,raw,digest))
        db.execute('INSERT INTO game_prediction_state VALUES(?,?,NULL,?,?)',(GID,VID,json.dumps(game),'{"report_enabled":true}'))
    clock = Clock()
    queue = PublicationQueue(tmp_path/'delivery/queue.sqlite', clock=clock)
    return database, queue, clock, game, {**version,'payload_sha256':digest}


@pytest.fixture
def transport(tmp_path, published):
    database, queue, clock, game, version = published
    bare, source = tmp_path/'remote.git', tmp_path/'developer'
    bare.mkdir(); git(bare,'init','--bare','--initial-branch=main')
    source.mkdir(); git(source,'init','--initial-branch=main')
    git(source,'config','user.name','Offline publisher');git(source,'config','user.email','offline@example.invalid')
    # Exercise the actual public exporter, not a test that writes the desired output itself.
    exporter = Path(__file__).resolve().parents[3]/'courtvision-public/publish_game.py'
    if not exporter.is_file(): exporter = Path(__file__).resolve().parents[2]/'publish_game.py'
    shutil.copyfile(exporter,source/'publish_game.py')
    (source/'public_data').mkdir();(source/'public_data/index.json').write_text('{"games":[]}',encoding='utf-8')
    (source/'README.md').write_text('Original README',encoding='utf-8')
    git(source,'add','.');git(source,'commit','-m','Offline initial');git(source,'remote','add','origin',str(bare));git(source,'push','origin','main')
    (source/'README.md').write_text('Uncommitted human work',encoding='utf-8')
    (source/'design-drafts').mkdir();(source/'design-drafts/keep.txt').write_text('keep',encoding='utf-8')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith('/api/game?'):
                value = {'status':'success','game':game,'prediction':{'status':'current','game_id':GID,'version_id':VID,'version':version,'state_revision':1}}
            elif self.path.startswith('/api/postgame?') and version.get('test_postgame_payload'):
                value={'status':'available','postgame':version['test_postgame_payload']}
            else: value = {'status':'unavailable','postgame':None}
            raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
        def do_POST(self): raise AssertionError('No publication POST allowed')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    options={'public_root':source,'queue_root':queue.root,'remote':str(bare),
        'api_base':f'http://127.0.0.1:{server.server_port}','run_checks':False}
    try: yield options,bare,source,calls
    finally: server.shutdown();server.server_close();thread.join(timeout=2)


def test_queue_is_separate_deduplicated_and_restartable(published):
    database,queue,clock,*_ = published
    before=database.read_bytes()
    queue.enqueue(database,GID,VID);queue.enqueue(database,GID,VID)
    restarted=PublicationQueue(queue.path,clock=clock)
    assert len(restarted.summary())==1
    assert restarted.claim()['version_id']==VID
    assert database.read_bytes()==before


def test_recover_published_version_after_worker_ack_loss(published):
    database,queue,clock,*_=published
    assert queue.discover(database,game_ids={GID},stages={'FINAL_PREGAME'})['eligible']==1
    assert queue.claim()['version_id']==VID


def test_discovery_respects_game_and_stage_scope(published):
    database,queue,*_=published
    assert queue.discover(database,game_ids={'espn:another'})['eligible']==0
    assert queue.discover(database,stages={'EARLY_PREVIEW'})['eligible']==0
    assert queue.summary()==[]


def test_postgame_delivery_has_own_identity_and_does_not_skip_same_pregame(published,transport,monkeypatch):
    from v2.postgame_system import editorial
    database,queue,clock,game,version=published
    options,bare,source,calls=transport
    queue.enqueue(database,GID,VID)
    assert queue.run(queue.claim(),lambda job:publish_saved(job,**options))['status']=='succeeded'
    post_id='offline-postgame'
    post={'game_id':GID,'version_id':post_id,'base_prediction_version_id':VID,
        'base_prediction_payload_sha256':version['payload_sha256'],'published_at':'2026-10-21T02:00:00Z'}
    raw=json.dumps(post,sort_keys=True)
    with sqlite3.connect(database)as db:
        db.execute('CREATE TABLE postgame_versions(game_id TEXT,version_id TEXT,base_prediction_version_id TEXT,published_at TEXT,payload_json TEXT,payload_sha256 TEXT)')
        db.execute('INSERT INTO postgame_versions VALUES(?,?,?,?,?,?)',(GID,post_id,VID,post['published_at'],raw,hashlib.sha256(raw.encode()).hexdigest()))
    public={'game_id':GID,'version_id':post_id,'title':'Test review','short_review':'Direction review',
        'article':'Synthetic postgame article.','score':{'text':'110-100'},'prediction':{},'market_reference':{},'generated_at':'2026-10-21T02:01:00Z'}
    monkeypatch.setattr(editorial,'load_postgame_editorial',lambda *args,**kwargs:{'artifact_sha256':'f'*64})
    monkeypatch.setattr(editorial,'build_public_postgame_payload',lambda *args,**kwargs:public)
    monkeypatch.setattr(editorial,'build_public_postgame_entry',lambda *args,**kwargs:{'status':'available','version_id':post_id})
    game['postgame']={'status':'available','version_id':post_id}
    version['test_postgame_payload']=public
    assert queue.discover_postgames(database,game_ids={GID})['eligible']==1
    job=queue.claim()
    assert job['version_id'].startswith(VID+'::postgame::'+post_id+'::view::')
    before=git(bare,'rev-list','--count','main')
    result=queue.run(job,lambda item:publish_saved(item,**options))
    assert result['status']=='succeeded'
    assert int(git(bare,'rev-list','--count','main'))==int(before)+1
    remote=json.loads(git(bare,'show','main:public_data/games/espn_401909089.json'))
    assert remote['postgame']['postgame']['article']==public['article']
    assert remote['prediction']['version']['version_id']==VID
    assert '/api/postgame?game_id='+GID in calls


def test_dispatch_does_not_claim_another_scope(published):
    database,queue,*_=published
    queue.enqueue(database,GID,VID)
    assert queue.claim(game_ids=set()) is None
    assert queue.claim(game_ids={'espn:999999999'}) is None
    assert queue.claim(game_ids={GID})['version_id']==VID


def test_retry_and_expired_lease_use_new_ownership(published):
    database,queue,clock,*_=published
    queue.enqueue(database,GID,VID);old=queue.claim();clock.advance(121)
    new=PublicationQueue(queue.path,clock=clock).claim()
    assert new['owner_token']!=old['owner_token'] and new['attempts']==2
    with pytest.raises(ValueError,match='claim_lost'):queue._finish(old,result={'status':'pushed'})
    assert queue.run(new,lambda job:{'status':'already_published'})['status']=='succeeded'


def test_real_git_delivery_preserves_dirty_development_tree(published,transport):
    database,queue,*_=published;options,bare,source,calls=transport
    head=git(source,'rev-parse','HEAD');queue.enqueue(database,GID,VID)
    result=queue.run(queue.claim(),lambda job:publish_saved(job,**options))
    assert result['status']=='succeeded' and result['result']['status']=='pushed'
    assert git(source,'rev-parse','HEAD')==head
    assert (source/'README.md').read_text()=='Uncommitted human work'
    assert (source/'design-drafts/keep.txt').read_text()=='keep'
    record=json.loads(git(bare,'show','main:public_data/games/espn_401909089.json'))
    assert record['prediction']['version_id']==VID
    assert calls and all(path.startswith('/api/')for path in calls)


def test_network_failure_retries_without_generation(published,transport):
    database,queue,clock,*_=published;options,*_=transport
    count=[0]
    def flaky(args,**kwargs):
        if args[:2]==['git','clone'] and not count[0]:
            count[0]+=1;raise subprocess.TimeoutExpired(args,1)
        return subprocess.run(args,**kwargs)
    queue.enqueue(database,GID,VID)
    assert queue.run(queue.claim(),lambda job:publish_saved(job,runner=flaky,**options))['status']=='retry'
    assert queue.claim() is None
    clock.advance(31)
    assert queue.run(queue.claim(),lambda job:publish_saved(job,runner=flaky,**options))['status']=='succeeded'


def test_lost_push_ack_does_not_create_a_second_commit(published,transport):
    database,queue,clock,*_=published;options,bare,source,calls=transport
    def lost_ack(args,**kwargs):
        result=subprocess.run(args,**kwargs)
        if args[:2]==['git','push'] and result.returncode==0:
            return subprocess.CompletedProcess(args,1,'','simulated acknowledgment loss')
        return result
    queue.enqueue(database,GID,VID)
    assert queue.run(queue.claim(),lambda job:publish_saved(job,runner=lost_ack,**options))['status']=='retry'
    head=git(bare,'rev-parse','main');requests=len(calls);clock.advance(31)
    result=queue.run(queue.claim(),lambda job:publish_saved(job,**options))
    assert result['result']['status']=='already_published'
    assert git(bare,'rev-parse','main')==head and len(calls)==requests


def test_newer_remote_version_is_not_overwritten(published,transport,tmp_path):
    database,queue,*_=published;options,bare,source,calls=transport
    path=tmp_path/'other';git(tmp_path,'clone',str(bare),str(path))
    git(path,'config','user.name','Other');git(path,'config','user.email','other@example.invalid')
    folder=path/'public_data/games';folder.mkdir()
    (folder/'espn_401909089.json').write_text(json.dumps({'prediction':{'version':{
        'version_id':'newer-version','published_at':'2026-10-20T14:00:00+00:00'}}}),encoding='utf-8')
    git(path,'add','.');git(path,'commit','-m','Newer report');git(path,'push','origin','main')
    queue.enqueue(database,GID,VID)
    result=queue.run(queue.claim(),lambda job:publish_saved(job,**options))
    assert result['status']=='superseded' and calls==[]


def test_corrupt_payload_never_enters_delivery_queue(published):
    database,queue,*_=published
    with sqlite3.connect(database)as db:db.execute("UPDATE prediction_versions SET payload_json='{}'")
    with pytest.raises(ValueError,match='missing_or_corrupt'):queue.enqueue(database,GID,VID)
    assert queue.summary()==[]


def test_retry_limit_is_terminal(published):
    database,queue,clock,*_=published
    queue=PublicationQueue(queue.path,clock=clock,max_attempts=2)
    queue.enqueue(database,GID,VID)
    def fail(job):raise TimeoutError('offline')
    assert queue.run(queue.claim(),fail)['status']=='retry';clock.advance(901)
    assert queue.run(queue.claim(),fail)['status']=='failed';clock.advance(901)
    assert queue.claim() is None
