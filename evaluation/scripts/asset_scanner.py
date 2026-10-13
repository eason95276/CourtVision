"""Read-only asset discovery. Counts come from schemas and contents, never directory-name guesses."""
from __future__ import annotations
import ast,csv,hashlib,json,math,os,re,sqlite3
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
EVAL=ROOT/'evaluation'
RUNTIME_DB=ROOT/'v2/frontend/output/prediction_versions.sqlite'
SKIP={'.git','.venv','node_modules','__pycache__','evaluation'}

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def canonical_hash(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def finite(value):
    try:
        number=float(value)
        return number if math.isfinite(number) else None
    except (ValueError,TypeError):return None
def instant(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError,ValueError):return None
def connect(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');return db
def resolve(ref):
    p=Path(str(ref or ''))
    for candidate in (p,ROOT/p,ROOT.parent/'courtvision'/p):
        if candidate.is_file():return candidate.resolve()
    return None

def files_in(root):
    for parent,dirs,files in os.walk(root):
        dirs[:]=[d for d in dirs if d not in SKIP]
        for name in files:yield Path(parent)/name

def gid(value):
    text=str(value or '').strip()
    if text.startswith(('espn:','nba:','unresolved:')):return text
    if text.isdigit() and len(text)<=10 and len(text)>=7:
        padded=text.zfill(10)
        if padded[:3] in {'001','002','003','004','005','006','007','008','009','152'}:return 'nba:'+padded
    return 'unresolved:'+text if text else None

def season_from_date(value):
    try:
        date=datetime.fromisoformat(str(value)[:10]);start=date.year if date.month>=7 else date.year-1
        return f'{start}-{str(start+1)[-2:]}'
    except (TypeError,ValueError):return None
def season_label(value,date=None):
    if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}',value):return value
    if str(value).isdigit() and len(str(value))==4:return f'{value}-{str(int(value)+1)[-2:]}'
    return season_from_date(date)
def competition(value):
    s=str(value or '').lower().replace(' ','').replace('_','')
    if 'preseason' in s:return 'preseason'
    if 'playin' in s:return 'play_in'
    if 'playoff' in s:return 'playoffs'
    if 'regular' in s:return 'regular_season'
    if 'allstar' in s:return 'all_star'
    return str(value) if value else None

class Assets:
    def __init__(self):
        self.games={};self.sources=[];self.db_inventory=[];self.json_assets=[];self.failures=[];self.tests=[];self.test_runs=[]
        self.aliases={};self.raw_predictions={};self.raw_postgames={};self.selected_predictions={};self.selected_postgames={}
        self.results={};self.editorials={};self.backup_versions=set();self.conflicts=[];self.unkeyed_market_rows=[]
        self.ml_rows=[];self.team_box=set();self.player_box=set();self.official_injury=set();self.archive_injury=set()
    def game(self,id,source,**fields):
        key=gid(id)
        if not key:return None
        key=self.aliases.get(key,key)
        row=self.games.setdefault(key,{'game_id':key,'source_refs':[]})
        if source not in row['source_refs']:row['source_refs'].append(source)
        for field,value in fields.items():
            if value is not None and row.get(field) is None:row[field]=value
        return row
    def sql_source(self,path,table,rows,**extra):
        self.sources.append({'path':str(path),'table':table,'rows':rows,**extra})
    def scan_databases(self,paths):
        for path in paths:
            try:
                with connect(path) as db:
                    tables=[r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    inventory={'path':str(path),'authoritative_runtime':path.resolve()==RUNTIME_DB.resolve(),'tables':[]}
                    for table in tables:
                        q='"'+table.replace('"','""')+'"'
                        columns=[r['name'] for r in db.execute(f'PRAGMA table_info({q})')]
                        count=db.execute(f'SELECT COUNT(*) FROM {q}').fetchone()[0]
                        inventory['tables'].append({'table':table,'columns':columns,'rows':count})
                    self.db_inventory.append(inventory)
                    if path.resolve()==RUNTIME_DB.resolve():self._runtime(db,path,tables)
                    elif 'prediction_versions' in tables:
                        for row in db.execute('SELECT version_id,payload_sha256 FROM prediction_versions'):
                            self.backup_versions.add((row['version_id'],row['payload_sha256']))
                    if 'player_knowledge_games' in tables:
                        for r in db.execute('SELECT * FROM player_knowledge_games'):
                            self.game(r['game_id'],f'{path}#player_knowledge_games',season=r['season'],game_date=r['game_date'],
                                home_team_id=r['home_team_id'],away_team_id=r['away_team_id'],game_type=competition(r['season_type']))
                        self.sql_source(path,'player_knowledge_games',db.execute('SELECT COUNT(*) FROM player_knowledge_games').fetchone()[0])
                    if 'player_game_facts' in tables:
                        columns={r['name'] for r in db.execute('PRAGMA table_info(player_game_facts)')}
                        core=[k for k in ('points','minutes_seconds','fgm','fga','three_pm','three_pa','ftm','fta','rebounds','assists','turnovers','steals','blocks','fouls') if k in columns]
                        nulls=' OR '.join(k+' IS NULL' for k in core)
                        for r in db.execute('SELECT game_id,COUNT(DISTINCT team_id) teams,COUNT(*) facts,SUM(CASE WHEN ('+nulls+') AND participation_status="played" THEN 1 ELSE 0 END) missing FROM player_game_facts GROUP BY game_id'):
                            if r['teams']==2 and r['missing']==0:self.player_box.add(gid(r['game_id']))
                        self.sql_source(path,'player_game_facts',db.execute('SELECT COUNT(*) FROM player_game_facts').fetchone()[0])
                    if 'dim_game' in tables:self._legacy_games(db,path)
                    if 'games' in tables and 'player_boxscores' in tables:self._detailed_games(db,path)
                    if 'injury_snapshots' in tables:
                        for r in db.execute('SELECT * FROM injury_snapshots'):
                            if (r['source']=='nba_official_injury_report_archive' and instant(r['source_published_at']) and
                                instant(r['cutoff_time']) and instant(r['source_published_at'])<=instant(r['cutoff_time']) and
                                str(r['source_url']).startswith('https://ak-static.cms.nba.com/referee/injury/Injury-Report_') and resolve(r['raw_path'])):
                                self.archive_injury.add(gid(r['game_id']))
                        self.sql_source(path,'injury_snapshots',db.execute('SELECT COUNT(*) FROM injury_snapshots').fetchone()[0],scope='archival; not capture-before-cutoff proof')
            except (sqlite3.Error,ValueError,KeyError,TypeError) as e:self.sources.append({'path':str(path),'error':type(e).__name__+':'+str(e)[:200]})
    def _runtime(self,db,path,tables):
        for r in db.execute('SELECT * FROM schedule_catalog_games'):
            key=gid(r['game_id'])
            if r['nba_game_id']:self.aliases[gid(r['nba_game_id'])]=key
            ending_year=r['season']
            explicit_season=r['season_label'] or (f'{int(ending_year)-1}-{str(ending_year)[-2:]}' if ending_year and r['provider_namespace']=='espn' else None)
            row=self.game(r['game_id'],f'{path}#schedule_catalog_games',season=explicit_season,game_date=r['scheduled_tipoff'][:10],
                home_team=r['home_team_name'],away_team=r['away_team_name'],home_team_id=r['home_team_id'],away_team_id=r['away_team_id'],
                scheduled_tipoff=r['scheduled_tipoff'],game_type=r['game_type'])
            if r['status']=='final' and finite(r['home_score']) is not None and finite(r['away_score']) is not None:
                row['ground_truth']={'home_score':r['home_score'],'away_score':r['away_score'],'actual_home_margin':r['home_score']-r['away_score'],
                    'actual_total':r['home_score']+r['away_score'],'source_ref':f'{path}#schedule_catalog_games/{r["game_id"]}'}
        self.sql_source(path,'schedule_catalog_games',db.execute('SELECT COUNT(*) FROM schedule_catalog_games').fetchone()[0])
        for table,target in [('prediction_versions',self.raw_predictions),('postgame_versions',self.raw_postgames)]:
            for r in db.execute(f'SELECT * FROM {table} ORDER BY published_at,rowid'):
                value=json.loads(r['payload_json']);valid=canonical_hash(value)==r['payload_sha256']
                target[r['version_id']]={'payload':value,'stored_hash':r['payload_sha256'],'valid_hash':valid,'source_ref':f'{path}#{table}/{r["version_id"]}'}
                if not valid:self.conflicts.append({'source':f'{path}#{table}/{r["version_id"]}','error':'payload_hash_mismatch'})
                if table=='postgame_versions' and valid:self.selected_postgames[gid(r['game_id'])]=r['version_id']
            self.sql_source(path,table,len(target))
        states={gid(r['game_id']):r for r in db.execute('SELECT * FROM game_prediction_state')}
        for key,state in states.items():
            version=state['locked_version_id'] or state['current_version_id']
            if key in self.selected_postgames:version=self.raw_postgames[self.selected_postgames[key]]['payload']['base_prediction_version_id']
            if version in self.raw_predictions and self.raw_predictions[version]['valid_hash']:self.selected_predictions[key]=version
        for r in db.execute('SELECT * FROM game_result_snapshots'):
            body=json.loads(r['payload_json'])
            if canonical_hash(body)!=r['payload_sha256']:continue
            self.results[r['result_id']]=body
            actual=body['game'];identity=body['identity'];key=gid(identity['canonical_game_id'])
            row=self.game(key,f'{path}#game_result_snapshots/{r["result_id"]}')
            if actual.get('status')=='final':row['ground_truth']={'home_score':actual['home_points'],'away_score':actual['away_points'],
                'actual_home_margin':actual['actual_home_margin'],'actual_total':actual['actual_total'],'source_ref':f'{path}#game_result_snapshots/{r["result_id"]}'}
            components=body.get('components') or {}
            if components.get('team_boxscores')=='available' and len(body.get('team_boxscores') or [])==2:self.team_box.add(key)
            if components.get('player_boxscores')=='available' and len({p.get('team_id') for p in body.get('player_boxscores') or []})==2:self.player_box.add(key)
        if 'official_injury_records' in tables:
            for r in db.execute('SELECT DISTINCT game_id FROM official_injury_records'):self.official_injury.add(gid(r['game_id']))
            self.sql_source(path,'official_injury_records',db.execute('SELECT COUNT(*) FROM official_injury_records').fetchone()[0])
    def _legacy_games(self,db,path):
        names={r['team_id']:r['team_name'] for r in db.execute('SELECT * FROM dim_team')}
        for r in db.execute('SELECT * FROM dim_game'):
            row=self.game(r['game_id'],f'{path}#dim_game',season=season_label(r['season_start'] if 'season_start' in r.keys() else None,r['game_date']),
                game_date=r['game_date'],home_team=names.get(r['home_team_id']),away_team=names.get(r['away_team_id']),
                home_team_id=r['home_team_id'],away_team_id=r['away_team_id'],game_type=competition(r['game_type']) if 'game_type' in r.keys() else None)
            if all(k in r.keys() for k in ('home_score','away_score')) and r['home_score'] is not None and r['away_score'] is not None:
                row['ground_truth']={'home_score':r['home_score'],'away_score':r['away_score'],'actual_home_margin':r['home_score']-r['away_score'],
                    'actual_total':r['home_score']+r['away_score'],'source_ref':f'{path}#dim_game/{r["game_id"]}'}
        self.sql_source(path,'dim_game',db.execute('SELECT COUNT(*) FROM dim_game').fetchone()[0])
    def _detailed_games(self,db,path):
        for r in db.execute('SELECT * FROM games'):
            fields=dict(r);key=gid(fields.get('game_id') or fields.get('gameId'))
            self.game(key,f'{path}#games',season=fields.get('season'),game_date=fields.get('game_date'),game_type=competition(fields.get('season_type')))
            for table,target in [('team_boxscores',self.team_box),('player_boxscores',self.player_box)]:
                columns={x[1] for x in db.execute('pragma table_info('+table+')')}
                game_col='game_id' if 'game_id' in columns else 'gameId';team_col='team_id' if 'team_id' in columns else 'teamId'
                if team_col in columns and db.execute(f'SELECT COUNT(DISTINCT {team_col}) FROM {table} WHERE {game_col}=?',(fields.get('game_id') or fields.get('gameId'),)).fetchone()[0]==2:target.add(key)
    def scan_csv(self,paths):
        seen=set()
        for path in paths:
            fingerprint=sha(path)
            if fingerprint in seen:
                self.sources.append({'path':str(path),'sha256':fingerprint,'duplicate_content':True});continue
            seen.add(fingerprint)
            try:
                with path.open(encoding='utf-8-sig',newline='') as f:
                    reader=csv.DictReader(f);columns=reader.fieldnames or [];n=0
                    id_field=next((k for k in ('game_id','gameId') if k in columns),None)
                    for r in reader:
                        n+=1
                        if not id_field:
                            if {'spread','total','score_home','score_away'}<=set(columns):self.unkeyed_market_rows.append({'source_file':str(path),'source_row':n+1,'has_spread':finite(r['spread']) is not None,'has_total':finite(r['total']) is not None})
                            continue
                        id=r[id_field]
                        row=self.game(id,str(path),season=season_label(r.get('season_start') or r.get('season'),r.get('game_date') or r.get('gameDate')),
                            game_date=r.get('game_date') or r.get('gameDate'),home_team=r.get('home_team') or (' '.join([r.get('hometeamCity',''),r.get('hometeamName','')]).strip() or None),
                            away_team=r.get('away_team') or (' '.join([r.get('awayteamCity',''),r.get('awayteamName','')]).strip() or None),game_type=competition(r.get('game_type') or r.get('gameType')))
                        if row is None:continue
                        home=finite(r.get('homeScore',r.get('home_score')));away=finite(r.get('awayScore',r.get('away_score')))
                        if home is not None and away is not None and home>0 and away>0 and home!=away:
                            row.setdefault('ground_truth',{'home_score':home,'away_score':away,'actual_home_margin':home-away,'actual_total':home+away,'source_ref':f'{path}:{n+1}',
                                'basis':'archival scored-game record, not an inferred live status'})
                        if 'teamScore' in columns and finite(r.get('teamId')) is not None and float(r['teamId'])>0:
                            row.setdefault('_team_stat_ids',set()).add(r.get('teamId'))
                        if {'predicted_margin','predicted_total','home_win','point_margin','total_points'}<=set(columns):
                            row['has_historical_ml_prediction']=True
                            if 'market_expected_home_margin' in columns:self.ml_rows.append({**r,'source_file':str(path),'source_row':n+1})
                        if 'bad_case_types' in columns:
                            self.failures.append({'case_id':canonical_hash({'source':fingerprint,'row':n,'type':r['bad_case_types']})[:24],
                                'game_id':row['game_id'],'stage':'historical_ml_evaluation','prediction_version_id':None,'postgame_version_id':None,
                                'failure_type':r['bad_case_types'],'severity_if_known':None,'description':'Existing exported model error case',
                                'evidence':{k:r[k] for k in ('prediction_error','margin_absolute_error','total_absolute_error','actual_result') if r.get(k)},
                                'source_file':str(path),'source_locator':f'CSV row {n+1}','root_cause_if_known':None,'fix_if_known':None,
                                'regression_status_if_known':None,'first_observed_at_if_known':None,'scope':'historical_descriptive','regression_tests':[]})
                    self.sources.append({'path':str(path),'sha256':fingerprint,'rows':n,'columns':columns})
            except (OSError,UnicodeError,csv.Error) as e:self.sources.append({'path':str(path),'error':str(e)})
        for key,row in self.games.items():
            if len(row.pop('_team_stat_ids',set()))==2:self.team_box.add(key)
    def scan_json(self,paths):
        for path in paths:
            try:
                if path.stat().st_size>32_000_000:
                    self.sources.append({'path':str(path),'status':'large_json_not_decoded','bytes':path.stat().st_size});continue
                data=read_json(path)
                if not isinstance(data,dict):continue
                schema=str(data.get('schema_version') or data.get('protocol') or '')
                kind=None
                if schema=='postgame-editorial-v1':kind='postgame_editorial'
                elif schema=='postgame-editorial-publication-v1':kind='manual_postgame_editorial'
                elif schema=='portfolio-editorial-v1':kind='portfolio_pregame_editorial'
                elif {'title','lead','article','watch_themes','confidence'}<=data.keys():kind='pregame_editorial_draft'
                elif isinstance(data.get('parsed'),dict) and {'title','lead','article','watch_themes'}<=data['parsed'].keys():kind='pregame_editorial_draft'
                draft=data.get('parsed') if isinstance(data.get('parsed'),dict) else data
                if not kind and isinstance(data.get('content'),str) and ('received_at' in data or 'usage' in data or 'model' in data):
                    try:
                        content=data['content'].strip();content=re.sub(r'^```(?:json)?\s*|\s*```$','',content)
                        decoded=json.loads(content)
                        if isinstance(decoded,dict) and {'title','lead','article','watch_themes'}<=decoded.keys():
                            kind='pregame_editorial_draft';draft=decoded
                    except ValueError:pass
                if kind:
                    id=data.get('game_id') or (data.get('provenance') or {}).get('game_id')
                    if not id and data.get('base_prediction_version_id') in self.raw_predictions:id=self.raw_predictions[data['base_prediction_version_id']]['payload']['game_id']
                    standard=ROOT/'v2/postgame_system/outputs/postgame_editorials/articles'/str(data.get('postgame_version_id'))
                    pub=kind=='postgame_editorial' and path.resolve()==standard.with_suffix('.json').resolve()
                    if kind=='portfolio_pregame_editorial':pub=path.resolve()==(ROOT/'v2/frontend/output/editorial-overrides'/str(data.get('base_prediction_version_id'))).with_suffix('.json').resolve()
                    if kind=='manual_postgame_editorial':pub=path.resolve()==(ROOT/'v2/postgame_system/outputs/postgame_editorials/manual_publications'/str(data.get('postgame_version_id'))).with_suffix('.json').resolve()
                    item={'path':str(path),'sha256':sha(path),'kind':kind,'game_id':gid(id),
                        'prediction_version_id':data.get('base_prediction_version_id'),'postgame_version_id':data.get('postgame_version_id'),
                        'generated_at':data.get('generated_at'),'model':data.get('model'),'used_by_current_reader':pub,
                        'is_manual':kind in {'manual_postgame_editorial','portfolio_pregame_editorial'},'artifact_hash_valid':None}
                    item['article_output_hash']=canonical_hash(draft.get('editorial') or {k:draft.get(k) for k in ('title','lead','article','watch_themes','player_analysis')})
                    if data.get('artifact_sha256'):item['artifact_hash_valid']=canonical_hash({k:v for k,v in data.items() if k!='artifact_sha256'})==data['artifact_sha256']
                    if pub and kind=='postgame_editorial':self.editorials[data['postgame_version_id']]={'payload':data,'path':str(path),'valid':item['artifact_hash_valid']}
                    self.json_assets.append(item)
                error=data.get('error_code') or data.get('error') or data.get('failure_reason') or data.get('error_type')
                if str(data.get('status')).lower()=='failed' and error:
                    error=json.dumps(error,ensure_ascii=False) if isinstance(error,(list,dict)) else str(error)
                    description=str(data.get('message') or data.get('reason') or error)
                    code=error.split(':',1)[0] if re.match(r'^[A-Za-z_][A-Za-z0-9_.-]*:',error) else str(data.get('error_type') or 'recorded_failure')
                    self.failures.append({'case_id':canonical_hash({'source':str(path),'error':error})[:24],
                        'game_id':gid(data.get('game_id')),'stage':data.get('stage') or data.get('failed_stage'),
                        'prediction_version_id':data.get('prediction_version_id'),'postgame_version_id':data.get('postgame_version_id'),
                        'failure_type':code,'severity_if_known':data.get('severity'),'description':description[:1500],
                        'evidence':{'error':error[:1500],'status':'failed'},'source_file':str(path),'source_locator':'$.status / $.error_type / $.message',
                        'root_cause_if_known':data.get('root_cause'),'fix_if_known':data.get('fix'),
                        'regression_status_if_known':data.get('regression_status'),'first_observed_at_if_known':data.get('failed_at') or data.get('created_at'),
                        'scope':'synthetic' if data.get('demo_fixture') or data.get('fixture') else 'recorded_failure_unclassified_execution',
                        'regression_tests':[]})
            except (OSError,ValueError,TypeError) as e:self.sources.append({'path':str(path),'status':'json_decode_error','error':str(e)[:120]})
    def scan_tests(self,paths,xmls):
        for path in paths:
            try:
                tree=ast.parse(path.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test'):
                        self.tests.append({'source_file':str(path),'name':node.name,'line':node.lineno,
                            'category':str(path.relative_to(ROOT).parent),'string_references':[v.value for v in ast.walk(node) if isinstance(v,ast.Constant) and isinstance(v.value,str) and len(v.value)<200]})
            except (OSError,UnicodeError,SyntaxError):continue
        for path in xmls:
            try:
                root=ET.parse(path).getroot();cases=root.findall('.//testcase');failed=sum(c.find('failure') is not None or c.find('error') is not None for c in cases);skip=sum(c.find('skipped') is not None for c in cases)
                self.test_runs.append({'source_file':str(path),'mtime':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                    'tests':len(cases),'passed':len(cases)-failed-skip,'failed_or_error':failed,'skipped':skip,
                    'case_names':[c.get('name') for c in cases],'scope':'recorded receipt, not rerun in this evaluation preparation'})
            except (OSError,ET.ParseError):continue
        for case in self.failures:
            code=case['failure_type']
            if len(code)<8 or code in {'ValueError','RuntimeError','OSError','TimeoutError'}:continue
            case['regression_tests']=[{'source_file':t['source_file'],'name':t['name'],'basis':'static exact failure-code reference; not per-case execution proof'} for t in self.tests if any(code in s for s in t['string_references'])]

def protection_snapshot():
    with connect(RUNTIME_DB) as db:
        tables={t:{r['version_id']:{'stored_hash':r['payload_sha256'],'payload_json_sha256':hashlib.sha256(r['payload_json'].encode()).hexdigest()}
                   for r in db.execute(f'SELECT version_id,payload_json,payload_sha256 FROM {t}')} for t in ('prediction_versions','postgame_versions')}
        attempts=[dict(r) for r in db.execute('SELECT postgame_version_id,attempt_count,status FROM postgame_writer_attempts')]
    files={}
    for folder in ('v2/frontend/output/editorial-overrides','v2/postgame_system/outputs/postgame_editorials/manual_publications','v2/postgame_system/outputs/postgame_editorials/articles'):
        for path in (ROOT/folder).glob('*.json'):files[str(path)]=sha(path)
    return {'versions':tables,'published_artifacts':files,'writer_attempts':attempts,'captured_at':datetime.now(timezone.utc).isoformat()}
