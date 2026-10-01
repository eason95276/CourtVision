"""Additive pregame features. All windows exclude the target calendar date.

V1's feature calculation remains unchanged. New rates pool made/attempt counts.
No player impact or injury adjustment is fabricated when longitudinal data is absent.
"""
from collections import defaultdict
import numpy as np
import pandas as pd
from . import ROOT
from courtvision.data import load_games, load_team_statistics
from courtvision.features import build_game_dataset, add_derived_team_stats


def history():
    games = load_games(ROOT/'data/raw/Games.csv')
    keys={(r.game_id,int(t)) for r in games.itertuples() for t in [r.home_team_id,r.away_team_id]}
    stats = load_team_statistics(ROOT/'data/raw/TeamStatistics.csv', eligible_game_ids=set(games.game_id),eligible_team_keys=keys)
    # A date-only cutoff is intentionally conservative; V1's shift is equivalent
    # only if no team plays twice on a calendar date.
    if stats.assign(day=stats.game_date.dt.normalize()).duplicated(['team_id','day']).any():
        raise ValueError('same_team_same_date_requires_explicit_temporal_resolution')
    return games, stats


def ratio(a, b):
    return float(a/b) if b > 0 else np.nan


def enrich(stats):
    own = add_derived_team_stats(stats)
    cols = ['game_id','team_id','reboundsOffensive','reboundsTotal','threePointersMade',
            'threePointersAttempted','fieldGoalsAttempted','freeThrowsAttempted','turnovers']
    opp = stats[cols].rename(columns={c:'opp_'+c for c in cols if c!='game_id'})
    own = own.merge(opp, left_on=['game_id','opponent_team_id'],right_on=['game_id','opp_team_id'],validate='one_to_one')
    return own.sort_values(['team_id','game_date','game_id'])


def summary(rows):
    if not rows:
        return {k:np.nan for k in ['three','allow_three','three_volume','allow_three_volume','oreb',
                'dreb','turnover','forced_turnover','ft_rate','allow_ft_rate','pace','off','def']}
    def total(k): return sum(float(r[k]) for r in rows)
    oreb=total('reboundsOffensive'); dreb=total('reboundsTotal')-oreb
    opp_oreb=total('opp_reboundsOffensive');opp_dreb=total('opp_reboundsTotal')-opp_oreb
    return dict(three=ratio(total('threePointersMade'),total('threePointersAttempted')),
        allow_three=ratio(total('opp_threePointersMade'),total('opp_threePointersAttempted')),
        three_volume=ratio(total('threePointersAttempted'),total('fieldGoalsAttempted')),
        allow_three_volume=ratio(total('opp_threePointersAttempted'),total('opp_fieldGoalsAttempted')),
        oreb=ratio(oreb,oreb+opp_dreb),dreb=ratio(dreb,dreb+opp_oreb),
        turnover=ratio(total('turnovers'),total('game_possessions')),
        forced_turnover=ratio(total('opp_turnovers'),total('game_possessions')),
        ft_rate=ratio(total('freeThrowsAttempted'),total('fieldGoalsAttempted')),
        allow_ft_rate=ratio(total('opp_freeThrowsAttempted'),total('opp_fieldGoalsAttempted')),
        pace=total('pace')/len(rows),off=total('off_rating')/len(rows),
        **{'def':total('def_rating')/len(rows)})


def team_context(prior, day, season, is_home):
    prior=[r for r in prior if r.get('_day',r['game_date'])<day]
    recent=prior[-15:]
    season_rows=[r for r in prior if r['season_start']==season]
    previous=[r for r in prior if r['season_start']==season-1]
    road=0
    if not is_home:
        for r in reversed(season_rows):
            if r['home']:break
            road+=1
    schedule={'games_in_three_days':1+sum(0<(day-r['game_date'].normalize()).days<=2 for r in prior[-4:]),
              'games_in_four_days':1+sum(0<(day-r['game_date'].normalize()).days<=3 for r in prior[-5:]),
              'consecutive_away':road+1 if not is_home else 0}
    cold={'current_games':len(season_rows),'prior_season_games':len(previous),
          **{k:v for k,v in summary(previous).items() if k in ['off','def','pace','three']}}
    return summary(recent),schedule,cold,{'recent_games':len(recent),'latest_game_date':str(prior[-1]['game_date']) if prior else None,
       'history_game_ids':[r['game_id'] for r in recent], 'rates':'pooled counts over preceding up to 15 games'}


def extra_features(games, stats):
    histories=defaultdict(list)
    for row in enrich(stats).to_dict('records'):
        row['_day']=row['game_date'].normalize();histories[row['team_id']].append(row)
    output=[];audit=[];groups=defaultdict(list)
    interactions=[('three','allow_three'),('three_volume','allow_three_volume'),('oreb','dreb'),
                  ('turnover','forced_turnover'),('ft_rate','allow_ft_rate'),('pace','pace'),('off','def')]
    for g in games.to_dict('records'):
        day=pd.Timestamp(g['game_date']).normalize()
        h=team_context(histories[g['home_team_id']],day,g['season_start'],True)
        a=team_context(histories[g['away_team_id']],day,g['season_start'],False)
        row={'game_id':g['game_id']}
        for side,own,other in [('home',h,a),('away',a,h)]:
            for attack,defense in interactions:
                # Include components alongside product so the model can reject interaction.
                for suffix,value in [('attack',own[0][attack]),('defense',other[0][defense]),
                                      ('interaction',own[0][attack]*other[0][defense])]:
                    key=f'matchup_{side}_{attack}_{suffix}';row[key]=value
                    if key not in groups['matchup']:groups['matchup'].append(key)
            for name,index in [('schedule',1),('cold',2)]:
                for key,value in own[index].items():
                    col=f'{name}_{side}_{key}';row[col]=value
                    if col not in groups[name]:groups[name].append(col)
        output.append(row);audit.append({'game_id':g['game_id'],'home':h[3],'away':a[3]})
    return pd.DataFrame(output),dict(groups),audit


def dataset(games, stats):
    base,columns=build_game_dataset(games,stats)
    extra,groups,audit=extra_features(games,stats)
    return base.merge(extra,on='game_id',validate='one_to_one'),{'baseline':columns,**groups},audit
