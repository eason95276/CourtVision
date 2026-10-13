import hashlib
from datetime import datetime,timezone
import pytest
from evaluation.research_shadow.core import (
    REQUIRED_FIELDS,candidate,canonical_url,deduplicate,source_tier,qualify,
    extract_article,extract_leads,metrics,aggregate,verify_snapshot,
)

GAME={'away_team':'Los Angeles Lakers','home_team':'Golden State Warriors',
      'away_team_id':1610612747,'home_team_id':1610612744,'scheduled_tipoff':'2026-10-07T02:00:00Z'}
CUTOFF='2026-10-06T18:00:00Z'
def row(**kw):
    return candidate(channel='exa',source='exa',source_type='search',title='Lakers Warriors preview',
       url='https://www.espn.com/nba/story/_/id/123/preview',cutoff=CUTOFF,
       published_at='2026-10-06T17:00:00Z',timestamp_source='exa.Published',**kw)

def test_candidate_requires_all_requested_fields():
    assert REQUIRED_FIELDS<=row().keys()

def test_tracking_dedup_does_not_drop_identity_query():
    assert canonical_url('https://www.espn.com/nba/game?gameId=123&utm_source=x#top')==canonical_url('https://espn.com/nba/game?gameId=123')
    assert canonical_url('https://espn.com/nba/game?gameId=123')!=canonical_url('https://espn.com/nba/game?gameId=124')
    a=row();b=row();b['url']+='?utm_source=x'
    d=deduplicate([a,b],existing=[a]);assert len(d)==1 and d[0]['duplicate_with_existing'] and len(d[0]['discovery_provenance'])==2

@pytest.mark.parametrize('url,tier',[
 ('https://www.nba.com/lakers/news/x','TIER_A'),('https://raptors.com/x','TIER_A'),
 ('https://www.espn.com/nba/story/x','TIER_B'),('https://www.si.com/nba/x','TIER_B'),
 ('https://nba.com.evil.example/x','TIER_C'),('https://reddit.com/r/nba/x','TIER_C')])
def test_source_tier_no_domain_lookalike(url,tier):assert source_tier(url)==tier

def test_old_publication_on_current_page_not_historical_proof():
    a=row();a.update(fetch_status='success',fetched_at='2026-10-12T12:00:00Z',timestamp_confidence='original_precise',
                       source_timestamp_verified=True)
    qualify(a,game=GAME);assert a['publication_before_cutoff'] and not a['cutoff_pass'] and not a['eligible_for_hard_fact']
    assert 'historical_snapshot_unproven' in a['rejection_reasons']

def test_provider_timestamp_is_not_original_verification():
    a=row();a.update(fetch_status='success',fetched_at='2026-10-06T17:30:00Z')
    qualify(a,game=GAME);assert not a['eligible_for_hard_fact'] and 'publication_origin_unverified' in a['rejection_reasons']

def test_date_same_day_is_not_fictitious_midnight():
    a=row();a.update(published_at='2026-10-06',timestamp_confidence='original_date',source_timestamp_verified=True,
                     fetched_at='2026-10-06T17:30:00Z',fetch_status='success')
    qualify(a,game=GAME);assert not a['publication_before_cutoff'] and not a['cutoff_pass']

def test_provider_midnight_has_unknown_precision():
    a=row();a['published_at']='2026-10-06T00:00:00Z';qualify(a,game=GAME)
    assert a['timestamp_confidence']=='provider_unverified' and not a['eligible_for_hard_fact']

def test_current_capture_cannot_claim_forged_snapshot():
    a=row();a['historical_snapshot_proof']={'verified':True,'url':a['url'],'captured_at':'2026-10-06T16:00:00Z'}
    a.update(fetch_status='success',fetched_at='2026-10-12T12:00:00Z',source_timestamp_verified=True,timestamp_confidence='original_precise')
    qualify(a,game=GAME);assert not a['cutoff_pass']

def test_snapshot_requires_pinned_receipt_and_exact_bytes(tmp_path):
    import json
    p=tmp_path/'snapshot.json';body='Lakers Warriors pregame interview.'
    receipt={'url':row()['url'],'captured_at':'2026-10-06T17:20:00Z','body':body}
    p.write_text(json.dumps(receipt),encoding='utf-8');pin=hashlib.sha256(p.read_bytes()).hexdigest()
    proof=verify_snapshot(p,pin,url=receipt['url'],body=body,cutoff=CUTOFF)
    assert proof['verified']
    assert not verify_snapshot(p,pin,url=receipt['url'],body=body+' changed',cutoff=CUTOFF)['verified']
    assert not verify_snapshot(p,'0'*64,url=receipt['url'],body=body,cutoff=CUTOFF)['verified']

def test_future_update_rejects_even_original_publication():
    a=row();a.update(updated_at='2026-10-06T19:00:00Z',fetch_status='success',fetched_at='2026-10-06T17:20:00Z',
                     source_timestamp_verified=True,timestamp_confidence='original_precise')
    qualify(a,game=GAME);assert not a['cutoff_pass']

def test_tier_c_cannot_be_hard_fact_even_time_safe():
    a=row();a.update(url='https://forum.example/preview',source_tier='TIER_C',fetch_status='success',
        fetched_at='2026-10-06T17:30:00Z',source_timestamp_verified=True,timestamp_confidence='original_precise')
    qualify(a,game=GAME);assert not a['eligible_for_hard_fact'] and 'source_tier_c' in a['rejection_reasons']

def test_original_html_metadata_not_confused_with_related_article():
    url=row()['url'];html='<title>Main Lakers preview</title><meta property="article:published_time" content="2026-10-06T17:00:00Z"><p>Lakers prepare for Warriors tonight.</p><script type="application/ld+json">[{"@type":"NewsArticle","url":"https://other.example/related","datePublished":"2026-10-12T12:00:00Z"}]</script>'
    info=extract_article(html,url);assert info['published_at']=='2026-10-06T17:00:00+00:00' and not info['updated_at']
    assert 'Lakers prepare' in info['body'] and 'datePublished' not in info['body']

def test_si_route_has_original_metadata_without_changing_production():
    info=extract_article('<meta property="article:published_time" content="2026-10-06T17:00:00Z"><p>Warriors coach discussed switching against Lakers.</p>',
      'https://www.si.com/nba/warriors/onsi/a-title')
    assert info['published_at'] and info['timestamp_source']=='html_meta.article:published_time'

def test_quotes_are_exact_body_not_search_snippets():
    body='Los Angeles Lakers will face Golden State Warriors on October 6, 2026. Their pick-and-roll matchup tests the switching defense.'
    a=row();a['fetch_status']='success';quotes=extract_leads(a,body,GAME)
    assert quotes and all(q['quote'] in body for q in quotes)
    assert all(q['assertion_kind']=='research_excerpt_not_certified_fact' for q in quotes)
    assert not extract_leads({**a,'fetch_status':'failed'},'Warriors injury snippet.',GAME)

def test_metrics_unknown_not_success_and_denominators():
    a=row();qualify(a,game=GAME);a['fetch_status']='failed';a['fetch_attempted']=True
    m=metrics([a],raw_count=2,existing=[])
    assert m['fetch_success_rate']==0 and m['duplicate_rate']==0.5 and m['hard_fact_eligible_count']==0
    assert m['cutoff_verifiable_rate']==0 and m['existing_accepted_style_evidence_count']==0
    empty=metrics([],raw_count=0,existing=[]);assert empty['fetch_success_rate'] is None and empty['published_at_coverage'] is None
    g=aggregate([m,empty]);assert g['candidate_count']==1 and g['fetch_attempt_count']==1
