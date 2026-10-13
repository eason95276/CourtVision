"""Prior-event-only ledger. Target numeric fields are never materialized."""
import math,sqlite3
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from .safe_csv import projected_rows
from .metadata import SOURCE_GAMES,SOURCE_TEAMS,PK,META_FIELDS,ET
from evaluation.research_shadow.core import sha

def num(x):
    try:
        v=float(x);return v if math.isfinite(v)else None
    except (ValueError,TypeError):return None
def kind(row):
    k={'Regular Season':'regular_season','Playoffs':'playoffs','Preseason':'preseason','Play-in Tournament':'play_in'}.get(row.get('gameType'),'unknown')
    if 'nba cup'in(row.get('gameLabel')or'').lower()and'championship'in(row.get('gameSubLabel')or'').lower():return 'cup_championship'
    return k
def season(day):return int(day[:4])if day[5:7]>='07'else int(day[:4])-1
def summary_value(values):return sum(values)/len(values)if values else None
def summarize(rows):
    n=len(rows);wins=sum(r['points_for']>r['points_against']for r in rows)
    s={'observed_n':n,'wins':wins,'losses':n-wins,'win_pct':wins/n if n else None,
       'points_for_per_game':summary_value([r['points_for']for r in rows]),'points_against_per_game':summary_value([r['points_against']for r in rows])}
    for label,key in (('offensive_rating','off_rating'),('defensive_rating','def_rating'),('net_rating','net_rating'),('pace','pace'),('turnover_rate','turnover_rate')):
        values=[r[key]for r in rows if r.get(key)is not None];s[label]={'value':summary_value(values),'observed_n':len(values)}
    for label,made,attempted in (('three_point_pct','three_pm','three_pa'),('field_goal_pct','fgm','fga')):
        valid=[r for r in rows if r.get(made)is not None and r.get(attempted)is not None];denom=sum(r[attempted]for r in valid)
        s[label]={'value':sum(r[made]for r in valid)/denom if denom else None,'observed_n':len(valid),'method':'pooled made/attempts'}
    reb=[r for r in rows if r.get('rebounds_total')is not None and r.get('opponent_rebounds_total')is not None]
    total=sum(r['rebounds_total']+r['opponent_rebounds_total']for r in reb)
    s['rebound_share']={'value':sum(r['rebounds_total']for r in reb)/total if total else None,'observed_n':len(reb),'method':'pooled team rebounds / (team + opponent rebounds) from historical boxscore'}
    return s
def current_windows(rows,current):
    rows=[r for r in rows if r['season']==current and r.get('kind','regular_season')=='regular_season']
    return {f'recent_{n}':{**summarize(rows[-n:]),'requested_games':n,'game_ids':[r['game_id']for r in rows[-n:]],
                           'scope':'current season only','status':'COMPLETE'if len(rows)>=n else'PARTIAL'if rows else'UNAVAILABLE'}for n in(5,10)}

class PriorLedger:
    def __init__(self,samples):
        self.samples=samples;self.target_ids={s['target_game_id'].split(':')[-1]for s in samples}
        self.metadata={};self.games={};self.teams=defaultdict(list);self.diagnostics=[]
        self.captured_at=datetime.now(timezone.utc).isoformat()
        for row in projected_rows(SOURCE_GAMES,META_FIELDS):
            gid=row['gameId'].zfill(10);day=row['gameDateTimeEst'][:10]
            if gid in self.target_ids:continue
            if day<'2010-07-01' or day>=max(s['game_date']for s in samples):continue
            row.update(nba_id=gid,day=day,season=season(day),kind=kind(row));self.metadata[gid]=row
        allowed=set(self.metadata)
        fields={'gameId','teamId','opponentTeamId','home','teamScore','opponentScore','fieldGoalsMade','fieldGoalsAttempted','threePointersMade','threePointersAttempted','freeThrowsAttempted','reboundsOffensive','reboundsTotal','turnovers','numMinutes'}
        paired=defaultdict(list)
        for index,r in enumerate(projected_rows(SOURCE_TEAMS,fields,allowed_game_ids=allowed),2):
            gid=r['gameId'].zfill(10);g=self.metadata[gid]
            pf,pa=num(r['teamScore']),num(r['opponentScore'])
            if pf is None or pa is None or pf<=0 or pa<=0 or pf==pa:continue
            team=int(r['teamId']);row={'game_id':'nba:'+gid,'event_date':g['day'],'event_time':datetime.fromisoformat(g['gameDateTimeEst']).replace(tzinfo=ET).astimezone(timezone.utc).isoformat(),
              'season':g['season'],'kind':g['kind'],'team_id':team,'opponent_id':int(r['opponentTeamId']),
              'points_for':pf,'points_against':pa,'is_home':team==int(g['hometeamId']),
              'source_ref':str(SOURCE_TEAMS)+'#gameId='+gid+'&teamId='+str(team),'status':'completed_historical_score_record'}
            for key,source in [('fga','fieldGoalsAttempted'),('fgm','fieldGoalsMade'),('three_pa','threePointersAttempted'),('three_pm','threePointersMade'),('fta','freeThrowsAttempted'),('oreb','reboundsOffensive'),('rebounds_total','reboundsTotal'),('turnovers','turnovers'),('minutes','numMinutes')]:row[key]=num(r.get(source))
            paired[gid].append(row)
        for gid,rows in paired.items():
            if len(rows)!=2 or{r['team_id']for r in rows}!={int(self.metadata[gid]['hometeamId']),int(self.metadata[gid]['awayteamId'])}:continue
            if rows[0]['points_for']!=rows[1]['points_against']or rows[1]['points_for']!=rows[0]['points_against']:continue
            poss=None
            if all(all(r.get(k)is not None for k in('fga','oreb','turnovers','fta'))for r in rows):poss=sum(r['fga']-r['oreb']+r['turnovers']+0.44*r['fta']for r in rows)/2
            for r in rows:
                r['off_rating']=100*r['points_for']/poss if poss and poss>0 else None
                r['def_rating']=100*r['points_against']/poss if poss and poss>0 else None
                r['net_rating']=r['off_rating']-r['def_rating']if r['off_rating']is not None else None
                r['pace']=240*poss/r['minutes']if poss and r.get('minutes')and r['minutes']>=240 else None
                r['turnover_rate']=100*r['turnovers']/poss if poss and r.get('turnovers')is not None else None
                r['opponent_rebounds_total']=next(other.get('rebounds_total')for other in rows if other['team_id']!=r['team_id'])
                self.teams[r['team_id']].append(r)
            self.games[gid]=rows
        for rows in self.teams.values():rows.sort(key=lambda r:(r['event_date'],r['game_id']))
        self.source_hashes={str(p):sha(p.read_bytes())for p in(SOURCE_GAMES,SOURCE_TEAMS,PK)}
    def for_sample(self,sample):
        day=sample['game_date'];current=int(sample['season'][:4]);out={};events={}
        for side in('home','away'):
            team=int(sample[side+'_team_id']);past=[r for r in self.teams[team]if r['event_date']<day]
            season_rows=[r for r in past if r['season']==current and r['kind']=='regular_season']
            expected_current=[g for g in self.metadata.values()if g['day']<day and g['season']==current and g['kind']=='regular_season'and team in{int(g['hometeamId']),int(g['awayteamId'])}]
            previous=[r for r in past if r['season']==current-1 and r['kind']=='regular_season']
            last=past[-1]if past else None;record=summarize(season_rows)
            record.update(expected_observed_games=len(expected_current),source_game_coverage=len(season_rows)/len(expected_current)if expected_current else None,
                missing_game_ids=['nba:'+g['nba_id']for g in expected_current if not any(r['game_id']=='nba:'+g['nba_id']for r in season_rows)])
            home=[r for r in season_rows if r['is_home']];away=[r for r in season_rows if not r['is_home']]
            streak=0;direction=None
            for r in reversed(season_rows):
                result='W'if r['points_for']>r['points_against']else'L'
                if direction and direction!=result:break
                direction=result;streak+=1
            record.update(home_record={'wins':sum(r['points_for']>r['points_against']for r in home),'losses':sum(r['points_for']<r['points_against']for r in home)},
                          away_record={'wins':sum(r['points_for']>r['points_against']for r in away),'losses':sum(r['points_for']<r['points_against']for r in away)},streak={'direction':direction,'games':streak})
            previous_meta=[g for g in self.metadata.values()if g['season']==current-1 and g['kind']=='regular_season'and team in{int(g['hometeamId']),int(g['awayteamId'])}]
            prev=summarize(previous);prev.update(season=f'{current-1}-{str(current)[-2:]}',expected_team_regular_games=len(previous_meta),
                full_82_games_observed=len(previous)==82,coverage=len(previous)/len(previous_meta)if previous_meta else None,
                method='possessions = mean(two teams FGA - OREB + TOV + 0.44*FTA); ORtg/DRtg per-game mean; pace 240*poss/team_minutes; estimates, not official advanced ratings')
            rest=None
            if last:
                gap=(datetime.fromisoformat(day)-datetime.fromisoformat(last['event_date'])).days
                recent4=[r for r in past if r['event_date']>=(datetime.fromisoformat(day)-timedelta(days=3)).date().isoformat()]
                rest={'days_rest':gap-1,'back_to_back':gap==1,'three_in_four':len(recent4)+1>=3,'games_in_four_days_including_target':len(recent4)+1,
                      'previous_game_id':last['game_id'],'previous_game_date':last['event_date'],'basis':'completed pre-target historical games; calendar rest not capped model feature'}
                latest_meta=max((g['day']for g in self.metadata.values()if g['day']<day and team in{int(g['hometeamId']),int(g['awayteamId'])}),default=None)
                if latest_meta and latest_meta>last['event_date']:
                    rest={'days_rest':None,'back_to_back':None,'three_in_four':None,'reason':'latest_schedule_event_missing_valid_completed_stats','latest_metadata_day':latest_meta}
            players=self.player_history(sample,team,season_rows)
            out[side]={'record':record,'recent':current_windows(past,current),'previous':prev,'rest':rest,'player_history':players}
            used=season_rows+previous+([last]if last else[])
            for r in used:events[(r['game_id'],r['team_id'])]={k:r[k]for k in('game_id','team_id','event_date','event_time','source_ref')}
        pair={int(sample['home_team_id']),int(sample['away_team_id'])}
        meetings=[]
        for gid,rows in self.games.items():
            if rows[0]['event_date']>=day or{r['team_id']for r in rows}!=pair:continue
            home=next(r for r in rows if r['is_home']);away=next(r for r in rows if not r['is_home'])
            meetings.append({'game_id':'nba:'+gid,'event_date':home['event_date'],'event_time':home['event_time'],'home_team_id':home['team_id'],'away_team_id':away['team_id'],
                  'home_points':home['points_for'],'away_points':away['points_for'],'game_type':home['kind'],'source_ref':home['source_ref']})
        meetings.sort(key=lambda r:(r['event_date'],r['game_id']));meetings=meetings[-10:]
        for r in meetings:events[(r['game_id'],0)]={k:r[k]for k in('game_id','event_date','event_time','source_ref')}
        out['head_to_head']={'meetings':meetings,'observed_n':len(meetings),'requested_games':10,'target_excluded':True}
        out['event_records']=list(events.values());return out
    def player_history(self,sample,team,season_rows):
        cutoff_day=sample['game_date'];target_ids=sorted(self.target_ids)
        marks=','.join('?'for _ in target_ids)
        # WHERE guards are applied before fetching any numeric/player/start fields.
        sql=f'''SELECT game_id,game_date,team_id,player_id,player_name,participation_status,starter,minutes_seconds,points,rebounds,assists,turnovers,steals,blocks,source_provider,source_url,collection_time_utc,receipt_id
                FROM player_game_facts WHERE substr(game_date,1,10)<? AND game_id NOT IN ({marks}) AND team_id=? ORDER BY game_date,game_id,player_id'''
        db=sqlite3.connect(PK.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        allrows=[dict(r)for r in db.execute(sql,[cutoff_day,*target_ids,team])];db.close()
        validids={r['game_id'].split(':')[-1]for r in season_rows};allrows=[r for r in allrows if str(r['game_id']).zfill(10)in validids]
        lastgames=sorted({(r['game_date'][:10],r['game_id'])for r in allrows})[-10:];lastids={g for day,g in lastgames}
        rows=[r for r in allrows if r['game_id']in lastids];by=defaultdict(list)
        for r in rows:by[r['player_id']or r['player_name']].append(r)
        players=[]
        for pid,history in by.items():
            played=[r for r in history if r['participation_status']=='played'];mins=[r['minutes_seconds']/60 for r in played if r['minutes_seconds']is not None]
            starts=[r['starter']for r in played if r['starter']is not None]
            if not played:continue
            last=played[-1];entry={'player_id':str(pid),'player_name':last['player_name'],'team_id':team,'recorded_appearances':len(played),'last_observed_before_target':last['game_date'],
                'minutes_per_appearance':{'value':summary_value(mins),'observed_n':len(mins)},'historical_starts':{'value':sum(starts),'observed_n':len(starts)},
                'recent_game_ids':['nba:'+str(r['game_id']).zfill(10)for r in played],
                'source':str(PK)+'#player_game_facts/receipt_ids='+','.join(str(r['receipt_id'])for r in played),
                'source_urls':sorted({r['source_url']for r in played}),'captured_at':max((r['collection_time_utc']or'')for r in played)or None,
                'event_time':last['game_date'],'availability':'UNKNOWN','interpretation':'historical observation != current availability; historical starts != target starters'}
            for stat in('points','rebounds','assists','turnovers','steals','blocks'):
                values=[r[stat]for r in played if r[stat]is not None];entry[stat+'_per_appearance']={'value':summary_value(values),'observed_n':len(values)}
            players.append(entry)
        players.sort(key=lambda p:(-(p['minutes_per_appearance']['value']or 0),p['player_name']))
        return {'players':players,'observed_team_games':len(lastgames),'requested_team_games':10,'scope':'current-season prior observed appearances only',
                'sql_target_exclusion':target_ids,'same_day_excluded_without_completion_time':True,'rotation_candidates':[p['player_id']for p in players[:10]]}
