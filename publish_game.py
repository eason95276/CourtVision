"""Publish saved display artifacts only. Never invokes an Agent or model."""
import argparse,hashlib,json,re,urllib.request
from pathlib import Path
from datetime import datetime,timezone

GAME_KEYS=set('id tipoff status home away venue neutral_site game_type season season_start season_phase schedule_revision registered_game schedule_catalog_fact nba_game_id background matchup_history player_display_names roster_context previous_season_team_stats official_injury_report season_form postgame technical source missing public_market_reference prediction'.split())
VERSION_KEYS=set('analysis_contract_version base_input_hash game_id game_identity generated_at input_cutoff_at input_snapshot judgment public_market_reference published_at schedule_revision stage version_id payload_sha256 editorial_material sources evidence_index'.split())
JUDGMENT_KEYS=set('analysis_article analysis_article_evidence_ids confidence core_question final_prediction game_script key_matchup_variables model_risk player_analysis pregame_story roster_analysis subjective_rationale supporting_evidence uncertainty unresolved_uncertainty watch_themes winner_lean'.split())
PRIVATE=re.compile(r'(^|_)(token|secret|password|cookie|headers|raw|trace|traceback|artifact|path|request|task|debug|provenance|collector|local)(_|$)',re.I)
LOCAL=re.compile(r'(?<![A-Za-z])[A-Za-z]:[\\/]|/Users/|/home/[^/]+/|file://|localhost|127\.0\.0\.1',re.I)
def sha(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,v):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_suffix('.tmp')
    temp.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');temp.replace(p)
def clean(v):
    if isinstance(v,dict):return {k:clean(x) for k,x in v.items() if not PRIVATE.search(k) and k not in {'raw_text','raw_response','evidence_path','runtime_asset_identity','model_version','prediction_method','runtime_mode'}}
    if isinstance(v,list):return [clean(x) for x in v]
    if isinstance(v,str) and LOCAL.search(v):return '本地资料（不公开路径）'
    return v
def choose(v,keys):return clean({k:x for k,x in (v or{}).items() if k in keys})
def game_projection(raw):
    g=choose(raw,GAME_KEYS);g['source']=choose(raw.get('source'),{'schedule_captured_at'});g['missing']=g.get('missing')or[]
    g['technical']=choose(raw.get('technical'),{'cutoff','fact_context_cutoff','source_label'})
    rc=raw.get('roster_context')or{}
    g['roster_context']={'teams':{s:choose(row,{'team_id','official_team_roster','current_team_roster','current_rotation','projected_starters','confirmed_starters','projected_starters_evidence_ids','confirmed_starters_evidence_ids'}) for s,row in (rc.get('teams')or{}).items()}}
    for row in g['roster_context']['teams'].values():
        for key in ('official_team_roster','current_team_roster'):
            if isinstance(row.get(key),dict):
                roster=row[key];roster['players']=[choose(p,{'name','player_name','team_id','player_id','display_name','display_name_zh'}) for p in roster.get('players',[])]
                for field in list(roster):
                    if field not in {'status','players','team_id','source','observed_at','published_at'}:roster.pop(field)
    return g
def public_selection(s,game):
    if not s.get('version'):return {'status':s.get('status','unavailable'),'game_id':game['id'],'version_id':None,'version':None,'state_revision':s.get('state_revision',0)}
    source=s['version'];ident=source.get('game_identity')or{}
    if source['game_id']!=game['id'] or ident.get('game_id')!=game['id'] or int(ident.get('home_team_id',0))!=game['home']['id'] or int(ident.get('away_team_id',0))!=game['away']['id']:raise ValueError('prediction_identity_mismatch')
    if ident.get('scheduled_tipoff')!=game['tipoff']:raise ValueError('prediction_schedule_mismatch')
    if source.get('version_id')!=s.get('version_id') or not source.get('payload_sha256'):raise ValueError('prediction_version_hash_missing')
    if datetime.fromisoformat(source['input_cutoff_at'].replace('Z','+00:00'))>=datetime.fromisoformat(game['tipoff'].replace('Z','+00:00')):raise ValueError('prediction_not_pregame')
    v=choose(source,VERSION_KEYS);v['judgment']=choose(source.get('judgment'),JUDGMENT_KEYS)
    v['input_snapshot']=game_projection(source.get('input_snapshot')or{})
    # Retain only structural/lineup facts the existing reader consumes, never
    # redistribute full provider/article text or model trace.
    material=[]
    for row in source.get('editorial_material')or[]:
        if not isinstance(row,dict) or not row.get('pregame_plan'):continue
        item=clean(row)
        if row.get('section')=='web_context':
            value=item.get('value')or{};quote=value.get('quote','')
            names=[p.get('canonical_name_en','') for p in source.get('input_snapshot',{}).get('player_display_names',[]) if p.get('canonical_name_en') in quote]
            value['quote']=' / '.join(names);item['value']=value
        material.append(item)
    v['editorial_material']=material
    objective=[]
    for row in (source.get('analyst_context')or{}).get('objective_evidence',[]):
        if row.get('section')!='web_context':continue
        value=choose(row.get('value'),{'eligible_for_fact','team_id','claim_type','temporal_scope','source_role'})
        quote=(row.get('value')or{}).get('quote','')
        names=[p.get('canonical_name_en') for p in source.get('input_snapshot',{}).get('player_display_names',[]) if p.get('canonical_name_en') and p['canonical_name_en'] in quote]
        value['quote']=' / '.join(names);objective.append({'id':row['id'],'section':'web_context','value':value})
    v['analyst_context']={'objective_evidence':objective}
    for row in v.get('sources',[]):
        if isinstance(row,dict):
            for key in ('quote','body','snippet','content'):row.pop(key,None)
    out=choose(s,{'status','game_id','version_id','state_revision','locked_at','lock_reason','schedule_revision','fact_view','display_projection'})
    out['version']=v
    out['public_contract']='courtvision-public-v1'
    return out
def publish(game_id,destination,api_base='http://127.0.0.1:8625'):
    if not re.fullmatch(r'(espn|nba):\d+',game_id):raise ValueError('invalid_namespaced_game_id')
    def fetch(route):
        with urllib.request.urlopen(api_base+route,timeout=35) as r:return json.load(r)
    payload=fetch('/api/game?game_id='+game_id)
    if payload.get('status')!='success':raise ValueError('saved_game_unavailable')
    raw=payload['game'];game=game_projection(raw);game['homepage_visible']=True
    if not (payload.get('prediction')or{}).get('version'):game.pop('prediction',None)
    selection=public_selection(payload.get('prediction')or{},game)
    post=fetch('/api/postgame?game_id='+game_id) if (raw.get('postgame')or{}).get('status')=='available' else {'status':'unavailable','postgame':None}
    if post.get('status')=='available':
        if post['postgame'].get('game_id')!=game_id or post['postgame'].get('version_id')!=raw['postgame'].get('version_id'):raise ValueError('postgame_version_mismatch')
    market=fetch('/api/market-reference?game_id='+game_id)
    record={'contract_version':'courtvision-public-v1','game':game,'prediction':selection,'postgame':clean(post),'market':clean(market),
       'publication':{'exported_at':datetime.now(timezone.utc).isoformat(),'source_prediction_version_id':selection.get('version_id'),'source_prediction_payload_sha256':(selection.get('version')or{}).get('payload_sha256'),'mode':'saved-read-only-projection'}}
    record['public_sha256']=sha(record)
    folder=Path(destination)/'games';slug=game_id.replace(':','_');save(folder/(slug+'.json'),record)
    index_path=Path(destination)/'index.json';index=read(index_path) if index_path.exists() else {'contract_version':'courtvision-public-v1','games':[]}
    index['games']=[r for r in index['games'] if r['game_id']!=game_id]+[{'game_id':game_id,'file':'games/'+slug+'.json','tipoff':game['tipoff'],'version_id':selection.get('version_id'),'public_sha256':record['public_sha256']}]
    index['games'].sort(key=lambda r:r['tipoff'],reverse=True);index['published_at']=record['publication']['exported_at'];save(index_path,index)
    print(json.dumps({'published_game':game_id,'version':selection.get('version_id'),'postgame':post.get('status'),'destination':str(destination)},ensure_ascii=True))
    return record
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('game_id');p.add_argument('--destination',default=str(Path(__file__).resolve().parent/'public_data'));p.add_argument('--api-base',default='http://127.0.0.1:8625');a=p.parse_args();publish(a.game_id,a.destination,a.api_base)
