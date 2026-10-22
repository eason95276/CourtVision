"""Bounded official GitHub API delivery using the existing local login."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = 'eason95276/CourtVision'
PREFIX = '/repos/' + REPOSITORY


class GitHubAPI:
    def __init__(self, transport=None):
        self.transport = transport

    def request(self, method, path, data=None):
        if not path.startswith(PREFIX + '/'):
            raise ValueError('github_delivery_repository_scope_invalid')
        if self.transport:
            return self.transport(method, path, data)
        from scripts.github_release_cli import credential
        request = urllib.request.Request('https://api.github.com'+path, method=method,
            data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if data is not None else None,
            headers={'Authorization':'Bearer '+credential(),'Accept':'application/vnd.github+json',
                     'User-Agent':'CourtVision-Saved-Delivery','Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=15) as response:
                body=response.read(2_000_001)
                if len(body)>2_000_000:raise ValueError('github_delivery_response_too_large')
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError('github_delivery_http_'+str(exc.code)) from None
        except (OSError,urllib.error.URLError):
            raise RuntimeError('github_delivery_network_unavailable') from None

    def head(self):
        return self.request('GET',PREFIX+'/branches/main')['commit']

    def read(self,path,ref):
        if not path.startswith(('public_data/','config/')) or '..' in path.split('/'):
            raise ValueError('github_delivery_path_invalid')
        value=self.request('GET',PREFIX+'/contents/'+path+'?ref='+ref)
        if value.get('encoding')!='base64':raise ValueError('github_delivery_content_encoding_invalid')
        return json.loads(base64.b64decode(value['content']).decode('utf-8'))


def canonical(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def verify_record(record,entry=None):
    digest=canonical({k:v for k,v in record.items()if k!='public_sha256'})
    if digest!=record.get('public_sha256'):raise ValueError('public_release_remote_record_integrity_failed')
    if entry is not None and (entry.get('version_id')!=record['prediction']['version_id']
            or entry.get('public_sha256')!=digest):
        raise ValueError('public_release_remote_index_integrity_failed')


def publish_api(job,*,public_root,queue_root,api_base='http://127.0.0.1:8625',api=None,exporter=None):
    from .publication_queue import saved_publication
    from v2.pregame_system.prediction_versions import _instant
    source=saved_publication(job['source_database'],job['game_id'],job['version_id'])
    if source['payload_sha256']!=job['payload_sha256']:raise ValueError('public_release_queued_hash_changed')
    if '::postgame::' in job['version_id']:
        # The same exported-view hash is checked below; no Writer is invoked.
        postgame=source.get('public_postgame')
    else:postgame=None
    root=Path(public_root).resolve()
    if exporter is None:
        spec=importlib.util.spec_from_file_location('courtvision_saved_export',root/'publish_game.py')
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
    api=api or GitHubAPI()
    version=source['version'];gid=job['game_id'];path='public_data/games/'+gid.replace(':','_')+'.json'
    folder=Path(queue_root)/'api-exports'/job['owner_token']
    record=exporter.publish(gid,folder/'public_data',api_base)
    selected=record['prediction'].get('version') or {}
    expected_sha=source.get('prediction_payload_sha256',source['payload_sha256'])
    if selected.get('version_id')!=version['version_id']:
        return {'status':'superseded','version_id':job['version_id'],'selected_version_id':selected.get('version_id')}
    if selected.get('payload_sha256')!=expected_sha:raise ValueError('public_release_export_hash_changed')
    if source.get('editorial_sha256'):
        overlay=((record['prediction'].get('display_projection')or{}).get('portfolio_editorial')or{})
        if overlay.get('editorial_sha256')!=source['editorial_sha256']:
            raise ValueError('public_release_editorial_selection_mismatch')
    if postgame:
        actual=(record.get('postgame')or{}).get('postgame')or{}
        if any(actual.get(k)!=postgame.get(k)for k in ('version_id','article','title','score','prediction','market_reference','generated_at')):
            raise ValueError('public_release_postgame_projection_mismatch')
    verify_record(record)
    # Inspect exactly the new public material; never inspect or log auth headers.
    check=importlib.util.spec_from_file_location('courtvision_release_guard',root/'scripts/check_public_release.py')
    guard=importlib.util.module_from_spec(check);check.loader.exec_module(guard)
    findings=[];guard.inspect(path,json.dumps(record,ensure_ascii=False).encode(),findings)
    if findings:raise ValueError('public_release_private_material_detected')
    for attempt in range(3):
        head=api.head();sha=head['sha']
        index=api.read('public_data/index.json',sha)
        old_entry=next((r for r in index.get('games',[])if r['game_id']==gid),None)
        if old_entry:
            old=api.read(path,sha);verify_record(old,old_entry)
            previous=old['prediction'].get('version')or{}
            if previous.get('version_id')==version['version_id']:
                if previous.get('payload_sha256')!=expected_sha:raise ValueError('public_release_remote_version_hash_conflict')
                wanted_overlay=((record.get('prediction')or{}).get('display_projection')or{}).get('portfolio_editorial')or{}
                old_overlay=((old.get('prediction')or{}).get('display_projection')or{}).get('portfolio_editorial')or{}
                overlay_matches=not wanted_overlay or old_overlay.get('editorial_sha256')==wanted_overlay.get('editorial_sha256')
                prior_post=(old.get('postgame')or{}).get('postgame')or{}
                post_matches=not postgame or all(prior_post.get(k)==postgame.get(k)for k in
                    ('version_id','article','title','score','prediction','market_reference','generated_at'))
                if overlay_matches and post_matches:
                    return {'status':'already_published','version_id':job['version_id'],'commit':sha,
                            'transport':'github_api','generation_calls':0,'public_sha256':old['public_sha256']}
            elif previous.get('published_at') and _instant(previous['published_at'],'previous')>=_instant(version['published_at'],'published_at'):
                return {'status':'superseded','version_id':job['version_id'],'remote_version_id':previous['version_id']}
        entry={'game_id':gid,'file':path.removeprefix('public_data/'),'tipoff':record['game']['tipoff'],
            'version_id':version['version_id'],'public_sha256':record['public_sha256'],'home_record':exporter.home_record(record)}
        index['games']=[r for r in index.get('games',[])if r['game_id']!=gid]+[entry]
        index['games'].sort(key=lambda r:r['tipoff'],reverse=True)
        index['published_at']=record['publication']['exported_at']
        findings=[];guard.inspect('public_data/index.json',json.dumps(index,ensure_ascii=False).encode(),findings)
        if findings:raise ValueError('public_release_private_material_detected')
        tree=api.request('POST',PREFIX+'/git/trees',{'base_tree':head['commit']['tree']['sha'],'tree':[
            {'path':name,'mode':'100644','type':'blob','content':json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'}
            for name,value in [(path,record),('public_data/index.json',index)]]})
        commit=api.request('POST',PREFIX+'/git/commits',{'message':'Publish saved report '+gid+' '+job['version_id'],
            'tree':tree['sha'],'parents':[sha]})
        if api.head()['sha']!=sha:continue
        try:api.request('PATCH',PREFIX+'/git/refs/heads/main',{'sha':commit['sha'],'force':False})
        except RuntimeError as exc:
            if str(exc)in {'github_delivery_http_409','github_delivery_http_422'}:continue
            raise
        remote=api.read(path,commit['sha']);verify_record(remote)
        if remote['public_sha256']!=record['public_sha256']:raise ValueError('public_release_remote_verification_failed')
        return {'status':'pushed','game_id':gid,'version_id':job['version_id'],'commit':commit['sha'],
                'transport':'github_api','generation_calls':0,'deployment':'pending','public_sha256':record['public_sha256']}
    raise RuntimeError('github_delivery_concurrent_head_retry_exhausted')
