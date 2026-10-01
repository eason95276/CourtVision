"""Reuse the verified role-evidence artifact only inside its exact scope."""
import json
from . import ROOT
from .storage import instant,digest


def local_context(game,cutoff):
    from v2.scripts.validate_player_role_evidence import validate,DEFAULT_OUTPUT_DIR
    path=DEFAULT_OUTPUT_DIR/'player_evidence.json'
    evidence=json.loads(path.read_text(encoding='utf8'))
    if game['game_id']!=evidence['game_id'] or instant(cutoff)!=instant(evidence['cutoff']['utc']):return None
    result=validate()
    if result.get('errors'):return None
    manifest=json.loads((DEFAULT_OUTPUT_DIR/'source_manifest.json').read_text(encoding='utf8'))
    # Existing validator recomputes canonical response hashes inside wrappers,
    # plus byte hashes of formal outputs. A wrapper's byte hash is a different grain.
    return {'game_id':game['game_id'],'cutoff':cutoff,'path':str(path),'sha256':digest(path),
        'scope':'validated report-listed players for this exact game/cutoff only; not a league roster matrix',
        'players':[{k:p[k] for k in ['player_name','player_id','report_team','current_season'] if k in p} for p in evidence['players']],
        'forecast_point_adjustment':None,'reason':'no learned player impact model; no additional long-absence discount'}
