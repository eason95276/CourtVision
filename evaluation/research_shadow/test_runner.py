import ast,json
from pathlib import Path
from evaluation.research_shadow.runner import parse_feed,parse_exa,mcp_content,select_frozen_plans,PROJECT
from evaluation.research_shadow.core import compare_excerpts
from evaluation.research_shadow.test_core import GAME,CUTOFF,row
import pytest

def test_rss_metadata_is_publication_not_feed_update_or_capture():
    raw='<rss><channel><lastBuildDate>Mon, 12 Oct 2026 12:00:00 GMT</lastBuildDate><item><title>Lakers preview</title><link>https://www.espn.com/nba/story/x</link><pubDate>Tue, 06 Oct 2026 17:00:00 GMT</pubDate></item></channel></rss>'
    r=parse_feed(raw);assert len(r)==1 and r[0]['published_at']=='Tue, 06 Oct 2026 17:00:00 GMT'
    assert r[0]['timestamp_source']=='rss.pubDate'

def test_non_feed_does_not_turn_http200_into_success():
    assert parse_feed('<html><body>News page</body></html>')==[]

def test_exa_rows_keep_missing_time_and_original_urls():
    p={'game':GAME,'cutoff':CUTOFF};q={'category':'availability','query':'same frozen query'}
    text='Title: Lakers injury preview\nURL: https://www.espn.com/nba/story/x\nPublished: N/A\nAuthor: Reporter\nHighlights:\nWarriors matchup.'
    r=parse_exa(text,p,q,'2026-10-12T12:00:00Z')
    assert len(r)==1 and r[0]['published_at']is None and r[0]['cutoff']==CUTOFF and not r[0]['eligible_for_hard_fact']
    assert r[0]['query']==q['query']and r[0]['fetch_status']=='not_attempted'

def test_mcp_tool_error_is_failure_even_exit_zero():
    with pytest.raises(ValueError,match='mcp_tool_error'):
        mcp_content({'exit_code':0,'stdout':json.dumps({'isError':True,'content':[]})})

def test_excerpts_with_different_url_not_automatically_new_information():
    a=row();a['shadow_claims']=[{'quote':'Lakers coach described switching defense.','url':a['url'],'topics':['coach_tactical']}]
    b=row();b['url']='https://www.si.com/nba/another';b['shadow_claims']=[{'quote':'Lakers coach described switching defense!','url':b['url'],'topics':['coach_tactical']}]
    c=compare_excerpts([a],[b]);assert c['unique_new_exact_excerpts']==0 and c['duplicate_exact_excerpts']==1
    assert c['semantic_novel_information_count']is None

def test_three_game_selection_is_outcome_blind_and_not_golden():
    plans=select_frozen_plans();assert len(plans)==3
    assert len({p['game'][s+'_team_id']for p in plans for s in ('home','away')})==6
    assert all('evaluation'not in p['planner_request_path']and p['game']['season_start']==2026 for p in plans)
    assert all(len(p['query_rows'])==15 for p in plans)
    assert plans[0]['game']['game_id']=='espn:401902644'

def test_no_production_imports_or_model_calls_or_publication_hook():
    path=PROJECT/'evaluation/research_shadow/runner.py';tree=ast.parse(path.read_text(encoding='utf-8'))
    imports=[n.module for n in ast.walk(tree)if isinstance(n,ast.ImportFrom)]
    assert not any(m and any(bad in m for bad in ('analysis_service','forecasting','editorial','prediction_versions','web_research_agent'))for m in imports)
    assert not any(isinstance(n,ast.Attribute)and n.attr in {'_tool_calls','investigate','publish','generate','create_completion'}for n in ast.walk(tree))

def test_output_guard_rejects_existing_directory_without_network(tmp_path):
    from evaluation.research_shadow.runner import run
    with pytest.raises(ValueError,match='immutable_output_destination_exists'):run(tmp_path)

def test_output_guard_rejects_production_workspace_without_network():
    from evaluation.research_shadow.runner import run
    with pytest.raises(ValueError,match='runtime_artifacts_must_be_outside_project'):run(PROJECT/'evaluation/research_shadow/should_not_exist')
