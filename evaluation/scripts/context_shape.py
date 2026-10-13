"""Positive schema for the existing V1 representation; no data-definition changes."""
import math,re
from datetime import date

SUMMARY={'status','sample_games','wins','losses','win_pct','points_for_per_game','points_against_per_game','off_rating','def_rating','net_rating','pace','fg_pct','three_pct'}
PLAYER={'player_id','player_name','recorded_appearances','minutes_per_appearance','minutes_observed_n','historical_starts','starter_observed_n','starter_frequency','interpretation',
    *{k+'_per_appearance' for k in ('points','assists','rebounds','turnovers','steals','blocks')}}

def shape_issues(context):
    issues=[]
    def allowed(obj,keys,path):
        if not isinstance(obj,dict):issues.append('invalid_object:'+path);return False
        for key in set(obj)-keys:issues.append('unknown_nested_field:'+path+'/'+key)
        return True
    def stats(obj,path):
        if not isinstance(obj,dict):return
        for key in ('off_rating','def_rating','net_rating','pace','fg_pct','three_pct'):
            if key in obj:allowed(obj[key],{'value','observed_games'},path+'/'+key)
        n=obj.get('sample_games');wins=obj.get('wins');losses=obj.get('losses')
        if obj.get('status')=='unavailable' and n is not None and n!=0:issues.append('unavailable_sample_count:'+path)
        if isinstance(n,(int,float)) and wins is not None and losses is not None and wins+losses!=n:issues.append('sample_count_mismatch:'+path)
    m=context.get('matchup',{})
    allowed(m,{'target_game_id','game_date','season','home_team','away_team','game_type','team_a_role','team_b_role'},'matchup')
    if m.get('game_type') not in {'regular_season','preseason','playoffs','play_in'}:issues.append('invalid_target_competition')
    if context.get('team_a',{}).get('team_id') is not None and context['team_a'].get('team_id')==context.get('team_b',{}).get('team_id'):issues.append('duplicate_team_identity')
    for side in ('team_a','team_b'):
        t=context.get(side,{})
        allowed(t,{'team_id','team_name','record','recent_form','previous_season','player_history','recent_rotation_candidates'},side)
        r=t.get('record',{});allowed(r,SUMMARY|{'wins_before_game','losses_before_game','win_pct_before_game','basis'},side+'/record');stats(r,side+'/record')
        if r.get('wins') is not None and r.get('wins_before_game')!=r['wins']:issues.append('record_wins_mismatch')
        if r.get('losses') is not None and r.get('losses_before_game')!=r['losses']:issues.append('record_losses_mismatch')
        recent=t.get('recent_form',{});allowed(recent,{'recent_5','recent_10'},side+'/recent_form')
        for n in (5,10):
            w=recent.get('recent_'+str(n),{});allowed(w,SUMMARY|{'requested_games','games'},side+'/recent_'+str(n));stats(w,side+'/recent_'+str(n))
            games=w.get('games',[])
            if not isinstance(games,list):issues.append('invalid_games_list');continue
            if len(games)>n or w.get('requested_games')!=n or w.get('sample_games')!=len(games):issues.append('window_count_mismatch:'+side+'/'+str(n))
            refs=[g.get('game_ref') for g in games if isinstance(g,dict) and 'game_ref' in g]
            if len(refs)!=len(set(refs)):issues.append('duplicate_history_event:'+side+'/'+str(n))
            for g in games:allowed(g,{'game_ref','event_date','opponent_team_id','opponent','days_before_target','points_for','points_against','game_type'},side+'/history')
        prev=t.get('previous_season',{});allowed(prev,SUMMARY|{'season','season_finished_verified','full_archive_coverage_verified','league_regular_records','scope','reason','observed_rows'},side+'/previous');stats(prev,side+'/previous')
        for group in ('player_history','recent_rotation_candidates'):
            p=t.get(group,{});allowed(p,{'status','scope','basis','players'},side+'/'+group)
            for entry in p.get('players',[]):
                allowed(entry,PLAYER,side+'/'+group+'/player')
                for key in ('points','assists','rebounds','turnovers','steals','blocks'):allowed(entry.get(key+'_per_appearance',{}),{'value','observed_n'},side+'/player/'+key)
    h=context.get('head_to_head',{});allowed(h,{'status','actual_meetings','team_a_wins','team_b_wins','meetings'},'h2h')
    meetings=h.get('meetings',[])
    if h.get('actual_meetings')!=len(meetings) or h.get('team_a_wins',0)+h.get('team_b_wins',0)!=len(meetings):issues.append('h2h_count_mismatch')
    for g in meetings:allowed(g,{'game_ref','event_date','game_type','team_a_points','team_b_points','team_a_role','days_before_target'},'h2h/meeting')
    schedule=context.get('schedule_context',{});allowed(schedule,{'team_a','team_b'},'schedule')
    for side,r in schedule.items():allowed(r,{'status','days_since_previous_recorded_game','days_rest','back_to_back','games_in_previous_7_calendar_days','basis'},'schedule/'+side)
    for group in ('market','injury','research'):
        allowed(context.get(group,{}),{'status'},group)
        if context.get(group,{}).get('status')!='unavailable':issues.append('unexpected_historical_auxiliary:'+group)
    def scan(v,path=''):
        if isinstance(v,dict):
            for key,x in v.items():
                if key in {'event_date','game_date'}:
                    try:
                        if not isinstance(x,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',x):raise ValueError()
                        date.fromisoformat(x)
                    except (ValueError,TypeError):issues.append('invalid_calendar_date:'+path+'/'+key)
                scan(x,path+'/'+key)
        elif isinstance(v,list):
            for x in v:scan(x,path)
        elif isinstance(v,float) and not math.isfinite(v):issues.append('nonfinite_number:'+path)
    scan(context);return issues
