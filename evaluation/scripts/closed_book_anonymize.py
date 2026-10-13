"""Identity-removing transform; mappings are evaluator-private, never model input."""
import re,unicodedata
from datetime import date
from asset_scanner import canonical_hash

def anonymize(context):
    from context_shape import shape_issues
    if shape_issues(context):raise ValueError('Unknown/invalid identity-bearing context structure')
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
    leaked=identity_leaks(result,mapping)
    if leaked:raise ValueError('Anonymization identity residual: '+str(leaked[:4]))
    return result,mapping

def identity_leaks(context,mapping):
    import json
    raw=json.dumps(context,ensure_ascii=False);issues=[]
    normalized=unicodedata.normalize('NFKD',raw).casefold()
    normalized=''.join(c for c in normalized if not unicodedata.combining(c))
    collapsed=re.sub(r'[^a-z0-9]','',normalized)
    real_names=[v['name'] for v in mapping['teams'].values() if v.get('name')]+list(mapping['player_names'])
    for name in real_names:
        norm=unicodedata.normalize('NFKD',str(name)).casefold();norm=''.join(c for c in norm if not unicodedata.combining(c))
        if name and (norm in normalized or (len(re.sub(r'[^a-z0-9]','',norm))>=6 and re.sub(r'[^a-z0-9]','',norm) in collapsed)):issues.append('real_identity_name:'+name)
    aliases={'ATL','BOS','CLE','NOP','CHI','DAL','DEN','GSW','HOU','LAC','LAL','MIA','MIL','MIN','BKN','NYK','ORL','IND','PHI','PHX','POR','SAS','OKC','TOR','UTA','MEM','WAS','DET','CHA'}
    cities={'Atlanta','Boston','Cleveland','New Orleans','Chicago','Dallas','Denver','Golden State','Houston','Los Angeles','Miami','Milwaukee','Minnesota','Brooklyn','New York','Orlando','Indiana','Philadelphia','Phoenix','Portland','San Antonio','Oklahoma City','Toronto','Utah','Memphis','Washington','Detroit','Charlotte'}
    arenas={'Madison Square Garden','Chase Center','TD Garden','Barclays Center','United Center','Crypto.com Arena','Staples Center','American Airlines Center'}
    for token in aliases|cities|arenas:
        if re.search(r'(?<!\w)'+re.escape(token.casefold())+r'(?!\w)',normalized):issues.append('explicit_identity_token:'+token)
    if re.search(r'https?://|file://|[A-Za-z]:[\\/]|\.json\b|\.csv\b|\$\{?\w*(GROUND|TRUTH)',raw,re.I):issues.append('identity_path_or_url')
    for id in [mapping['target_game_id'],*mapping['players_by_id'],*mapping['teams']]:
        if '"'+id+'"' in raw:issues.append('real_identity_id:'+id)
    if re.search(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',raw):issues.append('exact_date')
    if re.search(r'(?:nba|espn)\s*:\s*\d{7,}',raw,re.I):issues.append('provider_identity_id')
    forbidden={'game_id','target_game_id','game_ref','player_id','team_id','opponent_team_id','game_date','season','espn_id','nba_id',
        'city','arena','location','venue','abbreviation','source_url','source_ref','source_metadata','filename','path','timestamp','created_at','debug','comments','ordering','season_specific_label'}
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in forbidden:issues.append('identity_key:'+k)
                walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(context);return issues
