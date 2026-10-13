"""Maintainer-only packaging of existing models and one real feature snapshot.

No training. Full historical assets are needed only to rebuild this bundle.
"""
import json
import hashlib
from pathlib import Path
import zipfile

from .prediction import Predictor, feature_row, latest
from .residual_model import DEFAULT_METADATA
from .storage import BASE, clean, digest, content_hash
from .runtime_assets import BUNDLE_VERSION


def pack():
    target=BASE/'demo_assets'
    if (target/'runtime-v1.zip').exists():
        raise ValueError('bundle_exists; create a new version instead of overwriting')
    predictor=Predictor()
    games=json.loads(latest('future_schedule','games.json').read_text(encoding='utf8'))
    game=next(game for game in games if game['game_id']=='espn:401909088')
    cutoff='2026-09-13T20:00:00+00:00'
    row,groups,audit,prior=feature_row(game,cutoff,predictor.games,predictor.stats,games)
    feature_values=clean(row.iloc[0].to_dict())
    # Only pregame model/context fields belong in the portable input snapshot.
    columns={column for values in groups.values() for column in values}
    feature_values={key:value for key,value in feature_values.items() if key in columns}
    pure=json.loads(predictor.production.read_text(encoding='utf8'))
    residual=json.loads(DEFAULT_METADATA.read_text(encoding='utf8'))
    files={};sources={}
    for name,info in pure['models'].items():
        source=Path(info['path']);relative=f'pure/{name}.joblib'
        files[relative]=source.read_bytes();sources[relative]={'sha256':digest(source),'model_version':pure['version']}
        info['path']=name+'.joblib'
    for name,info in residual['models'].items():
        source=DEFAULT_METADATA.parent/info['path'];relative=f'residual/{name}.joblib'
        files[relative]=source.read_bytes();sources[relative]={'sha256':digest(source),'model_version':residual['version']}
        info['path']=name+'.joblib'
    residual.pop('metadata_content_hash',None)
    residual['metadata_content_hash']=content_hash(residual)
    context={'demo_runtime':True,'game':game,'cutoff':cutoff,'feature_values':feature_values,
        'groups':groups,'history_audit':audit,'history_latest_game':str(prior.game_date.max()),
        'source_hashes':{path.name:digest(path) for path in predictor.source_paths},
        'market_fixture':{'game_id':game['game_id'],'home_team_id':game['home_team_id'],
            'away_team_id':game['away_team_id'],'provider_id':'100','provider':'Draft Kings',
            'captured_at':cutoff,'source_published_at':None,'eligible_prediction_input':True,
            'expected_home_margin':-2.5,'total':226.5,'home_probability_no_vig':0.48,
            'demo_fixture':True,'source':{'kind':'synthetic offline fixture; not actual bookmaker data'}}}
    for name,value in [('pure/model.json',pure),('residual/current.json',residual),('context.json',context)]:
        files[name]=json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False).encode('utf8')
    target.mkdir(parents=True,exist_ok=True)
    bundle=target/'runtime-v1.zip'
    with zipfile.ZipFile(bundle,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,raw in sorted(files.items()):archive.writestr(name,raw)
    manifest={'version':BUNDLE_VERSION,'bundle_sha256':digest(bundle),
        'runtime':'one fixed offline scenario; trained models plus precomputed real pregame features',
        'market':'synthetic fixture for four-layer demo only; official final rejects demo runtime',
        'python':'3.13','dependencies':'../requirements.txt',
        'files':{name:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in sorted(files.items())},
        'model_sources':sources,'training_assets_required':False}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    print(json.dumps({'bundle_bytes':bundle.stat().st_size,'uncompressed_bytes':sum(map(len,files.values())),'files':len(files)}))


if __name__=='__main__':pack()
