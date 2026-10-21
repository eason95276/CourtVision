import copy
from .player_focus import build_player_focus


def bundle():
    rows=[{'period':p,'text':f'End of the {p} Quarter'}for p in range(1,5)]
    rows.extend([
        {'id':'1','period':1,'text':'Player A misses jump shot','shootingPlay':True,'scoringPlay':False,'scoreValue':0},
        {'id':'2','period':4,'text':'Player A makes layup','shootingPlay':True,'scoringPlay':True,'scoreValue':2,
         'clock':{'displayValue':'4:20'},'scoreHome':2,'scoreAway':0},
        {'id':'3','period':4,'text':'Player B makes three point shot (Player A assists)','shootingPlay':True,
         'scoringPlay':True,'scoreValue':3,'clock':{'displayValue':'4:00'},'scoreHome':5,'scoreAway':0}])
    return {'play_by_play':rows,'player_boxscores':[{'full_name':'Player A','points':2,'fgm':1,'fga':2,'assists':1}]}


def run(b):return build_player_focus(b,{'甲球员'},lambda n:'甲球员',lambda t:t)


def test_assist_does_not_count_as_shooters_attempt_and_quarters_reconcile():
    result=run(bundle());row=next(r for r in result['player_period_stats']if r['period']==4)
    assert row['points']==2 and row['fgm']==1 and row['fga']==1 and row['assists']==1
    assert len(result['verified_game_events'])==2


def test_incomplete_or_mismatched_play_by_play_never_becomes_zero_stats():
    b=bundle();b['play_by_play']=[r for r in b['play_by_play']if r.get('id')!='1']
    assert run(b)['player_period_stats']==[]
    b=bundle();b['play_by_play']=b['play_by_play'][1:]
    assert run(b)['status']=='incomplete_play_by_play'


def test_input_is_not_mutated():
    b=bundle();before=copy.deepcopy(b);run(b);assert b==before


def test_dnp_is_not_projected_as_four_zero_point_periods():
    b=bundle();b['player_boxscores'][0].update(participation_status='did_not_play',points=0,fgm=0,fga=0,assists=0)
    assert run(b)['player_period_stats']==[]


def test_blocked_shot_is_attempt_by_shooter_not_defender():
    b=bundle();b['player_boxscores'][0]['fga']=3
    b['play_by_play'].append({'period':4,'text':"Player B blocks Player A 's layup",'shootingPlay':True,'scoringPlay':False})
    b['play_by_play'].append({'period':4,'text':"Player A blocks Player B's layup",'shootingPlay':True,'scoringPlay':False})
    row=next(r for r in run(b)['player_period_stats']if r['period']==4)
    assert row['fga']==2 and row['fgm']==1
