"""Bounded P0-first discovery; original-source A/B/C, no LLM extraction."""
import concurrent.futures,json,re
from pathlib import Path
from urllib.parse import urlparse
from evaluation.research_shadow.core import canonical_url,source_tier,extract_article,sha,stamp,mentions,aliases
from evaluation.research_shadow.runner import CLI,process,http,write
from .validation import qualify_article,qualify_lineup

UNSAFE_ROUTE=re.compile(r'/game/|/gametracker/|box[-_]?score|scoreboard|recap|results|live-score|play-by-play',re.I)
def primary_body(body):
    lines=[]
    for line in body.splitlines():
        stripped=line.strip()
        if re.fullmatch(r'Latest Lakers News|MORE .* STORIES|Loading recommendations.*|Add us as a preferred source on Google',stripped,re.I):break
        if stripped.startswith('More news:'):continue
        lines.append(line)
    return '\n'.join(lines)
def pilot_source_tier(url):
    """Pilot-only original media qualification; production allowlist unchanged."""
    host=(urlparse(url).hostname or'').casefold()
    if host=='usatoday.com'or host.endswith('.usatoday.com'):return 'TIER_B'
    return source_tier(url)
def query_rows(sample):
    matchup=sample['away_team']+' '+sample['home_team'];day=sample['game_date']
    return [
      ('P0_lineup',f'{matchup} {day} confirmed starting lineup starters pregame official'),
      ('P0_expected',f'{matchup} {day} projected starting lineups pregame preview'),
      ('P0_available',f'{matchup} {day} injury report ruled out questionable pregame latest'),
      ('P1_rotation',f'{matchup} {day} coach minutes restriction rotation matchup preview'),
    ]
def discovery(sample,folder):
    folder.mkdir(parents=True,exist_ok=False)
    rows=[];failures=[]
    def one(pair):
        index,(priority,q)=pair
        args={'query':q,'numResults':5,'objective':f"Original pregame information for {sample['away_team']} at {sample['home_team']} {sample['game_date']}, published no later than {sample['cutoff_at']}. Prioritize explicit starting lineups, late availability, coach limits and this specific matchup. Exclude recaps, boxscores, final scores, retrospective starter listings. Need original URL and publication metadata. Data reconstruction only, no forecast."}
        response=process(CLI+['call','exa.web_search_exa','--args',json.dumps(args),'--output','json','--timeout','25000'],cwd=folder,timeout=35)
        write(folder/f'{index:02d}-request.json',args);write(folder/f'{index:02d}-response.json',response)
        found=[]
        try:
            if response['exit_code']!=0:raise ValueError('discovery_transport_failure')
            payload=json.loads(response['stdout'])
            if payload.get('isError'):raise ValueError('discovery_tool_failure')
            text='\n'.join(b.get('text','')for b in payload.get('content',[])if b.get('type')=='text')
            for block in re.split(r'\n---\n',text):
                fields={k:(m.group(1).strip()if(m:=re.search(r'^'+k+r':\s*(.*)$',block,re.M))else None)for k in('Title','URL','Published')}
                if not fields['URL']or not fields['Title']:continue
                found.append({'url':fields['URL'],'title':fields['Title'],'provider_published_at':fields['Published'],
                    'priority':priority,'channel':'exa','source_tier':pilot_source_tier(fields['URL']),'temporal_grade':'TEMPORAL_C',
                    'discovery_only':True,'request_path':str(folder/f'{index:02d}-request.json'),'response_hash':sha(response['stdout']),
                    'skip_reason':'target_result_route'if UNSAFE_ROUTE.search(fields['URL'])else None})
            return found,None
        except (ValueError,TypeError)as e:return [],{'query':q,'priority':priority,'error':str(e)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3)as pool:
        for found,error in pool.map(one,enumerate(query_rows(sample))):
            rows.extend(found)
            if error:failures.append(error)
    dedup={}
    for r in rows:
        key=canonical_url(r['url'])
        if key not in dedup:dedup[key]={**r,'priorities':[r['priority']]}
        elif r['priority']not in dedup[key]['priorities']:dedup[key]['priorities'].append(r['priority'])
    return list(dedup.values()),failures

def qualify_sources(sample,rows,folder,budget=10):
    folder.mkdir(parents=True,exist_ok=False)
    # P0 direct information, not a publisher-first season-preview ladder.
    rank={'P0_lineup':0,'P0_expected':1,'P0_available':2,'P1_rotation':3}
    selected=sorted((r for r in rows if not r['skip_reason']and r['source_tier']!='TIER_C'),
                    key=lambda r:(min(rank[p]for p in r['priorities']),r['url']))[:budget]
    evidence=[];rejected=[]
    def one(row):
        response=http(row['url'],timeout=16);ident=sha(row['url'])[:16];write(folder/(ident+'-raw.json'),response)
        if response['status']!=200:return None,{**row,'rejection_reason':'original_fetch_failed','http_status':response['status']}
        parsed=extract_article(response['body'],response.get('final_url')or row['url'])
        # Parsed article body only; scripts/live widgets are not article evidence.
        combined=row['title']+'\n'+parsed['title']+'\n'+parsed['body'][:10000]
        matching=all(mentions(combined,aliases(sample,s))for s in('home','away'))and len(parsed['body'])>=150
        pregame=matching and bool(re.search(r'preview|pregame|pre-game|projected|starting lineups|injury report|how to watch|stream|赛前|预计首发',combined,re.I))
        if re.search(r'\brecap\b|takeaways from|\b(?:win|loss) over\b|赛后',row['title']+' '+parsed['title'],re.I):pregame=False
        meta={**row,**{k:parsed.get(k)for k in('published_at','updated_at','timestamp_source','timestamp_confidence','source_timestamp_verified')},'is_pregame':pregame}
        qualified=qualify_article(meta,sample['cutoff_at'])
        record={**meta,**qualified,'source_url':row['url'],'captured_at':response['fetched_at'],'document_hash':response['raw_sha256'],
                'body_hash':sha(parsed['body']),'raw_path':str(folder/(ident+'-raw.json')),'parsed_path':str(folder/(ident+'-parsed.json')),
                'cutoff':sample['cutoff_at'],'fact_extraction_method':'exact source excerpt only; no invented claim or LLM certification'}
        write(folder/(ident+'-parsed.json'),parsed)
        if qualified['research_fact_eligible']:
            # Short original excerpt, not a full article redistribution.
            parts=[p.strip()for p in parsed['body'].splitlines()if len(p.strip())>45 and any(mentions(p,aliases(sample,s))for s in('home','away'))]
            snippet=parts[0]if parts else parsed['body'][:200]
            record['source_excerpt']=' '.join(snippet.split()[:22]);record['source_excerpt_full_hash']=sha(snippet)
            return record,None
        return None,{**record,'rejection_reason':';'.join(qualified['reasons'])}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4)as pool:
        for accepted,denied in pool.map(one,selected):
            if accepted:evidence.append(accepted)
            if denied:rejected.append(denied)
    unseen=[{**r,'rejection_reason':r.get('skip_reason')or'low_source_tier_or_bounded_fetch_budget'}for r in rows if r not in selected]
    return evidence,rejected+unseen,{'original_fetch_budget':budget,'attempted':len(selected),'accepted_temporal_b':len(evidence),
                                  'priority':'P0 injury separately official PDFs; lineup -> availability -> rotation -> background'}

def roster_from_history(sample,ledger,injury):
    sides={}
    for side in('home','away'):
        team=int(sample[side+'_team_id']);members={}
        for p in ledger[side]['player_history']['players']:
            members[p['player_name']]={'player_id':p['player_id'],'player_name':p['player_name'],'team_id':team,
                'identity_confidence':'historical_candidate_not_current_roster_confirmation','identity_basis':'pre-target recorded appearance',
                'last_observed_before_target':p['last_observed_before_target'],'source':p['source'],'availability':'UNKNOWN'}
        for r in injury.get('records',[]):
            if r['team']!=sample[side+'_team']:continue
            item=members.setdefault(r['player_name'],{'player_id':None,'player_name':r['player_name'],'team_id':team,'last_observed_before_target':None})
            item.update(identity_confidence='official_report_current_team_identity',identity_basis='matched official pre-cutoff injury report team column',
                        source=injury['source_url'],availability=r['availability_status'],availability_basis='official_report')
        sides[side]={'status':'PARTIAL'if members else'UNAVAILABLE','team_id':team,'players':list(members.values()),
                     'complete_roster_archive_found':False,'historical_observation_is_not_availability':True}
    return sides

def normalized_name(value):
    import unicodedata
    s=''.join(c for c in unicodedata.normalize('NFKD',value)if not unicodedata.combining(c))
    return re.sub(r'[^a-z]','',s.casefold())

def extract_lineups(sample,evidence,roster,injury):
    teams=[];rejections=[]
    for side in('home','away'):
        name=sample[side+'_team'];catalog=roster[side]['players'];team_id=int(sample[side+'_team_id'])
        found=None;proposals=[]
        for source in evidence:
            parsed=json.loads(Path(source['parsed_path']).read_text(encoding='utf-8'));body=primary_body(parsed['body'])
            # Require an explicit labeled source list. Never infer from five top minutes.
            lines=[line.strip()for line in body.splitlines()if line.strip()];patterns=[name,name.split()[-1]]
            if name=='Minnesota Timberwolves':patterns.append('Wolves')
            for i,line in enumerate(lines):
                context='\n'.join(lines[max(0,i-4):i+8])
                if not any(p.casefold()in line.casefold()for p in patterns):continue
                section=None
                for earlier in lines[:i+1]:
                    if re.fullmatch(r'(PROJECTED|EXPECTED|PROBABLE|POSSIBLE|CONFIRMED|OFFICIAL) (STARTERS|STARTING LINEUPS?|STARTING FIVE)',earlier,re.I):section=earlier
                    elif re.fullmatch(r'INJURY REPORT|BETTING LINES.*|QUOTABLE',earlier,re.I):section=None
                if not re.search(r'(?:projected|expected|possible|probable|confirmed|starting)\s+(?:starting\s+)?lineups?|starters|starting five|lineup alert|will start',(section or'')+'\n'+context,re.I):continue
                direct_list=bool(re.search(r"starters.{0,8}at |lineup alert:.{0,35}will start",line,re.I))
                named_inline=sum(normalized_name(p['player_name'])in normalized_name(line)for p in catalog)>=5
                text=line if direct_list or named_inline else line+'\n'+'\n'.join(lines[i+1:i+7]);players=[]
                surnames={}
                for p in catalog:surnames.setdefault(normalized_name(p['player_name'].split()[-1]),[]).append(p['player_name'])
                for p in catalog:
                    fullname=normalized_name(p['player_name']);surname=normalized_name(p['player_name'].split()[-1])
                    if fullname in normalized_name(text)or(len(surnames[surname])==1 and re.search(r'(?<!\w)'+re.escape(p['player_name'].split()[-1])+r'(?!\w)',text,re.I)):
                        players.append(p['player_name'])
                if len(set(players))!=5:continue
                expected=bool(re.search(r'projected|expected|possible|probable',section or context,re.I))
                # A direct pregame reporter list is distinct from an expected paragraph.
                definitive_list=direct_list
                confirmed=definitive_list or bool(re.search(r'confirmed starting|announced.{0,25}starting|official starting',context,re.I))
                if definitive_list:expected=False
                if not(expected or confirmed):continue
                row={'team_id':team_id,'team':name,'players':players,'basis':'source_expected'if expected else'confirmed_before_cutoff',
                    'source':urlparse(source['source_url']).hostname,'source_url':source['source_url'],'published_at':source['published_at'],
                    'document_hash':source['document_hash'],'captured_at':source['captured_at'],'temporal_grade':source['temporal_grade'],
                    'identity_verified':True,'source_list_excerpt_hash':sha(text),'identity_basis':'source names matched this team candidate catalog'}
                outnames={normalized_name(r['player_name'])for r in injury.get('records',[])if r['team']==name and r['availability_status']=='Out'}
                q=qualify_lineup(row,sample['cutoff_at'])
                if any(normalized_name(p)in outnames for p in players):q={'eligible':False,'reasons':['source_expected_conflicts_with_latest_official_out']}
                if q['eligible']:proposals.append({**row,**q})
                else:rejections.append({**row,**q})
        if proposals:found=min(proposals,key=lambda r:0 if r['basis']=='confirmed_before_cutoff'else 1)
        teams.append(found or{'team_id':team_id,'team':name,'basis':'unknown','players':[],'cutoff_pass':False,
                'reason':'no_qualified_explicit_five_player_pregame_source','agent_expected_not_generated':True})
    return {'teams':teams,'rejected_lists':rejections,'target_boxscore_read':False,'historical_starts_not_used_to_fill':True}
