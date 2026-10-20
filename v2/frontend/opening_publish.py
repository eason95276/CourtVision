"""Opt-in saved-report delivery through isolated Git checkouts."""
from __future__ import annotations
import argparse
import json
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from v2.pregame_system.prediction_versions import DEFAULT_DATABASE, _instant
from .publication_queue import PublicationQueue, saved_publication

REMOTE = 'https://github.com/eason95276/CourtVision.git'


def publish_saved(job, *, public_root, queue_root, api_base='http://127.0.0.1:8625', runner=None, run_checks=True, remote=REMOTE):
    source = saved_publication(job['source_database'], job['game_id'], job['version_id'])
    if source['payload_sha256'] != job['payload_sha256']:
        raise ValueError('public_release_queued_hash_changed')
    source_root = Path(public_root).resolve()
    env = {**os.environ, 'GIT_TERMINAL_PROMPT':'0', 'GCM_INTERACTIVE':'never'}
    def command(args, *, cwd, timeout=90):
        result = (runner or subprocess.run)(args, cwd=cwd, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout, env=env)
        if result.returncode:
            raise RuntimeError('public_release_command_failed:' + args[0] + ':' + (args[1] if len(args)>1 else ''))
        return result.stdout.strip()
    configured = command(['git','remote','get-url','origin'], cwd=source_root)
    if configured.rstrip('/').removesuffix('.git') != remote.rstrip('/').removesuffix('.git'):
        raise ValueError('public_release_remote_unverified')
    work = Path(queue_root).resolve() / 'workspaces' / str(uuid.uuid4())
    work.mkdir(parents=True, exist_ok=False)
    checkout = work / 'repository'
    command(['git','clone','--single-branch','--branch','main',configured,str(checkout)], cwd=work, timeout=120)
    slug = job['game_id'].replace(':','_')
    path = checkout / 'public_data/games' / (slug+'.json')
    if path.is_file():
        old = json.loads(path.read_text(encoding='utf-8'))
        previous = (old.get('prediction') or {}).get('version') or {}
        if previous.get('version_id') == job['version_id']:
            if previous.get('payload_sha256') != job['payload_sha256']:
                raise ValueError('public_release_remote_version_hash_conflict')
            clean = {key:value for key,value in old.items() if key != 'public_sha256'}
            digest = hashlib.sha256(json.dumps(clean,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            index = json.loads((checkout/'public_data/index.json').read_text(encoding='utf-8'))
            entry = next((row for row in index.get('games',[]) if row.get('game_id')==job['game_id']),None)
            if (old.get('public_sha256') != digest or not entry or entry.get('public_sha256') != digest
                    or entry.get('version_id') != job['version_id']):
                raise ValueError('public_release_remote_record_integrity_failed')
            return {'status':'already_published','version_id':job['version_id'],
                    'commit':command(['git','rev-parse','HEAD'],cwd=checkout)}
        if previous.get('published_at') and _instant(previous['published_at'],'remote_published_at') >= _instant(source['version']['published_at'],'published_at'):
            return {'status':'superseded','version_id':job['version_id'],'remote_version_id':previous.get('version_id')}
    command([sys.executable,str(checkout/'publish_game.py'),job['game_id'],'--destination',str(checkout/'public_data'),'--api-base',api_base],cwd=checkout,timeout=60)
    record = json.loads(path.read_text(encoding='utf-8'))
    exported = record['prediction']['version']
    if (exported['version_id'],exported['payload_sha256']) != (job['version_id'],job['payload_sha256']):
        return {'status':'superseded','version_id':job['version_id'],'selected_version_id':exported['version_id']}
    selection_path = checkout/'config/daily_picks.json'
    if selection_path.is_file():
        selection = json.loads(selection_path.read_text(encoding='utf-8'))
        changed_selection = False
        for picks in selection.get('slates',{}).values():
            for pick in picks:
                if pick.get('game_id') == job['game_id'] and pick.get('update_status') != 'published':
                    pick['update_status'] = 'published'
                    changed_selection = True
        if changed_selection:
            selection_path.write_text(json.dumps(selection,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if run_checks:
        command([sys.executable,'-m','unittest','discover','-s','tests/public','-q'],cwd=checkout)
        command([sys.executable,'scripts/check_public_release.py','--history'],cwd=checkout,timeout=180)
        command([sys.executable,'scripts/build_public_site.py'],cwd=checkout)
    command(['git','diff','--check'],cwd=checkout)
    paths = ['public_data/index.json','public_data/games/'+slug+'.json']
    if selection_path.is_file(): paths.append('config/daily_picks.json')
    if not set(command(['git','diff','--name-only'],cwd=checkout).splitlines()) <= set(paths):
        raise ValueError('public_release_unexpected_changed_files')
    command(['git','add','--',*paths],cwd=checkout)
    name = command(['git','config','user.name'],cwd=source_root)
    email = command(['git','config','user.email'],cwd=source_root)
    command(['git','-c','user.name='+name,'-c','user.email='+email,'commit','-m','Publish saved report '+job['game_id']+' '+job['version_id']],cwd=checkout)
    command(['git','push','origin','HEAD:main'],cwd=checkout,timeout=120)
    return {'status':'pushed','game_id':job['game_id'],'version_id':job['version_id'],
        'commit':command(['git','rev-parse','HEAD'],cwd=checkout),'generation_calls':0,'deployment':'GitHub Pages Actions pending'}


def publication_queue(database=DEFAULT_DATABASE):
    return PublicationQueue(Path(database).resolve().parent / 'public-release' / 'queue.sqlite')


def publish_final(game_id,version_id,*,database=DEFAULT_DATABASE):
    if not os.environ.get('COURTVISION_PUBLIC_PUBLISH_ROOT'): return {'status':'disabled'}
    source=saved_publication(database,game_id,version_id)
    if source['version'].get('stage')!='FINAL_PREGAME': raise ValueError('public_final_not_legal_pregame')
    queue=publication_queue(database);queue.enqueue(database,game_id,version_id);job=queue.claim(version_id)
    if job is None:return {'status':'queued','version_id':version_id}
    return queue.run(job,lambda item:publish_saved(item,public_root=os.environ['COURTVISION_PUBLIC_PUBLISH_ROOT'],
        queue_root=queue.root,api_base=os.environ.get('COURTVISION_PUBLIC_API_BASE','http://127.0.0.1:8625')))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('game_id');p.add_argument('version_id');p.add_argument('--database',default=str(DEFAULT_DATABASE));a=p.parse_args()
    print(json.dumps(publish_final(a.game_id,a.version_id,database=a.database)))
