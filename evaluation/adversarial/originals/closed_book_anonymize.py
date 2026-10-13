"""Identity-removing transform; mappings are evaluator-private, never model input."""
import re
from datetime import date
from asset_scanner import canonical_hash

def anonymize(context):
    target=date.fromisoformat(context['matchup']['game_date']);teams={};players={};player_names={};counts={'A':0,'B':0}
    for label,prefix in [('team_a','A'),('team_b','B')]:
        t=context[label];teams[str(t['team_id'])]={'alias':'Team '+prefix,'name':t['team_name']}
        for section in ('player_history','recent_rotation_candidates'):
            for p in t[section]['players']:
                id=str(p['player_id'])
                if id not in players:
                    counts[prefix]+=1;players[id]='Player '+prefix+str(counts[prefix])
                player_names[p['player_name']]=players[id]
    replacements={v['name']:v['alias'] for v in teams.values()};replacements.update(player_names)
    other_count=0
    def transform(value):
        nonlocal other_count
        if isinstance(value,dict):
            out={}
            for k,v in value.items():
                if k in {'target_game_id','game_id','game_ref','game_date','season','team_id','player_id'}:continue
                if k=='event_date':out['days_before_target']=(target-date.fromisoformat(v)).days;continue
                if k=='opponent_team_id':
                    id=str(v)
                    if id not in teams:
                        other_count+=1;teams[id]={'alias':'Other Team '+str(other_count),'name':None}
                    out['opponent']=teams[id]['alias'];continue
                out[k]=transform(v)
            return out
        if isinstance(value,list):return [transform(v) for v in value]
        if isinstance(value,str):
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}.*',value):raise ValueError('unhandled exact date in anonymous context')
            for real,alias in sorted(replacements.items(),key=lambda x:-len(x[0])):value=value.replace(real,alias)
        return value
    result=transform(context)
    mapping={'target_game_id':context['matchup']['target_game_id'],'target_date':target.isoformat(),'teams':teams,'players_by_id':players,
        'player_names':player_names,'original_context_sha256':canonical_hash(context),'anonymous_context_sha256':canonical_hash(result),
        'warning':'Hidden evaluator mapping; never append to model input. Statistical fingerprints can remain.'}
    return result,mapping

def identity_leaks(context,mapping):
    import json
    raw=json.dumps(context,ensure_ascii=False);issues=[]
    real_names=[v['name'] for v in mapping['teams'].values() if v.get('name')]+list(mapping['player_names'])
    for name in real_names:
        if name and name in raw:issues.append('real_identity_name:'+name)
    for id in [mapping['target_game_id'],*mapping['players_by_id'],*mapping['teams']]:
        if '"'+id+'"' in raw:issues.append('real_identity_id:'+id)
    if re.search(r'\b\d{4}-\d{2}-\d{2}\b',raw):issues.append('exact_date')
    forbidden={'game_id','target_game_id','game_ref','player_id','team_id','opponent_team_id','game_date','season','espn_id','nba_id'}
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in forbidden:issues.append('identity_key:'+k)
                walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(context);return issues
