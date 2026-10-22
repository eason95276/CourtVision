"""Durable website verification; a successful Git push is not a deployed report."""
from __future__ import annotations
import json
import sqlite3
import uuid
import urllib.request
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from .github_delivery import GitHubAPI,PREFIX,verify_record


class DeploymentChecks:
    def __init__(self,queue):
        self.queue=queue
        with queue.connect()as db:
            db.execute('''CREATE TABLE IF NOT EXISTS deployment_checks(
                version_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,base_version_id TEXT NOT NULL,
                expected_payload_sha256 TEXT NOT NULL,commit_sha TEXT NOT NULL,status TEXT NOT NULL,
                attempts INTEGER NOT NULL,created_at TEXT NOT NULL,next_check_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,owner_token TEXT,lease_until TEXT,last_error TEXT,result_json TEXT,expected_public_sha256 TEXT)''')

    def enqueue(self,job,result):
        if not result.get('commit') or not result.get('public_sha256'):return
        base=job['version_id'].split('::',1)[0]
        with closing(sqlite3.connect(Path(job['source_database']).resolve().as_uri()+'?mode=ro',uri=True))as db:
            row=db.execute('SELECT payload_sha256 FROM prediction_versions WHERE version_id=? AND game_id=?',(base,job['game_id'])).fetchone()
        if not row:raise ValueError('deployment_base_version_missing')
        at=self.queue.clock()
        with self.queue.connect()as db:
            db.execute('INSERT OR IGNORE INTO deployment_checks VALUES(?,?,?,?,?,?,0,?,?,?,NULL,NULL,NULL,NULL,?)',
                (job['version_id'],job['game_id'],base,row[0],result['commit'],'pending',at.isoformat(),at.isoformat(),(at+timedelta(hours=1)).isoformat(),result['public_sha256']))

    def backfill(self,game_ids=None):
        with self.queue.connect()as db:
            jobs=[dict(r)for r in db.execute("SELECT * FROM publication_jobs WHERE status='succeeded' AND result_json IS NOT NULL")]
        for job in jobs:
            if game_ids is not None and job['game_id']not in game_ids:continue
            self.enqueue(job,json.loads(job['result_json']))

    def claim(self):
        at=self.queue.clock()
        with self.queue.connect()as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE deployment_checks SET status='pending',owner_token=NULL,lease_until=NULL WHERE status='running' AND lease_until<=?",(at.isoformat(),))
            db.execute("UPDATE deployment_checks SET status='failed',last_error='deployment_verification_deadline' WHERE status='pending' AND (deadline_at<=? OR attempts>=20)",(at.isoformat(),))
            row=db.execute("SELECT * FROM deployment_checks WHERE status='pending' AND next_check_at<=? ORDER BY next_check_at,version_id LIMIT 1",(at.isoformat(),)).fetchone()
            if not row:return None
            db.execute("UPDATE deployment_checks SET status='running',attempts=attempts+1,owner_token=?,lease_until=? WHERE version_id=?",
                (str(uuid.uuid4()),(at+timedelta(minutes=3)).isoformat(),row['version_id']))
            return dict(db.execute('SELECT * FROM deployment_checks WHERE version_id=?',(row['version_id'],)).fetchone())

    def run(self,job,*,api=None,site_url='https://nba.weitao.si',site_fetch=None):
        try:result=verify_deployment(job,api=api,site_url=site_url,site_fetch=site_fetch)
        except (OSError,ValueError,RuntimeError)as exc:
            result={'status':'pending','reason':str(exc)[:150]}
        status=result['status'];at=self.queue.clock()
        if status=='pending'and(job['attempts']>=20 or at.isoformat()>=job['deadline_at']):status='failed'
        with self.queue.connect()as db:
            changed=db.execute('''UPDATE deployment_checks SET status=?,next_check_at=?,owner_token=NULL,lease_until=NULL,
                last_error=?,result_json=? WHERE version_id=? AND status='running' AND owner_token=? AND lease_until>?''',
                (status,(at+timedelta(seconds=60)).isoformat(),result.get('reason'),json.dumps(result),job['version_id'],job['owner_token'],at.isoformat())).rowcount
        if not changed:raise ValueError('deployment_verification_claim_lost')
        return {'type':'website_verification','version_id':job['version_id'],'status':status,'result':result}

    def summary(self):
        with self.queue.connect()as db:
            return [dict(r)for r in db.execute('SELECT version_id,game_id,status,attempts,commit_sha,last_error,result_json FROM deployment_checks ORDER BY created_at DESC LIMIT 30')]


def verify_deployment(job,*,api=None,site_url='https://nba.weitao.si',site_fetch=None):
    api=api or GitHubAPI();path='public_data/games/'+job['game_id'].replace(':','_')+'.json'
    head=api.head()['sha'];remote=api.read(path,head);verify_record(remote)
    pred=remote['prediction'].get('version')or{}
    if pred.get('version_id')!=job['base_version_id']:
        return {'status':'superseded','reason':'remote_selection_replaced'}
    if pred.get('payload_sha256')!=job['expected_payload_sha256']:raise ValueError('deployment_remote_payload_conflict')
    if remote['public_sha256']!=job['expected_public_sha256']:
        return {'status':'superseded','reason':'newer_public_view'}
    if site_fetch:site=site_fetch(path)
    else:
        if not site_url.startswith('https://') or '@'in site_url:raise ValueError('deployment_site_url_invalid')
        with urllib.request.urlopen(site_url.rstrip('/')+'/'+path,timeout=8)as response:site=json.load(response)
    verify_record(site)
    runs=api.request('GET',PREFIX+'/actions/runs?per_page=20').get('workflow_runs',[])
    failed=any(r.get('head_sha')==job['commit_sha']and r.get('name')=='Public product website'
               and r.get('conclusion')=='failure'for r in runs)
    if site.get('public_sha256')!=remote['public_sha256']:
        return {'status':'failed'if failed else'pending',
                'reason':'pages_workflow_failed'if failed else'website_not_updated'}
    successes=[r for r in runs if r.get('name')=='Public product website'and r.get('head_branch')=='main'
               and r.get('status')=='completed'and r.get('conclusion')=='success']
    for run in successes[:3]:
        published=api.read(path,run['head_sha']);verify_record(published)
        if published['public_sha256']==remote['public_sha256']:
            return {'status':'succeeded','version_id':job['base_version_id'],
                'payload_sha256':job['expected_payload_sha256'],'public_sha256':remote['public_sha256'],
                'workflow_name':run['name'],'workflow_run_id':run['id'],'verified_commit':run['head_sha']}
    failed=any(r.get('head_sha')==job['commit_sha']and r.get('name')=='Public product website'
               and r.get('conclusion')=='failure'for r in runs)
    return {'status':'failed'if failed else'pending','reason':'pages_workflow_failed'if failed else'pages_success_not_verified'}
