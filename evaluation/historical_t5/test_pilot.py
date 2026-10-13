import json
from datetime import datetime,timezone
from pathlib import Path
import pytest
from evaluation.historical_t5.safe_csv import projected_rows

def test_csv_projection_never_materializes_target_result(tmp_path):
    p=tmp_path/'games.csv';p.write_text('gameId,gameDateTimeEst,homeScore,awayScore,winner,arenaName\n22500131,2025-10-29 18:30:00,TARGET_SCORE_SECRET,TARGET_SCORE_SECRET,TARGET_WINNER_SECRET,"Arena, Name"\n22500001,2025-10-22 19:00:00,100,90,home,Old Arena\n',encoding='utf-8')
    meta=list(projected_rows(p,{'gameId','gameDateTimeEst','arenaName'}))
    assert 'SECRET'not in json.dumps(meta)and meta[0]['arenaName']=='Arena, Name'
    past=list(projected_rows(p,{'gameId','homeScore','awayScore'},allowed_game_ids={'0022500001'}))
    assert len(past)==1 and past[0]['homeScore']=='100'and 'SECRET'not in json.dumps(past)

def test_csv_quotes_multiline_and_escaped_quote(tmp_path):
    p=tmp_path/'x.csv';p.write_text('gameId,teamScore,reason\n1,8,"A ""quoted""\nline"\n2,9,Other\n',encoding='utf-8')
    r=list(projected_rows(p,{'gameId','reason'}));assert r[0]['reason']=='A "quoted"\nline'and r[1]['gameId']=='2'

def test_event_windows_only_current_season_not_previous():
    from evaluation.historical_t5.ledger import current_windows
    rows=[{'season':2024,'event_date':'2025-04-12','game_id':'nba:old','points_for':100,'points_against':90},
          {'season':2025,'event_date':'2025-10-22','game_id':'nba:new','points_for':101,'points_against':90}]
    w=current_windows(rows,2025);assert w['recent_5']['observed_n']==1 and w['recent_5']['requested_games']==5
    assert w['recent_10']['game_ids']==['nba:new']

def test_stats_observed_n_missing_metric_not_window_size():
    from evaluation.historical_t5.ledger import summarize
    s=summarize([{'points_for':100,'points_against':90,'off_rating':110,'event_date':'2025-10-22','game_id':'nba:1'},
                 {'points_for':90,'points_against':100,'off_rating':None,'event_date':'2025-10-24','game_id':'nba:2'}])
    assert s['observed_n']==2 and s['offensive_rating']['observed_n']==1

def test_t5_timezone_uses_dst_not_fixed_offset():
    from evaluation.historical_t5.metadata import et_tipoff
    assert et_tipoff('2025-10-29 18:30:00')=='2025-10-29T22:30:00+00:00'
    assert et_tipoff('2025-11-05 21:00:00')=='2025-11-06T02:00:00+00:00'

def test_temporal_b_no_local_snapshot_but_original_timestamp():
    from evaluation.historical_t5.validation import qualify_article
    d=qualify_article({'source_tier':'TIER_B','published_at':'2025-10-29T18:00:00Z','updated_at':None,
        'source_timestamp_verified':True,'timestamp_source':'html_meta.article:published_time','is_pregame':True},'2025-10-29T22:25:00Z')
    assert d['temporal_grade']=='TEMPORAL_B'and d['research_fact_eligible']and not d['hard_fact_eligible']

@pytest.mark.parametrize('meta,reason',[
 ({'published_at':'2025-10-29T23:00:00Z','source_timestamp_verified':True},'published_after_cutoff'),
 ({'published_at':'2025-10-29T18:00:00Z','updated_at':'2025-10-30T02:00:00Z','source_timestamp_verified':True},'after_cutoff_modification_unresolved'),
 ({'published_at':'2025-10-29T18:00:00Z','source_timestamp_verified':False},'publication_origin_unverified')])
def test_unsafe_news_stays_lead(meta,reason):
    from evaluation.historical_t5.validation import qualify_article
    d=qualify_article({'source_tier':'TIER_B','is_pregame':True,**meta},'2025-10-29T22:25:00Z')
    assert d['temporal_grade']=='TEMPORAL_C'and not d['research_fact_eligible']and reason in d['reasons']

def test_source_c_no_hard_fact_even_date_proven():
    from evaluation.historical_t5.validation import qualify_article
    d=qualify_article({'source_tier':'TIER_C','published_at':'2025-10-29T18:00:00Z','source_timestamp_verified':True,'is_pregame':True},'2025-10-29T22:25:00Z')
    assert not d['research_fact_eligible']

def test_market_quote_after_cutoff_or_missing_time_rejected():
    from evaluation.historical_t5.validation import qualify_market
    assert not qualify_market({'quote_timestamp':None},'2025-10-29T22:25:00Z')['eligible']
    assert not qualify_market({'quote_timestamp':'2025-10-29T22:30:00Z'},'2025-10-29T22:25:00Z')['eligible']
    assert qualify_market({'quote_timestamp':'2025-10-29T22:20:00Z','source':'book','source_url':'https://book.example/quote','identity_verified':True},'2025-10-29T22:25:00Z')['eligible']

def test_lineup_requires_source_and_five_players_not_boxscore():
    from evaluation.historical_t5.validation import qualify_lineup
    base={'players':['A','B','C','D','E'],'published_at':'2025-10-29T22:20:00Z','source':'team official','source_url':'https://nba.com/news/lineup','basis':'confirmed_before_cutoff','temporal_grade':'TEMPORAL_A','identity_verified':True}
    assert qualify_lineup(base,'2025-10-29T22:25:00Z')['eligible']
    assert not qualify_lineup({**base,'basis':'target_boxscore_starters'},'2025-10-29T22:25:00Z')['eligible']
    assert not qualify_lineup({**base,'players':['A','B','C','D']},'2025-10-29T22:25:00Z')['eligible']

def test_validator_blocks_target_event_and_future_event():
    from evaluation.historical_t5.validation import validate_context
    base={'metadata':{'game_id':'nba:target','sample_id':'x'},'cutoff':{'value':'2025-10-29T22:25:00Z'},
          'matchup':{'value':{'scheduled_tipoff':'2025-10-29T22:30:00Z'}},
          'provenance':{'event_records':[{'game_id':'nba:target','event_date':'2025-10-28'}]}}
    assert not validate_context(base)['passed']
    base['provenance']['event_records']=[{'game_id':'nba:old','event_date':'2025-10-30'}]
    assert not validate_context(base)['passed']

def test_legacy_pdf_heading_not_filename_is_publication():
    from evaluation.historical_t5.injury import archive_name_matches
    assert archive_name_matches('https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-10-29_05PM.pdf','2025-10-29T17:30:00-04:00')
    assert not archive_name_matches('https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-10-29_06PM.pdf','2025-10-29T17:30:00-04:00')

def test_injury_page_header_does_not_drop_continuation_or_change_name():
    from evaluation.historical_t5.injury import validated_table_rows
    tables=[[
      ['10/29/2025','09:30(ET)','LAL@MIN','LosAngelesLakers','James, LeBron','Out','Sciatica'],
      [None,None,None,'MinnesotaTimberwolves','Clark, Jaylen','Out','Calf'],
      [None,None,None,'Injury Repor','t, 10/29/25','08:30PM',None]],
      [[None,None,None,None,'Edwards, Anthony','Out','Right Hamstring'],
       [None,None,None,None,'Dillingham, Rob','Available','Face mask']]]
    rows=validated_table_rows(tables,['Los Angeles Lakers','Minnesota Timberwolves'])
    assert len(rows)==4 and rows[0]['player_name']=='LeBron James'
    assert rows[2]['team']=='Minnesota Timberwolves'and rows[2]['availability_status']=='Out'
    assert rows[3]['availability_status']=='Available'

def test_protection_sql_tuples_survive_json_roundtrip_without_weakening_hash():
    live={'files':{'x':'hash'},'versions':{'prediction_versions':[('v','actual','stored')]}}
    persisted=json.loads(json.dumps(live))
    assert json.dumps(live,sort_keys=True)==json.dumps(persisted,sort_keys=True)
    persisted['versions']['prediction_versions'][0][1]='changed'
    assert json.dumps(live,sort_keys=True)!=json.dumps(persisted,sort_keys=True)

def test_actual_frozen_pregame_sources_expected_not_upgraded_and_confirmed_kept():
    from evaluation.historical_t5.metadata import AUDIT
    from evaluation.research_shadow.runner import read
    from evaluation.historical_t5.research import extract_lineups,roster_from_history
    samples=read(AUDIT/'selection.json')['samples']
    for index,basis in((1,'confirmed_before_cutoff'),(2,'source_expected')):
        s=samples[index];e=read(AUDIT/'research'/s['sample_id']/'result.json')['evidence'];h=read(AUDIT/(s['sample_id']+'-ledger-enriched.json'));i=read(AUDIT/(s['sample_id']+'-injury-reviewed.json'))
        roster=roster_from_history(s,h,i);lineups=extract_lineups(s,e,roster,i)
        assert all(r['basis']==basis and len(r['players'])==5 and r['cutoff_pass']for r in lineups['teams'])

def test_primary_article_excludes_later_recommendation_content():
    from evaluation.historical_t5.research import primary_body
    text='Lakers pregame lineup.\nMore news: sidebar link\nMain report.\nLatest Lakers News\nTarget postgame score.'
    body=primary_body(text)
    assert 'Main report.'in body and 'sidebar link'not in body and 'postgame score'not in body
