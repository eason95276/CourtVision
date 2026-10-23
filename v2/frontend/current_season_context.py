"""Observed current-season results for narrative analysis, never numeric features."""
from __future__ import annotations
import copy,hashlib,json,sqlite3
from pathlib import Path
from v2.pregame_system.storage import content_hash,instant


def attach_current_season(game,cutoff,database):
    path=Path(database)
    if not path.is_file():return game
    bound=instant(cutoff);target=game['id'];season=int(game.get('season_start')or bound.year)
    kind=game.get('game_type','regular_season')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)as db:
        db.row_factory=sqlite3.Row
        tables={r[0]for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'schedule_catalog_games'not in tables:return game
        scheduled=[dict(r)for r in db.execute('''SELECT * FROM schedule_catalog_games WHERE season=?
            AND game_type=? AND scheduled_tipoff<? AND game_id<>? AND status NOT IN ('cancelled','postponed')''',
            (season+1,kind,cutoff,target))]
        records={}
        for row in scheduled:
            if row['status']=='final'and row['captured_at']and instant(row['captured_at'])<=bound:
                records[row['game_id']]={'game_id':row['game_id'],'played_at':row['scheduled_tipoff'],
                    'home_team_id':row['home_team_id'],'away_team_id':row['away_team_id'],
                    'home_name':row['home_team_name'],'away_name':row['away_team_name'],
                    'home_score':row['home_score'],'away_score':row['away_score'],
                    'captured_at':row['captured_at'],'source_url':row['source_url']}
        if 'game_result_snapshots'in tables:
            for row in db.execute('SELECT * FROM game_result_snapshots WHERE captured_at<=? ORDER BY captured_at',(cutoff,)):
                if hashlib.sha256(row['payload_json'].encode()).hexdigest()!=row['payload_sha256']:continue
                value=json.loads(row['payload_json']);g=value.get('game')or{}
                types={'Regular Season':'regular_season','Preseason':'preseason','Playoffs':'playoffs'}
                if (row['game_id']==target or g.get('status')!='final' or g.get('season_start')!=season
                    or types.get(g.get('season_type'))!=kind or instant(g['scheduled_tipoff'])>=bound):continue
                records[row['game_id']]={'game_id':row['game_id'],'played_at':g['scheduled_tipoff'],
                    'home_team_id':g['home_team_id'],'away_team_id':g['away_team_id'],
                    'home_name':g['home_team'],'away_name':g['away_team'],
                    'home_score':g['home_points'],'away_score':g['away_points'],
                    'source_url':((value.get('receipts')or{}).get('summary')or{}).get('url'),
                    'captured_at':row['captured_at'],'result_id':row['result_id'],'source_payload_sha256':row['payload_sha256'],
                    'players':[{k:p.get(k)for k in ('full_name','team_id','points','rebounds','assists','minutes')}
                        for p in value.get('player_boxscores')or[]]}
        champion_evidence={}
        if 'prediction_versions'in tables:
            for row in db.execute('SELECT version_id,payload_json,payload_sha256 FROM prediction_versions WHERE published_at<=?',(cutoff,)):
                if hashlib.sha256(row['payload_json'].encode()).hexdigest()!=row['payload_sha256']:continue
                v=json.loads(row['payload_json']);form=(v.get('analyst_context')or{}).get('season_form')or{}
                if form.get('season_start')!=season:continue
                for t in (form.get('teams')or{}).values():
                    history=t.get('previous_playoffs')or{}
                    if history.get('championship_clinched')is True and history.get('finals_wins')==4:
                        champion_evidence[t['team_id']]={'source_version_id':row['version_id'],
                            'season_start':season-1,'finals_wins':4,'game_ids':history.get('game_ids',[])}
    updated=copy.deepcopy(game);form=copy.deepcopy(game.get('season_form')or{})
    form.update(status='available',game_id=target,cutoff=cutoff,season_start=season,
        source='captured_results_before_cutoff',numeric_model_usage=False,
        competition_scope=kind+'; previous-season windows retain their original labels')
    teams=form.setdefault('teams',{})
    for side in ('home','away'):
        tid=int(game[side]['id']);prior=[]
        for r in records.values():
            if tid not in (r['home_team_id'],r['away_team_id']):continue
            own='home'if tid==r['home_team_id']else'away';other='away'if own=='home'else'home'
            if r[own+'_score']is None or r[other+'_score']is None:continue
            opponent=r[other+'_team_id']
            item={'game_id':r['game_id'],'played_at':r['played_at'],'team_score':r[own+'_score'],
                'team_side':own,'home_team_name':r['home_name'],'away_team_name':r['away_name'],
                'opponent_score':r[other+'_score'],'won':r[own+'_score']>r[other+'_score'],
                'opponent_team_id':opponent,'opponent_name':r[other+'_name'],
                'captured_at':r['captured_at'],'result_id':r.get('result_id'),'source_url':r.get('source_url'),
                'source_payload_sha256':r.get('source_payload_sha256')}
            if opponent in champion_evidence:item['opponent_defending_champion']=champion_evidence[opponent]
            players=[p for p in r.get('players',[])if p['team_id']==tid]
            item['key_player_results']=sorted(players,key=lambda p:p.get('points')or 0,reverse=True)[:4]
            prior.append(item)
        prior.sort(key=lambda r:r['played_at'],reverse=True)
        expected={r['game_id']for r in scheduled if tid in (r['home_team_id'],r['away_team_id'])}
        observed={r['game_id']for r in prior}
        complete=bool(expected)and expected==observed
        entry=teams.setdefault(side,{});entry.update(team_id=tid,current_season_games=len(prior)if prior else None,
            current_season_game_ids=sorted(observed),current_count_semantics='verified completed games before cutoff',
            regular_season_game_number=len(prior)+1 if complete and kind=='regular_season'else None,
            last_result=prior[0]if prior else None,recent_games=prior[:3])
        for n in (5,10,15):
            sample=prior[:n];count=len(sample)
            entry['current_last'+str(n)]={'status':'available'if count else'unavailable','games':count,
                'game_ids':[r['game_id']for r in sample],'win_rate':sum(r['won']for r in sample)/count if count else None,
                'points_for_per_game':sum(r['team_score']for r in sample)/count if count else None,
                'points_against_per_game':sum(r['opponent_score']for r in sample)/count if count else None}
    form.pop('context_hash',None);form['context_hash']=content_hash(form);updated['season_form']=form
    counts=[teams[s]['current_season_games']for s in ('home','away')]
    updated['season_phase']={'phase':'current_season'if all(n is not None and n>=15 for n in counts)else'very_early',
        'game_type':kind,'home_current_season_games':counts[0],'away_current_season_games':counts[1],
        'primary_evidence':'verified_current_season_results_then_historical_baseline',
        'global_opening_night_confirmed':False,'display_label':'本赛季已核实比赛（缺失不等于尚未比赛）'}
    return updated
