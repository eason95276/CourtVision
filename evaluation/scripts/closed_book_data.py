"""Past-calendar-event reconstruction. No model, article, market or injury reader."""
from __future__ import annotations
import csv,json
from collections import defaultdict
from datetime import date
from pathlib import Path
from asset_scanner import ROOT,EVAL,RUNTIME_DB,read_json,connect,gid,finite,canonical_hash,sha

def kind(value):
    return {'Regular Season':'regular_season','Playoffs':'playoffs','Preseason':'preseason','Play-in Tournament':'play_in'}.get(value,'other')

def year(day):
    return int(day[:4]) if day[5:7]>='07' else int(day[:4])-1

def past(rows,target_day,target_id):
    return [r for r in rows if r['day']<target_day and r['game_id']!=target_id]

def summary(rows):
    if not rows:return {'status':'unavailable','sample_games':0}
    n=len(rows);wins=sum(r['points_for']>r['points_against'] for r in rows)
    result={'status':'available','sample_games':n,'wins':wins,'losses':n-wins,'win_pct':round(wins/n,6),
        'points_for_per_game':round(sum(r['points_for'] for r in rows)/n,3),
        'points_against_per_game':round(sum(r['points_against'] for r in rows)/n,3)}
    for key in ('off_rating','def_rating','net_rating','pace'):
        values=[r[key] for r in rows if r.get(key) is not None]
        result[key]={'value':round(sum(values)/len(values),3) if values else None,'observed_games':len(values)}
    for label,made,attempted in [('three_pct','three_pm','three_pa'),('fg_pct','fgm','fga')]:
        valid=[r for r in rows if r.get(made) is not None and r.get(attempted) is not None]
        denom=sum(r[attempted] for r in valid)
        result[label]={'value':round(sum(r[made] for r in valid)/denom,6) if denom else None,'observed_games':len(valid)}
    return result

def public_games(rows):
    return [{'game_ref':r['game_id'],'event_date':r['day'],'opponent_team_id':r['opponent_id'],
        'points_for':r['points_for'],'points_against':r['points_against'],'game_type':r['kind']} for r in rows]

class Ledger:
    def __init__(self):
        inv=read_json(EVAL/'reports/evaluation-asset-inventory.json')
        sources=inv['tabular_sources']
        self.game_path=Path(next(r['path'] for r in sources if r.get('rows') and 'hometeamId' in r.get('columns',[]) and not r.get('duplicate_content')))
        self.team_path=Path(next(r['path'] for r in sources if r.get('rows') and 'teamScore' in r.get('columns',[]) and not r.get('duplicate_content')))
        self.pk=ROOT/'v2/data/player_knowledge.sqlite';self.games={};self.teams=defaultdict(list);self.players=defaultdict(list)
        self.source_hashes={str(p):sha(p) for p in (self.game_path,self.team_path,self.pk)}
        with self.game_path.open(encoding='utf-8-sig',newline='') as file:
            for n,r in enumerate(csv.DictReader(file),2):
                home=finite(r['homeScore']);away=finite(r['awayScore'])
                if home is None or away is None or home<=0 or away<=0 or home==away:continue
                key=gid(r['gameId']);day=(r.get('gameDateTimeEst') or r['gameDate'])[:10]
                event_kind=kind(r['gameType'])
                if 'nba cup' in str(r.get('gameLabel','')).lower() and str(r.get('gameSubLabel','')).lower()=='championship':event_kind='cup_championship'
                self.games[key]={'game_id':key,'day':day,'season':year(day),'kind':event_kind,
                    'label':r.get('gameLabel'),
                    'home_id':int(r['hometeamId']),'away_id':int(r['awayteamId']),
                    'home_name':r['hometeamCity']+' '+r['hometeamName'],'away_name':r['awayteamCity']+' '+r['awayteamName'],
                    'home_points':home,'away_points':away,'source_ref':str(self.game_path)+':'+str(n)}
        with self.team_path.open(encoding='utf-8-sig',newline='') as file:
            for n,r in enumerate(csv.DictReader(file),2):
                key=gid(r['gameId']);g=self.games.get(key)
                if not g:continue
                row={'game_id':key,'day':g['day'],'season':g['season'],'kind':g['kind'],'team_id':int(r['teamId']),
                    'opponent_id':int(r['opponentTeamId']),'points_for':finite(r['teamScore']),'points_against':finite(r['opponentScore']),
                    'source_ref':str(self.team_path)+':'+str(n)}
                if row['points_for'] is None or row['points_against'] is None:continue
                for out,src in [('fga','fieldGoalsAttempted'),('fgm','fieldGoalsMade'),('three_pa','threePointersAttempted'),('three_pm','threePointersMade'),
                    ('fta','freeThrowsAttempted'),('oreb','reboundsOffensive'),('turnovers','turnovers'),('minutes','numMinutes')]:row[out]=finite(r.get(src))
                self.teams[row['team_id']].append(row)
        # Reconcile possession estimates between the two team rows; no target row
        # is selected until the later strict past() filter.
        paired=defaultdict(list)
        for rows in self.teams.values():
            for r in rows:paired[r['game_id']].append(r)
        for rows in paired.values():
            if len(rows)!=2:continue
            if any(any(r.get(k) is None for k in ('fga','oreb','turnovers','fta')) for r in rows):continue
            poss=sum(r['fga']-r['oreb']+r['turnovers']+0.44*r['fta'] for r in rows)/2
            if poss<=0:continue
            for r in rows:
                r['off_rating']=100*r['points_for']/poss;r['def_rating']=100*r['points_against']/poss
                r['net_rating']=r['off_rating']-r['def_rating'];r['pace']=240*poss/r['minutes'] if r.get('minutes') else None
        with connect(self.pk) as c:
            for r in c.execute('SELECT * FROM player_game_facts'):
                row=dict(r);row['game_id']=gid(row['game_id']);row['day']=str(row['game_date'])[:10]
                row['source_ref']=str(self.pk)+'#player_game_facts/'+str(row['fact_id']);self.players[int(row['team_id'])].append(row)
        # Saved final result snapshots are prior-event facts only when selected by
        # strict date filtering. They are never added to a target's visible context.
        with connect(RUNTIME_DB) as c:
            for r in c.execute('SELECT result_id,payload_json,payload_sha256 FROM game_result_snapshots'):
                p=json.loads(r['payload_json'])
                if canonical_hash(p)!=r['payload_sha256']:continue
                game=p.get('game',{});identity=p.get('identity',{});key=identity.get('canonical_game_id')
                schedule=c.execute('SELECT * FROM schedule_catalog_games WHERE game_id=?',(key,)).fetchone()
                if not schedule or game.get('status')!='final':continue
                from datetime import datetime
                from zoneinfo import ZoneInfo
                day=datetime.fromisoformat(schedule['scheduled_tipoff'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                self.games[key]={'game_id':key,'day':day,'season':year(day),'kind':schedule['game_type'],'home_id':schedule['home_team_id'],'away_id':schedule['away_team_id'],
                    'home_name':schedule['home_team_name'],'away_name':schedule['away_team_name'],'home_points':game['home_points'],'away_points':game['away_points'],
                    'source_ref':str(RUNTIME_DB)+'#game_result_snapshots/'+r['result_id']}
                for side,other in [('home','away'),('away','home')]:
                    team=self.games[key][side+'_id']
                    if not any(x['game_id']==key for x in self.teams[team]):self.teams[team].append({'game_id':key,'day':day,'season':year(day),'kind':schedule['game_type'],
                        'team_id':team,'opponent_id':self.games[key][other+'_id'],'points_for':game[side+'_points'],'points_against':game[other+'_points'],'source_ref':self.games[key]['source_ref']})
        for rows in (*self.teams.values(),*self.players.values()):rows.sort(key=lambda r:(r['day'],r['game_id']))
        self.season_counts=CounterLike(self.games.values())
        self.completed_seasons={};self.completed_season_events={}
        finals=defaultdict(list)
        for g in self.games.values():
            if g.get('label')=='NBA Finals' and g['kind']=='playoffs':finals[g['season']].append(g)
        for season,rows in finals.items():
            wins=defaultdict(int)
            for r in rows:wins[r['home_id'] if r['home_points']>r['away_points'] else r['away_id']]+=1
            if max(wins.values(),default=0)>=4:
                self.completed_seasons[season]=max(r['day'] for r in rows);self.completed_season_events[season]=rows

def CounterLike(rows):
    result=defaultdict(int)
    for g in rows:
        if g['kind']=='regular_season':result[g['season']]+=1
    return result

def player_summary(rows):
    by_player=defaultdict(list)
    for r in rows:by_player[str(r['player_id'])].append(r)
    result=[]
    for id,history in by_player.items():
        played=[r for r in history if r['participation_status']=='played'];mins=[r['minutes_seconds']/60 for r in played if r['minutes_seconds'] is not None]
        if not played:continue
        starts=[r['starter'] for r in played if r['starter'] is not None]
        entry={'player_id':id,'player_name':history[-1]['player_name'],'recorded_appearances':len(played),
            'minutes_per_appearance':round(sum(mins)/len(mins),3) if mins else None,'minutes_observed_n':len(mins),
            'historical_starts':sum(bool(x) for x in starts),'starter_observed_n':len(starts),'starter_frequency':round(sum(bool(x) for x in starts)/len(starts),6) if starts else None,
            'interpretation':'Prior-game observation; not current roster, confirmed target starter or target availability'}
        for stat in ('points','assists','rebounds','turnovers','steals','blocks'):
            values=[r[stat] for r in played if r[stat] is not None]
            entry[stat+'_per_appearance']={'value':round(sum(values)/len(values),3) if values else None,'observed_n':len(values)}
        result.append(entry)
    return sorted(result,key=lambda x:(-(x['minutes_per_appearance'] if x['minutes_per_appearance'] is not None else -1),x['player_id']))

def build_context(ledger,target):
    target_id=target['game_id'];day=target['game_date'][:10];identity=ledger.games.get(target_id)
    if identity:day=identity['day']
    season=year(day);provenance={'schema_version':'closed-book-provenance-v1','target_game_id':target_id,'target_event_time':day,
        'comparison_precision':'source calendar date; source Games.gameDateTimeEst; same-day excluded; no invented UTC cutoff',
        'source_hashes':ledger.source_hashes,'fields':{}}
    ctx={'matchup':{'target_game_id':target_id,'game_date':day,'season':str(season)+'-'+str(season+1)[-2:],
        'home_team':target['home_team'],'away_team':target['away_team'],'game_type':target['game_type'],'team_a_role':'home','team_b_role':'away'},
        'market':{'status':'unavailable'},'injury':{'status':'unavailable'},'research':{'status':'unavailable'},
        'known_missing_information':['historical_market','historical_injury','archived_news','target_roster','confirmed_target_starters','target_availability'],
        'task_mode':'controlled_closed_book_no_tools; historical event-time reconstruction, not strict replay or forward validation'}
    field_rows=defaultdict(list);field_values=defaultdict(dict);prior_by_team={}
    for side,label in [('home','team_a'),('away','team_b')]:
        team=int(target[side+'_team_id']);prior=past(ledger.teams.get(team,[]),day,target_id)
        prior=[r for r in prior if r['kind'] in {'regular_season','playoffs','play_in','preseason','cup_championship'}];prior_by_team[team]=prior
        # Recent form uses competitive NBA events, or preseason-only for a preseason target.
        allowed={'preseason'} if target['game_type']=='preseason' else {'regular_season','playoffs','play_in'}
        recent=[r for r in prior if r['kind'] in allowed]
        recordrows=[r for r in prior if r['season']==season and r['kind']==target['game_type']]
        record=summary(recordrows)
        record.update(wins_before_game=record.get('wins'),losses_before_game=record.get('losses'),win_pct_before_game=record.get('win_pct'))
        recentvalue={}
        for n in (5,10):
            used=recent[-n:];recentvalue['recent_'+str(n)]={**summary(used),'requested_games':n,'games':public_games(used)};field_rows['recent_form'].extend(used)
        previous=[r for r in prior if r['season']==season-1 and r['kind']=='regular_season']
        # Modern 2024/2025 previous seasons have a documented full NBA regular schedule.
        # Never call a partial season complete merely because some rows exist.
        complete=ledger.season_counts.get(season-1)==1230 and len(previous)==82
        finished_day=getattr(ledger,'completed_seasons',{}).get(season-1)
        finished=bool(finished_day and finished_day<day)
        prev={**summary(previous),'season':str(season-1)+'-'+str(season)[-2:],'season_finished_verified':finished,
            'full_archive_coverage_verified':complete,'league_regular_records':ledger.season_counts.get(season-1),
            'scope':'Observed regular-season games of the completed previous season; sample_games is actual N, not an assumed 82'}
        if not finished:prev={'status':'unavailable','reason':'Previous season completion not verified','observed_rows':len(previous)};previous=[]
        elif previous and not complete:prev['status']='partial';prev['reason']='Completed season, incomplete source archive; metrics describe observed games only'
        ph=past(ledger.players.get(team,[]),day,target_id)
        games=sorted({(r['day'],r['game_id']) for r in ph})[-10:];keys={key for _,key in games};ph=[r for r in ph if r['game_id'] in keys]
        history=player_summary(ph)
        rotation=history[:10] if any(p['minutes_observed_n'] for p in history) else []
        value={'team_id':team,'team_name':target[side+'_team'],'record':record,'recent_form':recentvalue,'previous_season':prev,
            'player_history':{'status':'available' if history else 'unavailable','scope':'Up to last 10 recorded team games; archive coverage may be incomplete','players':history},
            'recent_rotation_candidates':{'status':'available' if rotation else 'unavailable','basis':'Historical appearances, observed minutes and starting frequency; not target-game lineup','players':rotation}}
        ctx[label]=value
        for field,key,used in [('team_record','record',recordrows),('recent_form','recent_form',[]),('previous_season_metrics','previous_season',previous),
            ('player_history','player_history',ph),('rotation_candidates','recent_rotation_candidates',ph if rotation else [])]:
            field_values[field][label]=value[key];field_rows[field].extend(used)
        if finished:field_rows['previous_season_metrics'].extend(getattr(ledger,'completed_season_events',{}).get(season-1,[]))
    a=int(target['home_team_id']);b=int(target['away_team_id'])
    meetings=[g for g in ledger.games.values() if g['day']<day and g['game_id']!=target_id and {g['home_id'],g['away_id']}=={a,b} and g['kind'] in {'regular_season','playoffs','play_in','preseason','cup_championship'}]
    meetings=sorted(meetings,key=lambda g:(g['day'],g['game_id']))[-10:]
    h2h=[]
    for g in meetings:
        pa=g['home_points'] if g['home_id']==a else g['away_points'];pb=g['away_points'] if g['home_id']==a else g['home_points']
        h2h.append({'game_ref':g['game_id'],'event_date':g['day'],'game_type':g['kind'],'team_a_points':pa,'team_b_points':pb,'team_a_role':'home' if g['home_id']==a else 'away'})
    ctx['head_to_head']={'status':'available' if meetings else 'unavailable','actual_meetings':len(meetings),'team_a_wins':sum(r['team_a_points']>r['team_b_points'] for r in h2h),
        'team_b_wins':sum(r['team_b_points']>r['team_a_points'] for r in h2h),'meetings':h2h}
    field_values['h2h']=ctx['head_to_head'];field_rows['h2h']=meetings
    schedule={}
    for team,label in [(a,'team_a'),(b,'team_b')]:
        prior=prior_by_team[team];last=prior[-1] if prior else None
        delta=(date.fromisoformat(day)-date.fromisoformat(last['day'])).days if last else None
        schedule[label]={'status':'available' if last else 'unavailable','days_since_previous_recorded_game':delta,
            'days_rest':max(delta-1,0) if delta is not None else None,'back_to_back':delta==1 if delta is not None else None,
            'games_in_previous_7_calendar_days':sum(0<(date.fromisoformat(day)-date.fromisoformat(r['day'])).days<=7 for r in prior),
            'basis':'Observed completed-game dates only; not a complete planned schedule or model capped rest flag'}
        field_rows['rest'].extend(prior[-1:] + [r for r in prior if 0<(date.fromisoformat(day)-date.fromisoformat(r['day'])).days<=7])
    ctx['schedule_context']=schedule;field_values['rest']=schedule
    for field,value in field_values.items():
        source_events={ (r['game_id'],r['day'],r['source_ref']):{'game_id':r['game_id'],'event_time':r['day'],'source_ref':r['source_ref']} for r in field_rows[field] }
        events=list(source_events.values())
        provenance['fields'][field]={'field':field,'event_time_rule':'source_calendar_date < target_source_calendar_date; target ID excluded',
            'max_source_event_time':max((e['event_time'] for e in events),default=None),'future_rows_used':0,'target_rows_used':0,
            'source_events':events,'field_value_sha256':canonical_hash(value)}
    return ctx,provenance
