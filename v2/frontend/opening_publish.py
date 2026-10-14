"""Opt-in saved FINAL export/deploy. Never calls prediction or Research."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from v2.pregame_system.prediction_versions import PredictionVersionStore,DEFAULT_DATABASE,_instant
from v2.pregame_system.storage import now,save
from .opening_night import TARGETS,ROOT


def publish_final(game_id,version_id,*,database=DEFAULT_DATABASE):
    if game_id not in TARGETS:raise ValueError('public_final_outside_opening_scope')
    destination=os.environ.get('COURTVISION_PUBLIC_PUBLISH_ROOT')
    if not destination:return {'status':'disabled'}
    root=Path(destination).resolve();store=PredictionVersionStore(database)
    with store.connect()as db:row=db.execute('SELECT payload_json FROM prediction_versions WHERE game_id=? AND version_id=?',(game_id,version_id)).fetchone()
    if row is None:raise ValueError('public_final_version_missing')
    version=json.loads(row['payload_json'])
    if version.get('stage')!='FINAL_PREGAME' or _instant(version['published_at'],'published_at')>=_instant(version['game_identity']['scheduled_tipoff'],'tipoff'):
        raise ValueError('public_final_not_legal_pregame')
    def command(args,timeout=90):
        result=subprocess.run(args,cwd=root,capture_output=True,text=True,timeout=timeout,
            env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'})
        if result.returncode:raise RuntimeError('public_release_command_failed:'+args[0])
        return result.stdout.strip()
    remote=command(['git','remote','get-url','origin'])
    if remote.rstrip('/').removesuffix('.git')!='https://github.com/eason95276/CourtVision':raise ValueError('public_release_remote_unverified')
    if command(['git','branch','--show-current'])!='main':raise ValueError('public_release_branch_unverified')
    if command(['git','status','--porcelain']):raise ValueError('public_release_workspace_dirty')
    command(['git','fetch','origin','main'])
    if command(['git','rev-parse','HEAD'])!=command(['git','rev-parse','origin/main']):raise ValueError('public_release_remote_changed')
    import publish_game
    record=publish_game.publish(game_id,root/'public_data')
    if record['prediction']['version_id']!=version_id:raise ValueError('public_release_selected_version_changed')
    command([sys.executable,'-m','unittest','discover','-s','tests/public','-q'])
    command([sys.executable,'scripts/check_public_release.py','--history'],180)
    command([sys.executable,'scripts/build_public_site.py'])
    command(['git','diff','--check'])
    paths=['public_data/index.json','public_data/games/'+game_id.replace(':','_')+'.json']
    command(['git','add','--',*paths])
    command(['git','commit','-m','Publish Opening Night FINAL '+game_id+' '+version_id])
    command(['git','push','origin','main'],120)
    result={'status':'pushed','game_id':game_id,'version_id':version_id,'commit':command(['git','rev-parse','HEAD']),
        'website':'https://nba.weitao.si','generation_calls':0,'deployment':'GitHub Pages Actions pending'}
    save(ROOT/'output/opening-night-20261014/public-final-receipts'/version_id/(now().replace(':','')+'.json'),result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('game_id');p.add_argument('version_id');p.add_argument('--database',default=str(DEFAULT_DATABASE));a=p.parse_args()
    print(json.dumps(publish_final(a.game_id,a.version_id,database=a.database)))
