"""Historical and future audit/Eval examples, zero LLM requests; never official."""
from .agent import PregameAgent,Session
from .storage import save,new_run


def run():
    agent=PregameAgent();folder=new_run('examples');summaries=[]
    historical=[('0022500564','2026-01-13T19:00:00-05:00'),
                ('0022500564','2026-01-13T13:00:00-05:00')]
    games=agent.predictor.games
    others=games[(games.game_date.dt.strftime('%Y-%m-%d')=='2026-01-13') & (games.game_id!='0022500564')].head(2)
    historical.extend((gid,'2026-01-13T12:00:00-05:00') for gid in others.game_id)
    historical.append((games.iloc[0].game_id,'2010-10-26T12:00:00-04:00'))
    for i,(gid,cutoff) in enumerate(historical):
        r,s=agent.ask('historical structured report',cutoff=cutoff,replay=True,plan={'intent':'report','game_id':gid})
        save(folder/f'history_{i}.json',r)
        summaries.append({'kind':'historical_replay','game_id':gid,'cutoff':cutoff,
            'prediction':r['prediction'],'injury_records':len((r['availability'] or {}).get('records',[])),
            'market':r['market'],'missing':r['missing_data']})
    for i,g in enumerate(agent.schedule[:8]):
        r,s=agent.ask('future audit report',plan={'intent':'report','game_id':g['game_id']})
        save(folder/f'future_{i}.json',r)
        summaries.append({'kind':'future_audit','game_id':g['game_id'],'home':g['home_team'],'away':g['away_team'],
            'tipoff':g['scheduled_tipoff'],'prediction':r['prediction'],'snapshot_id':r['snapshot']['snapshot_id']})
    save(folder/'summary.json',summaries);print(folder,flush=True)

if __name__=='__main__':run()
