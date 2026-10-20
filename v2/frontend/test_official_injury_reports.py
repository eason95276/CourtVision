from datetime import datetime, timezone
import hashlib
import sqlite3
import pytest
from .official_injury_reports import OfficialInjuryReportStore, bind_report_games, publication_from_url

GAME={'id':'espn:401902644','tipoff':'2026-10-03T23:00:00Z',
      'home':{'id':1610612761,'name':'Toronto Raptors'},'away':{'id':1610612748,'name':'Miami Heat'}}
EARLY='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-03_03_00PM.pdf'
LATE='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-03_06_00PM.pdf'

def parsed(url,status='questionable'):
    return {'published_at':publication_from_url(url).isoformat(),'records':[
        {'game_date':'10/03/2026','game_time':'07:00 (ET)','matchup':'MIA@TOR','team':'Toronto Raptors','player_name':'Immanuel Quickley','status':status,'reason':'ankle soreness'},
        {'game_date':'10/03/2026','game_time':'07:00 (ET)','matchup':'MIA@TOR','team':'Miami Heat','player_name':'Bam Adebayo','status':'available','reason':''}]}

@pytest.mark.parametrize('status',['available','active','questionable','probable','doubtful','out'])
def test_official_status_roundtrip_without_collapsing_to_out(tmp_path,status):
    clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc)
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=clock,parser=lambda body,url:parsed(url,status))
    store.ingest(EARLY,b'%PDF-fixture '+status.encode(),extra_game=GAME)
    view=store.read_as_of(GAME,'2026-10-03T20:00:00Z')
    assert view['status']=='published' and view['coverage']=='complete'
    assert view['records'][0]['source_url']==EARLY
    assert next(row for row in view['records'] if row['player_name']=='Immanuel Quickley')['status']==status
    assert view['source_sha256']==hashlib.sha256(b'%PDF-fixture '+status.encode()).hexdigest()

def test_questionable_then_out_keeps_early_cutoff_and_rejects_posttipoff(tmp_path):
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc),
        parser=lambda body,url:parsed(url,'out' if url==LATE else 'questionable'))
    for url in [EARLY,LATE]:
        store.ingest(url,b'%PDF-fixture '+url.encode(),extra_game=GAME)
    assert store.read_as_of(GAME,'2026-10-03T20:00:00Z')['records'][-1]['status']=='questionable'
    assert store.read_as_of(GAME)['records'][-1]['status']=='out'
    assert store.read_as_of(GAME,'2026-10-03T18:00:00Z')['status']=='unavailable'
    assert store.read_as_of(GAME,'2026-10-03T20:00:00Z',captured_before_cutoff=True)['status']=='unavailable'
    with store.connect() as db:
        assert db.execute('select count(*) from official_injury_reports').fetchone()[0]==2
        with pytest.raises(sqlite3.IntegrityError): db.execute("update official_injury_records set status='out'")
    moved={**GAME,'tipoff':'2026-10-04T23:00:00Z'}
    assert store.read_as_of(moved)['status']=='unavailable'

def test_wrong_date_pair_team_or_time_cannot_bind_report():
    game={'game_id':GAME['id'],'scheduled_tipoff':GAME['tipoff'],'home_team_id':GAME['home']['id'],
          'away_team_id':GAME['away']['id'],'home_team_name':'Toronto Raptors','away_team_name':'Miami Heat'}
    for field,value in [('game_date','10/04/2026'),('matchup','TOR@MIA'),('game_time','08:00 (ET)')]:
        data=parsed(EARLY)
        for row in data['records']:row[field]=value
        assert bind_report_games(data,[game])==[]
    data=parsed(EARLY);data['records'][0]['team']='Los Angeles Lakers'
    with pytest.raises(ValueError,match='identity_unmatched'):bind_report_games(data,[game])

def test_failed_fetch_retains_history_and_records_failure(tmp_path):
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc),
        parser=lambda body,url:parsed(url),fetcher=lambda url:(_ for _ in ()).throw(OSError('offline')))
    store.ingest(EARLY,b'%PDF-fixture',extra_game=GAME)
    assert store.refresh(GAME)['status']=='failed'
    assert store.read_as_of(GAME)['status']=='published'
    with store.connect() as db: assert db.execute('select status from official_injury_fetch_runs').fetchone()[0]=='failed'

def test_discovery_only_uses_official_links_and_pdf_header_time(tmp_path):
    def fetch(url):
        if url.endswith('.pdf'):return b'%PDF-fixture'
        return (f'<a href="{EARLY}">report</a><a href="https://example.com/injury.pdf">news</a>').encode()
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:datetime(2026,10,3,21,tzinfo=timezone.utc),fetcher=fetch,parser=lambda body,url:parsed(url))
    assert store.refresh(GAME)['status']=='success'
    assert store.read_as_of(GAME)['source_url']==EARLY


def test_pdf_header_time_not_nominal_filename_is_cutoff_basis(tmp_path):
    def parser(body,url):
        value=parsed(url)
        value['published_at']='2026-10-03T19:14:00+00:00'
        return value
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc),parser=parser)
    store.ingest(EARLY,b'%PDF-fixture',extra_game=GAME)
    assert store.read_as_of(GAME,'2026-10-03T19:05:00Z')['status']=='unavailable'
    assert store.read_as_of(GAME,'2026-10-03T19:15:00Z')['source_published_at']=='2026-10-03T19:14:00+00:00'


def test_same_header_revised_body_does_not_overwrite_earlier_observation(tmp_path):
    at=[datetime(2026,10,3,19,30,tzinfo=timezone.utc)]
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:at[0],
        parser=lambda body,url:parsed(url,'out' if body.endswith(b'out') else 'questionable'))
    store.ingest(EARLY,b'%PDF-questionable',extra_game=GAME)
    at[0]=datetime(2026,10,3,22,30,tzinfo=timezone.utc)
    store.ingest(EARLY,b'%PDF-out',extra_game=GAME)
    assert store.read_as_of(GAME,'2026-10-03T20:00:00Z')['records'][-1]['status']=='questionable'
    assert store.read_as_of(GAME)['records'][-1]['status']=='out'


def test_known_url_is_refetched_and_updated_without_overwriting_history(tmp_path):
    at=[datetime(2026,10,3,19,30,tzinfo=timezone.utc)];body=[b'%PDF-questionable'];calls=[]
    def fetch(url):
        if url.endswith('.pdf'):calls.append(url);return body[0]
        return f'<a href="{EARLY}">report</a>'.encode()
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:at[0],fetcher=fetch,
        parser=lambda data,url:parsed(url,'out' if data.endswith(b'out') else 'questionable'))
    assert store.refresh(GAME)['status']=='success'
    at[0]=datetime(2026,10,3,22,30,tzinfo=timezone.utc);body[0]=b'%PDF-out'
    assert store.refresh(GAME)['status']=='success' and len(calls)==2
    assert store.read_as_of(GAME,'2026-10-03T20:00:00Z')['records'][-1]['status']=='questionable'
    assert store.read_as_of(GAME)['records'][-1]['status']=='out'


def test_continuation_page_title_does_not_replace_team_context(monkeypatch):
    import pdfplumber
    from .official_injury_reports import parse_official_pdf, ET
    from v2.scripts import import_official_injury_report as importer
    stamp = datetime(2026, 10, 20, 8, 30, tzinfo=ET)
    monkeypatch.setattr(importer, '_report_timestamp', lambda page: stamp)
    class Page:
        width = 600
        def __init__(self, rows): self.rows = rows
        def extract_table(self, settings): return self.rows
        def extract_text(self): return ''
    class Document:
        pages = [
            Page([['Game Date', 'Game Time', 'Matchup', 'Team', 'Player Name', 'Status', 'Reason'],
                  ['10/20/2026', '09:30(ET)', 'OKC@SAS', 'San Antonio Spurs', 'Harris,Tobias', 'Out', 'Calf']]),
            Page([['', '', '', 'Injury Repor', 't: 10/20/26 08:3', '0 AM', ''],
                  ['', '', '', '', 'Quaintance,Jayden', 'Out', 'Knee']]),
        ]
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(pdfplumber, 'open', lambda stream: Document())
    result = parse_official_pdf(b'%PDF-fixture',
        'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-20_08_30AM.pdf')
    assert [r['team'] for r in result['records']] == ['San Antonio Spurs', 'San Antonio Spurs']
    assert [r['player_name'] for r in result['records']] == ['Tobias Harris', 'Jayden Quaintance']
    assert all(r['matchup'] == 'OKC@SAS' for r in result['records'])


def test_parser_revision_schema_upgrade_preserves_every_old_row(tmp_path,monkeypatch):
    from . import official_injury_reports as module
    store=OfficialInjuryReportStore(tmp_path/'db.sqlite',clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc),parser=lambda body,url:parsed(url))
    monkeypatch.setattr(module,'PARSER_VERSION','fixture-parser-v1')
    first=store.ingest(EARLY,b'%PDF-fixture',extra_game=GAME)
    with store.connect() as db:
        # Recreate the exact early prototype constraint in this isolated DB.
        db.execute('CREATE TABLE old_schema AS SELECT * FROM official_injury_reports')
        db.execute('DROP TABLE official_injury_reports')
        db.execute('''CREATE TABLE official_injury_reports(report_id TEXT PRIMARY KEY,report_date TEXT,report_time TEXT,
          published_at TEXT,source_url TEXT,fetched_at TEXT,source_sha256 TEXT,raw_path TEXT,parser_version TEXT,
          parsed_json TEXT,UNIQUE(source_url,source_sha256))''')
        db.execute('INSERT INTO official_injury_reports SELECT * FROM old_schema');db.execute('DROP TABLE old_schema')
        before=tuple(db.execute('SELECT * FROM official_injury_reports').fetchone())
    monkeypatch.setattr(module,'PARSER_VERSION','fixture-parser-v2')
    second=store.ingest(EARLY,b'%PDF-fixture',extra_game=GAME)
    assert first['report_id']!=second['report_id']
    with store.connect() as db:
        assert tuple(db.execute('SELECT * FROM official_injury_reports WHERE report_id=?',(first['report_id'],)).fetchone())==before
        assert db.execute('SELECT count(*) FROM official_injury_reports').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM official_injury_report_games').fetchone()[0]==2


@pytest.mark.parametrize('team_id',range(1610612737,1610612767))
def test_all_thirty_official_team_names_match_even_without_english_catalog_name(team_id):
    from .export_data import team
    from .official_injury_reports import _abbr
    from v2.pregame_system.forecasting_pilot import TEAM_NAMES,TEAM_ALIASES
    other=1610612748 if team_id!=1610612748 else 1610612761
    rows=[]
    matchup=_abbr(team(other)['abbreviation'])+'@'+_abbr(team(team_id)['abbreviation'])
    for tid in (team_id,other):
        slug=team(tid)['abbreviation'].lower();english=TEAM_NAMES[TEAM_ALIASES.get(slug,slug)]
        rows.append({'game_date':'10/03/2026','game_time':'07:00 (ET)','matchup':matchup,
            'team':english,'player_name':'Fixture Player '+str(tid),'status':'questionable','reason':'fixture'})
    game={'game_id':'fixture:'+str(team_id),'home_team_id':team_id,'away_team_id':other,'scheduled_tipoff':GAME['tipoff']}
    result=bind_report_games({'records':rows},[game])
    assert len(result)==1 and {r['team_id'] for r in result[0]['records']}=={team_id,other}
