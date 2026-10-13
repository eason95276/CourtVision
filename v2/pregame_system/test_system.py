"""Offline directed tests; never spend network or model requests."""
import json
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import pytest
from . import ROOT
from .features import summary,extra_features,team_context
from .prediction import Predictor,feature_row,historical_identity,latest,local_day
from .agent import PregameAgent,QueryPlan,Session,Report,validate_availability,StructuredPlanner
from .market import normalize_odds,probability,events,match,benchmark_quotes
from .storage import BASE,save,digest,instant,now,content_hash
from .experiment import select


@pytest.fixture(scope='module')
def agent():return PregameAgent()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args,**kwargs):raise AssertionError('offline tests attempted a network call')
    monkeypatch.setattr('requests.Session.request',blocked)
    monkeypatch.setattr('urllib.request.OpenerDirector.open',blocked)


def test_pooled_rates_not_mean_of_percentages():
    rows=[]
    for made,attempt in [(0,1),(9,10)]:
        rows.append(dict(threePointersMade=made,threePointersAttempted=attempt,
            opp_threePointersMade=made,opp_threePointersAttempted=attempt,
            reboundsOffensive=2,reboundsTotal=10,opp_reboundsOffensive=3,opp_reboundsTotal=12,
            fieldGoalsAttempted=20,opp_fieldGoalsAttempted=25,turnovers=4,opp_turnovers=5,
            game_possessions=30,freeThrowsAttempted=6,opp_freeThrowsAttempted=8,pace=100,off_rating=110,def_rating=100))
    s=summary(rows)
    assert s['three']==pytest.approx(9/11)
    assert s['oreb']==pytest.approx(4/(4+18))
    assert s['dreb']==pytest.approx(16/(16+6))
    assert s['turnover']==pytest.approx(8/60)


def test_zero_denominator_is_missing():assert np.isnan(summary([])['three'])


def test_target_and_future_stats_excluded(agent):
    p=agent.predictor;g=p.games[p.games.game_id=='0022500564'];stats=p.stats
    before,_,_=extra_features(g,stats)
    poison=stats.copy();mask=poison.game_date>=g.iloc[0].game_date.normalize()
    for c in ['teamScore','threePointersMade','reboundsOffensive']:poison.loc[mask,c]=99999
    after,_,_=extra_features(g,poison)
    pd.testing.assert_frame_equal(before,after)


def test_cutoff_filters_base_and_new_features(agent):
    p=agent.predictor;game=historical_identity(p.games[p.games.game_id=='0022500564'].iloc[0].to_dict())
    row,_,audit,prior=feature_row(game,'2026-01-13T12:00:00-05:00',p.games,p.stats)
    assert prior.game_date.max()<pd.Timestamp('2026-01-13')
    assert all('0022500564' not in audit[s]['history_game_ids'] for s in ['home','away'])
    assert pd.isna(row.iloc[0].home_win)


def test_early_morning_cutoff_withholds_yesterdays_late_games():
    assert local_day('2026-01-13T01:00:00-05:00')==pd.Timestamp('2026-01-12')
    assert local_day('2026-01-13T12:00:00-05:00')==pd.Timestamp('2026-01-13')


def test_known_future_schedule_affects_load_only(agent):
    p=agent.predictor;g=next(g for g in agent.schedule if any(
        prev['game_id']!=g['game_id'] and prev['home_team_id']==g['home_team_id'] and instant(prev['scheduled_tipoff'])<instant(g['scheduled_tipoff']) for prev in agent.schedule))
    r,_,audit,_=feature_row(g,now(),p.games,p.stats,agent.schedule)
    assert audit['home'].get('scheduled_load_game_ids')
    assert r.iloc[0].home_rest_days<7
    assert all(not gid.startswith('espn:') for gid in audit['home']['history_game_ids'])


def test_snapshot_roundtrip_and_score_algebra(agent):
    p=agent.predictor;g=agent.schedule[0];result=p.predict(g,now())
    folder=Path(result['path']);inputs=json.loads((folder/'inputs.json').read_text(encoding='utf8'))
    assert content_hash(inputs)==result['snapshot']['input_hash']
    value=result['prediction'];assert 0<=value['win_probability']<=1
    assert value['expected_home_score']-value['expected_away_score']==pytest.approx(value['expected_margin'])
    assert value['expected_home_score']+value['expected_away_score']==pytest.approx(value['expected_total'])
    with pytest.raises(FileExistsError):save(folder/'snapshot.json',{})


def test_postgame_and_unqualified_optional_inputs_rejected(agent):
    g=agent.schedule[0]
    with pytest.raises(ValueError,match='cutoff_in_future'):agent.predictor.predict(g,g['scheduled_tipoff'])
    with pytest.raises(ValueError,match='unvalidated_optional'):agent.predictor.predict(g,now(),optional={'market':'unvalidated'})


def test_replay_training_boundary(agent):
    meta=agent.predictor.replay_models('2026-01-13T12:00:00-05:00')
    assert pd.Timestamp(meta['training_max_game_date'])<pd.Timestamp('2026-01-13')
    assert all(m['variant']=='baseline_replay' for m in meta['models'].values())


def test_ambiguous_scope_clears_every_old_identity(agent):
    session=Session(game=agent.schedule[0],player='Old Player',team_id=1610612738,
        cutoff='2020-01-01T00:00:00Z',prediction_snapshot={'old':1},evidence_refs=['old'])
    report,new=agent.ask('ambiguous',session,plan={'intent':'prediction'})
    assert report['analysis']['status']=='clarification'
    assert new.game is new.player is new.cutoff is new.prediction_snapshot is None
    assert new.evidence_refs==[] and new.pending_clarification


def test_game_switch_drops_old_player_and_snapshot(agent):
    old=Session(game=agent.schedule[0],player='Old Player',prediction_snapshot={'old':1})
    report,new=agent.ask('new game',old,plan={'intent':'market','game_id':agent.schedule[1]['game_id']})
    assert new.game['game_id']==agent.schedule[1]['game_id']
    assert new.player is None and new.prediction_snapshot is None
    assert report['prediction'] is None
    assert report['market']['as_of_cutoff'] is None


def test_explicit_followup_uses_current_scope(agent):
    old=Session(game=agent.schedule[0])
    report,new=agent.ask('followup',old,plan={'intent':'provenance','inherit_game':True})
    assert new.game['game_id']==old.game['game_id']
    assert [e['section'] for e in report['evidence']]==['provenance']


def test_actual_langgraph_nodes_and_tool_trace(agent):
    report,_=agent.ask('market',plan={'intent':'market','game_id':agent.schedule[0]['game_id']})
    assert 'select_evidence' in agent.graph.get_graph().nodes
    assert any(t.get('langchain_tool')=='market' for t in report['trace'])
    Report.model_validate(report)
    assert report['analysis']['player_conclusion'] is None


def test_plan_cannot_inject_facts_or_sql():
    with pytest.raises(ValueError):QueryPlan.model_validate({'intent':'prediction','home_probability':1})
    with pytest.raises(ValueError):QueryPlan.model_validate({'intent':'prediction','sections':['arbitrary_sql']})


def test_llm_budget_blocks_before_transport(tmp_path):
    for i in range(4):save(tmp_path/str(i)/'attempt.json',{})
    with pytest.raises(ValueError,match='budget_exhausted'):StructuredPlanner(tmp_path).plan('x',Session())


def test_injury_temporal_identity_and_hash_guards(tmp_path):
    raw=tmp_path/'raw';raw.write_text('test');game={'game_id':'g','home_team_id':1,'away_team_id':2}
    data={'raw_path':str(raw),'raw_sha256':digest(raw),'source_published_at':'2020-01-01T10:00:00Z',
       'records':[{'game_id':'g','team_id':1,'source_published_at':'2020-01-01T10:00:00Z'}]}
    assert validate_availability(data,game,'2020-01-01T11:00:00Z')
    with pytest.raises(ValueError,match='after_cutoff'):validate_availability(data,game,'2020-01-01T09:00:00Z')
    data['records'][0]['game_id']='other'
    with pytest.raises(ValueError,match='wrong_game'):validate_availability(data,game,'2020-01-01T11:00:00Z')
    raw.write_text('tampered')
    with pytest.raises(ValueError,match='integrity'):validate_availability(data,game,'2020-01-01T11:00:00Z')


def test_real_injury_and_exact_player_scope(agent):
    report,_=agent.ask('injury',cutoff='2026-01-13T19:00:00-05:00',replay=True,
          plan={'intent':'availability','game_id':'0022500564'})
    assert len(report['availability']['records'])==10
    assert len(report['availability']['key_player_context']['players'])==10
    from .player_context import local_context
    assert local_context({'game_id':'0022500564'},'2026-01-13T13:00:00-05:00') is None
    assert local_context({'game_id':'different'},'2026-01-13T19:00:00-05:00') is None


def test_odds_direction_provider_and_temporal_exclusion():
    path=next((BASE/'outputs/source_probe').glob('*/odds_401810419.json'))
    data=json.loads(path.read_text(encoding='utf8'));event={'game_id':'0022500564','event_id':'401810419',
        'home_team_id':1610612748,'away_team_id':1610612756,'home_espn_id':'14','away_espn_id':'21','scheduled_tipoff':'2026-01-14T00:30Z'}
    quotes=normalize_odds(data,event,{'status':'success'});close=next(r for r in quotes if r['quote_kind']=='close')
    assert close['home_spread']==-1.5 and close['expected_home_margin']==1.5
    assert close['total']==232.5 and close['provider_id']=='100'
    assert not close['eligible_prediction_input'] and close['published_at'] is None
    event['home_espn_id']='21';assert normalize_odds(data,event,{})==[]


def test_selection_ignores_diagnostic_holdout():
    rows=[]
    for season in [2022,2023,2024,2025]:
        rows.extend([dict(task='expected_margin',variant='baseline',season=season,mae=10),
                     dict(task='expected_margin',variant='matchup',season=season,mae=10 if season<2025 else 1)])
    assert select(pd.DataFrame(rows),'expected_margin')=='baseline'


def test_benchmark_excludes_forecast_providers_and_live_books():
    rows=[]
    for bid,name in [('1001','accuscore'),('200','Draft Kings - Live Odds'),('58','ESPN BET'),('40','DraftKings')]:
        for kind in ['open','close']:rows.append({'game_id':'g','provider_id':bid,'provider_name':name,'quote_kind':kind})
    result=benchmark_quotes(pd.DataFrame(rows))
    assert set(result.provider_id)=={'58'} and set(result.quote_kind)=={'open','close'}


def test_action_keeps_open_and_consensus_separate(agent):
    from .action import normalize
    from .market import cached_get
    data,receipt=cached_get('https://api.actionnetwork.com/web/v2/scoreboard/nba?date=20260113')
    lookup={('2026-01-13',1610612748,1610612756):['0022500564']}
    rows=normalize(data,receipt,lookup,{'15':'Consensus','30':'Open'})
    byid={r['provider_id']:r for r in rows}
    assert byid['15']['home_spread']==-1.5 and byid['30']['home_spread']==-1
    assert byid['15']['quote_kind']=='consensus_archive_unverified_close'
    assert not any(r['eligible_prediction_input'] for r in rows)


def test_original_assets_unchanged():
    hashes=json.loads((BASE/'outputs/preservation_before.json').read_text(encoding='utf8'))
    assert all((ROOT/path).is_file() and digest(ROOT/path)==expected for path,expected in hashes.items())


def test_insufficient_real_history_is_explicit(agent):
    gid=agent.predictor.games.iloc[0].game_id
    report,_=agent.ask('insufficient history',cutoff='2010-10-26T12:00:00-04:00',replay=True,
          plan={'intent':'prediction','game_id':gid})
    assert report['prediction'] is None and report['snapshot'] is None
    assert any(m['section']=='prediction' for m in report['missing_data'])


def test_lookup_cache_hits_existing_verified_archive_without_network(agent):
    report,_=agent.ask('reuse archive',cutoff='2026-01-13T19:00:00-05:00',replay=True,
          plan={'intent':'availability','game_id':'0022500564'},allow_lookup=True)
    assert len(report['availability']['records'])==10
    assert not any(t.get('requests',0)>0 for t in report['trace'])
