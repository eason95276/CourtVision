"""Existing local market CSV, matched by ET date plus both team identities.

The user selected this dataset as the historical market reference. No claim
of an exact historical quote timestamp or strict Close is attached to it.
Only full-game spread/total/moneyline are exposed, never outcomes or half lines.
"""
import pandas as pd
from . import ROOT
from .storage import digest,clean
from courtvision.data import load_odds,TEAM_ID_TO_ABBR
from courtvision.market import transform_market_fields

PATH=ROOT/'data/raw/nba_2008-2026.csv'


class HistoricalMarket:
    def __init__(self,path=PATH):
        self.path=path;self.frame=transform_market_fields(load_odds(path));self.sha256=digest(path)

    def get(self,game):
        day=pd.Timestamp(game['scheduled_tipoff']).tz_convert('America/New_York').normalize().tz_localize(None)
        home=TEAM_ID_TO_ABBR[game['home_team_id']];away=TEAM_ID_TO_ABBR[game['away_team_id']]
        rows=self.frame[(self.frame.date==day)&(self.frame.home==home)&(self.frame.away==away)]
        if len(rows)!=1:return None
        r=rows.iloc[0]
        quote=clean({'game_id':game['game_id'],'game_date':str(day.date()),'home_team_id':game['home_team_id'],
            'away_team_id':game['away_team_id'],'favored_side':r.whos_favored,'spread':r.spread,
            'expected_home_margin':r.market_expected_home_margin,'total':r.market_total,
            'home_moneyline':r.moneyline_home,'away_moneyline':r.moneyline_away,
            'home_probability_no_vig':r.market_home_win_probability})
        if r.whos_favored not in ['home','away'] or pd.isna(r.spread) or r.spread<0:
            quote['expected_home_margin']=None
        if any(pd.isna(x) or abs(x)<100 for x in [r.moneyline_home,r.moneyline_away]):
            quote['home_probability_no_vig']=None
        return {'historical_reference':quote,'as_of_cutoff':None,
            'usage_mode':'user_selected_historical_market_dataset','exact_quote_time_verified':False,
            'source':{'path':str(self.path),'sha256':self.sha256,'row_match':{'date':str(day.date()),'home':home,'away':away}},
            'model_market_disagreement':None,
            'missing_fields':[k for k in ['spread','total','home_moneyline','away_moneyline'] if quote[k] is None]}
