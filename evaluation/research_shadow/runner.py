"""One isolated, outcome-blind, three-game retrieval experiment.

Reuses recorded production planner subqueries; no LLM invocation, no production
callback, no database writes. Runtime receipts live outside the repository.
"""
from __future__ import annotations
from pathlib import Path
import argparse,concurrent.futures,copy,hashlib,importlib.util,json,os,re,sqlite3,subprocess,sys,time
import urllib.error,urllib.request,xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from .core import (sha,stamp,normalized_time,canonical_url,candidate,deduplicate,source_tier,
                   qualify,extract_article,extract_leads,aliases,mentions,metrics,aggregate,compare_excerpts)

PROJECT=Path(__file__).resolve().parents[2]
RSS_FEEDS=(('ESPN NBA','https://www.espn.com/espn/rss/nba/news'),
           ('CBS NBA','https://www.cbssports.com/rss/headlines/nba/'))
ARTIFACT_DEFAULT=Path(str(Path.home() / '.agent-reach/audits/courtvision-shadow-20261012/run-v1'))
CLI=['<local-configured-path> Files/nodejs/node.exe',str(Path.home() / '.agent-reach/tools/mcporter/node_modules/mcporter/dist/cli.js'),
     '--config',str(Path.home() / '.mcporter/mcporter.json')]

def now():return datetime.now(timezone.utc).isoformat()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8')as f:json.dump(value,f,ensure_ascii=False,indent=2)
def process(args,*,cwd,timeout=40,stdin=None,env=None):
    start=time.perf_counter();request=now()
    p=None
    try:
        p=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        out,err=p.communicate(input=stdin,timeout=timeout)
        r={'exit_code':p.returncode,'stdout':out,'stderr':err}
    except subprocess.TimeoutExpired as e:
        if p is not None:
            if os.name=='nt':
                subprocess.run(['taskkill.exe','/PID',str(p.pid),'/T','/F'],capture_output=True,timeout=8,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            else:p.kill()
            try:p.communicate(timeout=3)
            except subprocess.TimeoutExpired:pass
        r={'exit_code':'TIMEOUT','stdout':(e.stdout or b'').decode('utf-8',errors='replace')if isinstance(e.stdout,bytes)else e.stdout or'',
           'stderr':'bounded timeout','failure_type':'transport_timeout'}
    r.update(command=args,requested_at=request,completed_at=now(),seconds=round(time.perf_counter()-start,3));return r

def public_url(url):
    from v2.pregame_system.web_search import _public_discovery_url
    return _public_discovery_url(url)
class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not public_url(newurl):raise ValueError('unsafe_redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)
def http(url,timeout=16):
    r={'url':url,'requested_at':now()};start=time.perf_counter()
    try:
        if not public_url(url):raise ValueError('unsafe_url')
        req=urllib.request.Request(url,headers={'User-Agent':'CourtVision-Shadow-Evaluation/1.0','Accept':'text/html,application/rss+xml,application/xml'})
        with urllib.request.build_opener(PublicRedirect()).open(req,timeout=timeout)as response:
            raw=response.read(3*1024*1024+1)
            if len(raw)>3*1024*1024:raise ValueError('body_size_exceeded')
            encoding=response.headers.get_content_charset()or'utf-8'
            r.update(status=response.status,final_url=response.url,content_type=response.headers.get('Content-Type'),
                  body=raw.decode(encoding,errors='replace'),raw_sha256=sha(raw),http_date=response.headers.get('Date'))
    except Exception as e:r.update(status='FAILED',error={'type':type(e).__name__,'message':str(e)[:500]})
    r.update(fetched_at=now(),seconds=round(time.perf_counter()-start,3));return r

def select_frozen_plans():
    # Only registered 2026 production-input roots. No Dataset/Golden/output quality access.
    rows=[]
    roots=[PROJECT/'v2/pregame_system/outputs/prediction_version_runs',PROJECT/'v2/pregame_system/outputs/heat_raptors_operator_acceptance']
    for root in roots:
        for p in sorted(root.rglob('search_model_request.json')):
            trace=p.with_name('trace.json')
            if not trace.exists():continue
            request=read(p);visible=json.loads(request['messages'][-1]['content']);game=visible.get('game')or{}
            cut=visible.get('cutoff');tip=game.get('scheduled_tipoff')
            if not(game.get('registered_game')and str(game.get('game_id','')).startswith('espn:')and game.get('season_start')==2026):continue
            if not(stamp(cut)and stamp(tip)and stamp(cut)<stamp(tip)):continue
            if game.get('away_team_id')==game.get('home_team_id'):continue
            data=read(trace);call=next((c for c in data.get('calls',[])if c.get('tool')=='search_pregame_web'),None)
            if not call or not call.get('source_trace'):continue
            queries=[{'category':q['category'],'query':q['query']}for q in call['source_trace']if q['category']!='market']
            if not queries:continue
            rows.append({'game':game,'cutoff':cut,'query_rows':queries,'planner_args':call['args'],
                'planner_request_path':str(p),'planner_request_hash':sha(p.read_bytes()),'planner_trace_hash':sha(trace.read_bytes()),
                'plan_hash':sha(json.dumps({'game':game,'cutoff':cut,'queries':queries,'args':call['args']},sort_keys=True,ensure_ascii=False)),
                'planner_mode':'frozen_production_plan_replay_no_new_llm'})
    rows.sort(key=lambda r:(stamp(r['game']['scheduled_tipoff']),stamp(r['cutoff']),r['planner_request_path']))
    selected=[];ids=set();teams=set()
    for row in rows:
        gid=row['game']['game_id'];pair={row['game']['away_team_id'],row['game']['home_team_id']}
        if gid in ids or pair&teams:continue
        selected.append(row);ids.add(gid);teams|=pair
        if len(selected)==3:break
    if len(selected)!=3:raise ValueError('three_valid_distinct_team_frozen_plans_unavailable')
    return selected

def existing_search(plan,folder):
    """Real PregameWebSearch discovery/filtering, exact frozen subqueries.

    Existing primary free-search provider only; no keyed fallback is configured
    in this isolated process. This limitation is explicit in every receipt.
    """
    from v2.pregame_system.web_search import PregameWebSearch
    from v2.pregame_system.free_search_mcp_client import FreeSearchMCPTransport,FreeSearchMCPError
    folder.mkdir(parents=True,exist_ok=False)
    env={k:v for k,v in os.environ.items()if not(k.startswith('SEARCH_MCP_')or 'API_KEY'in k or k.endswith('_TOKEN'))}
    env.update(SEARCH_MCP_CONFIG_DIR=str(folder/'config'),SEARCH_MCP_CACHE_DIR=str(folder/'cache'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    (folder/'config').mkdir();(folder/'cache').mkdir()
    payload=[{'query':q['query'],'engine':'anysearch','max_results':6,'max_age_hours':0}for q in plan['query_rows']]
    args=['uv','run','--offline','--no-project','--with','free-search-mcp==0.11.0','--with','selectolax==0.4.13','python',str(PROJECT/'v2/pregame_system/free_search_mcp_client.py'),'--bridge']
    result=process(args,cwd=folder,timeout=100,stdin=json.dumps(payload,ensure_ascii=False),env=env)
    write(folder/'request.json',payload);write(folder/'response.json',result)
    records=[];errors=[];production_result=None
    try:
        if result['exit_code']!=0:raise ValueError('bridge_transport_failure')
        responses=json.loads(result['stdout'])
        if len(responses)!=len(payload):raise ValueError('bridge_response_count_mismatch')
        class CapturedTransport(FreeSearchMCPTransport):
            def search_many(self,payloads):
                if [p['query']for p in payloads]!=[q['query']for q in plan['query_rows']]:raise ValueError('frozen_queries_changed')
                return responses
        class FrozenQueries(PregameWebSearch):
            def queries(self,*args,**kwargs):return [(q['category'],q['query'])for q in plan['query_rows']]
        production_result=FrozenQueries(root=folder/'search-artifacts',local_transport=CapturedTransport()).search(
            plan['game'],plan['cutoff'],replay=False,categories=set(q['category']for q in plan['query_rows']))
        for q,response in zip(plan['query_rows'],responses):
            if response.get('errors'):errors.append({'query':q['query'],'errors':response['errors']})
        for r in production_result['records']:
            records.append(candidate(source='free-search-mcp:anysearch',source_type='search',channel='existing',title=r['title'],url=r['url'],cutoff=plan['cutoff'],
                 published_at=r.get('published_at'),timestamp_source='existing.provider.published_date'if r.get('published_at')else'unavailable',
                 snippet=r.get('snippet')or'',query=r.get('discovered_via'),category=r['category'],
                 provider_temporal_metadata=r.get('provider_temporal_metadata')or{},discovered_at=result['completed_at']))
    except (ValueError,KeyError,TypeError)as e:errors.append({'error':str(e)})
    return records,{'seconds':result['seconds'],'query_count':len(payload),'errors':errors,'status':'success'if records else'provider_failure'if errors else'empty','keyed_fallback':False,
                    'production_discovery_code_used':production_result is not None,'formal_claim_extractor_run':False,
                    'baseline_scope':'PregameWebSearch current discovery with free-search primary; Tavily fallback not configured in audit process'}

def mcp_content(result):
    if result['exit_code']!=0:raise ValueError('mcp_process_failure')
    data=json.loads(result['stdout'])
    if data.get('isError'):raise ValueError('mcp_tool_error')
    return '\n'.join(b.get('text','')for b in data.get('content',[])if b.get('type')=='text')
def parse_exa(text,plan,q,completed):
    rows=[]
    for block in re.split(r'\n---\n',text):
        fields={k:re.search(r'^'+k+r':\s*(.*)$',block,re.M)for k in ('Title','URL','Published','Author')}
        if not fields['URL']or not fields['Title']:continue
        url=fields['URL'].group(1).strip();title=fields['Title'].group(1).strip()
        if not public_url(url):continue
        if not any(mentions(title+'\n'+block,aliases(plan['game'],side))for side in ('home','away')):continue
        pub=fields['Published'].group(1).strip()if fields['Published']else None
        pub=pub if pub not in ('N/A','None','')else None
        rows.append(candidate(source='Exa anonymous MCP',source_type='search',channel='exa',title=title,url=url,cutoff=plan['cutoff'],published_at=pub,
              timestamp_source='exa.Published'if pub else'unavailable',provider_author=fields['Author'].group(1)if fields['Author']else None,
              snippet=block.split('Highlights:',1)[-1][:2000],query=q['query'],category=q['category'],discovered_at=completed))
    return rows
def exa_search(plan,folder):
    start=time.perf_counter();folder.mkdir(parents=True,exist_ok=False)
    def one(index,q):
        args={'query':q['query'],'numResults':6,'objective':f"Original pregame reporting for {plan['game']['away_team']} at {plan['game']['home_team']} scheduled {plan['game']['scheduled_tipoff']}. Focus on roster, availability, coach usage and matchup. Exclude target-game recaps and preserve original URLs/publication metadata. Shadow-only evaluation; no outcome input."}
        r=process(CLI+['call','exa.web_search_exa','--args',json.dumps(args,ensure_ascii=False),'--output','json','--timeout','30000'],cwd=folder,timeout=38)
        write(folder/f'{index:02d}-request.json',args);write(folder/f'{index:02d}-response.json',r)
        try:return parse_exa(mcp_content(r),plan,q,r['completed_at']),None
        except (ValueError,TypeError,KeyError)as e:return [],{'query':q['query'],'error':str(e),'exit_code':r['exit_code']}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3)as pool:
        outputs=list(pool.map(lambda pair:one(*pair),enumerate(plan['query_rows'])))
    return [r for rows,error in outputs for r in rows],{'seconds':round(time.perf_counter()-start,3),'query_count':len(outputs),'errors':[e for rows,e in outputs if e]}

def parse_feed(raw):
    tree=ET.fromstring(raw);rows=[]
    for item in [*tree.findall('.//item'),*tree.findall('{http://www.w3.org/2005/Atom}entry')]:
        fields={}
        for child in item:
            key=child.tag.rsplit('}',1)[-1]
            value=''.join(child.itertext()).strip()
            if key=='link'and child.attrib.get('href'):value=child.attrib['href']
            fields.setdefault(key,value)
        link=fields.get('link');title=fields.get('title');pub=fields.get('pubDate')or fields.get('published')or fields.get('date')
        if link and title:rows.append({'url':link,'title':title,'published_at':pub,'description':fields.get('description')or fields.get('summary')or'',
                 'timestamp_source':'rss.pubDate'if fields.get('pubDate')else'atom.published'if fields.get('published')else'rss.dc.date'if fields.get('date')else'unavailable'})
    return rows
def rss_search(plans,folder):
    start=time.perf_counter();outputs=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2)as pool:
        futures={pool.submit(http,url):name for name,url in RSS_FEEDS}
        for future in concurrent.futures.as_completed(futures):
            name=futures[future];response=future.result();write(folder/(re.sub(r'\W+','_',name)+'.json'),response)
            try:
                if response['status']!=200:raise ValueError('rss_http_failure')
                entries=parse_feed(response['body'])
                if not entries:raise ValueError('rss_no_entries')
                outputs.append({'name':name,'response':response,'entries':entries,'error':None})
            except (ValueError,ET.ParseError)as e:outputs.append({'name':name,'response':response,'entries':[],'error':str(e)})
    by_game={}
    for plan in plans:
        rows=[]
        for o in outputs:
            for r in o['entries']:
                if not public_url(r['url'])or source_tier(r['url'])=='TIER_C':continue
                if not any(mentions(r['title']+'\n'+r['description'],aliases(plan['game'],s))for s in ('home','away')):continue
                rows.append(candidate(source=o['name'],source_type='trusted_rss',channel='rss',title=r['title'],url=r['url'],cutoff=plan['cutoff'],
                    published_at=r['published_at'],timestamp_source=r['timestamp_source'],snippet=r['description'],feed_url=o['response']['url'],
                    discovered_at=o['response']['fetched_at'],rss_raw_sha256=o['response'].get('raw_sha256')))
        by_game[plan['game']['game_id']]=rows
    return by_game,{'seconds':round(time.perf_counter()-start,3),'feeds':[{'source':o['name'],'url':o['response']['url'],'status':o['response']['status'],'entry_count':len(o['entries']),'error':o['error']}for o in outputs]}

def fetch_rows(rows,game,folder,limit=4,existing=()):
    old={canonical_url(r['url'])for r in existing}
    ranked=sorted((r for r in rows if r['source_tier']!='TIER_C'and canonical_url(r['url'])not in old),
       key=lambda r:(r['source_tier'],not(mentions(r['title'],aliases(game,'home'))and mentions(r['title'],aliases(game,'away'))),
                     bool(re.search(r'recap|takeaways|\b(?:win|loss|beat|final)\b|赛后|大胜|不敌',r['title'],re.I)),r['url']))
    selected=ranked[:limit];start=time.perf_counter()
    def one(row):
        row['fetch_attempted']=True;r=http(row['url']);name=sha(row['canonical_url'])[:16]
        write(folder/(name+'-http.json'),r);row['fetched_at']=r['fetched_at'];row['fetch_latency_seconds']=r['seconds']
        if r['status']!=200:row['fetch_status']='failed';row['fetch_error']=r.get('error');return
        try:info=extract_article(r['body'],r.get('final_url')or row['url'])
        except (ValueError,TypeError)as e:row['fetch_status']='failed';row['fetch_error']=str(e);return
        if len(info['body'])<100:row['fetch_status']='empty_body';return
        row['fetch_status']='success';row['raw_response_sha256']=r['raw_sha256'];row['content_sha256']=sha(info['body'])
        row['fetch_receipt_path']=str(folder/(name+'-http.json'))
        row['identity_match']=any(mentions(info['body'],aliases(game,s))for s in ('home','away'))
        row['is_recap']=bool(re.search(r'takeaways|\brecap\b|\b(?:win|loss|beat)\b|赛后|大胜|不敌',info['title'],re.I))
        if info['published_at']:
            for f in ('published_at','updated_at','timestamp_source','timestamp_confidence','source_timestamp_verified'):row[f]=info[f]
        elif info['updated_at']:row['updated_at']=info['updated_at']
        row['html_temporal_candidates']=info['temporal_candidates'];qualify(row,game=game)
        row['shadow_claims']=extract_leads(row,info['body'],game);row['claim_extract_status']='deterministic_exact_excerpts_only'
        row['potentially_useful']=bool(row['shadow_claims'])and not row['is_recap']
        write(folder/(name+'-parsed.json'),info)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4)as pool:list(pool.map(one,selected))
    for row in rows:qualify(row,game=game)
    return {'seconds':round(time.perf_counter()-start,3),'attempted_count':len(selected),'url_limit':limit,
             'tier_ab_unfetched_count':max(0,len(ranked)-len(selected)),'tier_c_excluded_count':sum(r['source_tier']=='TIER_C'for r in rows),
             'policy':'shadow_bounded_original_http_read_not_full_production_fetch_architecture'}

def protection():
    # Hash-only: no interpretation of Dataset labels or ground-truth values.
    roots=('v2/pregame_system','v2/postgame_system','v2/agent','v2/frontend','agent/skills','evaluation/datasets','evaluation/golden_set')
    files={}
    for name in roots:
        for p in(PROJECT/name).rglob('*'):
            if p.is_file()and'__pycache__'not in p.parts and p.suffix not in {'.sqlite','.sqlite-wal','.sqlite-shm','.log','.pyc'}:
                files[str(p.relative_to(PROJECT))]=sha(p.read_bytes())
    dbpath=PROJECT/'v2/frontend/output/prediction_versions.sqlite';versions={}
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)as db:
        for table in ('prediction_versions','postgame_versions'):
            versions[table]=[(i,sha(raw),stored)for i,raw,stored in db.execute('SELECT version_id,payload_json,payload_sha256 FROM '+table+' ORDER BY version_id')]
            if any(actual!=stored for i,actual,stored in versions[table]):raise ValueError('existing_published_hash_mismatch')
    return {'files':files,'versions':versions}

def run(root):
    run_start=time.perf_counter()
    root=Path(root).resolve()
    if root.exists():raise ValueError('immutable_output_destination_exists')
    if root.is_relative_to(PROJECT):raise ValueError('runtime_artifacts_must_be_outside_project')
    if os.environ.get('TAVILY_API_KEY'):raise ValueError('keyed_search_requires_separate_authorized_comparison')
    config=read(str(Path.home() / '.mcporter/mcporter.json'))
    if config.get('imports')!=[]or config.get('mcpServers',{}).get('exa',{}).get('baseUrl')!='https://mcp.exa.ai/mcp':raise ValueError('anonymous_exa_config_boundary')
    exa=config['mcpServers']['exa']
    if any(k in exa for k in ('headers','env','auth','token')):raise ValueError('unexpected_exa_credentials')
    plans=select_frozen_plans();root.mkdir(parents=True,exist_ok=False)
    before=protection();write(root/'protection-before.json',before)
    write(root/'selection.json',{'selected_at':now(),'selection_rule':'chronological_valid_registered_frozen_plan; distinct games and nonoverlapping team pairs; no outcome/performance selection',
                                'plans':plans,'no_ground_truth_input':True,'cutoff_policy':'original_frozen_research_cutoff; strict publish_update_and_pre_cutoff_capture_proof'})
    schema=process(CLI+['list','exa','--schema','--json'],cwd=root,timeout=35);write(root/'exa-schema.json',schema)
    text=json.loads(schema.get('stdout')or'{}')
    if schema['exit_code']!=0 or not any(t['name']=='web_search_exa'for t in text.get('tools',[])):raise ValueError('exa_schema_preflight_failure')
    rss,rss_receipt=rss_search(plans,root/'rss');write(root/'rss-summary.json',rss_receipt)
    games=[]
    for index,plan in enumerate(plans):
        print(json.dumps({'stage':'search_started','game_id':plan['game']['game_id'],'query_count':len(plan['query_rows'])}),flush=True)
        folder=root/f'{index+1:02d}-{plan["game"]["game_id"].replace(":","_")}'
        folder.mkdir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2)as pool:
            af=pool.submit(existing_search,plan,folder/'existing');sf=pool.submit(exa_search,plan,folder/'exa')
            araw,a_receipt=af.result();sraw,s_receipt=sf.result()
        sraw+=rss[plan['game']['game_id']]
        existing=deduplicate(araw);shadow=deduplicate(sraw,existing=existing)
        afetch=fetch_rows(existing,plan['game'],folder/'fetch-existing')
        sfetch=fetch_rows(shadow,plan['game'],folder/'fetch-shadow',existing=existing)
        novelty=compare_excerpts(existing,shadow)
        a=metrics(existing,raw_count=len(araw),existing=[],latency_seconds=a_receipt['seconds']+afetch['seconds'])
        s=metrics(shadow,raw_count=len(sraw),existing=existing,latency_seconds=s_receipt['seconds']+sfetch['seconds'])
        combined=deduplicate(existing+shadow,existing=existing)
        b=metrics(combined,raw_count=len(araw)+len(sraw),existing=existing,
                  latency_seconds=max(a_receipt['seconds'],s_receipt['seconds'])+afetch['seconds']+sfetch['seconds'])
        result={'game':plan['game'],'cutoff':plan['cutoff'],'plan_hash':plan['plan_hash'],'planner_mode':plan['planner_mode'],
            'existing_candidates':existing,'shadow_candidates':shadow,'existing_metrics':a,'shadow_metrics':s,'combined_metrics':b,
            'existing_search_receipt':a_receipt,'shadow_search_receipt':s_receipt,'existing_fetch_selection':afetch,'shadow_fetch_selection':sfetch,'novelty':novelty,
            'comparison_baseline_reused_not_regenerated':True,'existing_candidate_hash':sha(json.dumps(existing,sort_keys=True,ensure_ascii=False)),
            'production_accepted_evidence_count':0,'production_forecast_calls':0}
        write(folder/'result.json',result);games.append(result)
        print(json.dumps({'stage':'game_complete','game_id':plan['game']['game_id'],'existing':a['candidate_count'],'shadow':s['candidate_count'],
          'new_urls':s['unique_new_urls'],'hard_fact_eligible':s['hard_fact_eligible_count'],'fetch_success':s['fetch_success_count']}),flush=True)
    after=protection();write(root/'protection-after.json',after)
    same=before==after
    output={'schema_version':'agent-reach-shadow-research-v1','created_at':now(),'artifact_root':str(root),'shadow_only':True,
       'sample_count':len(games),'selection':read(root/'selection.json'),'games':games,
       'overall':{'existing':aggregate([g['existing_metrics']for g in games]),'shadow':aggregate([g['shadow_metrics']for g in games]),
                  'combined':aggregate([g['combined_metrics']for g in games])},'rss':rss_receipt,
       'policy':{'same_frozen_planner':True,'historical_cutoff_proof_required':True,'new_model_calls':0,'full_production_claim_extractor_run':False,
                 'claim_method':'deterministic_exact_body_topic_excerpts_not_semantic_fact_certification','new_forecast_calls':0,'new_prediction_versions':0,
                 'keyed_search_fallback':False,'historical_search_is_new_discovery_not_point_in_time_replay':True},
       'run_wall_seconds':round(time.perf_counter()-run_start,3),'shared_rss_latency_seconds':rss_receipt['seconds'],
       'protection':{'unchanged':same,'protected_file_count':len(before['files']),'prediction_count':len(before['versions']['prediction_versions']),
                     'postgame_count':len(before['versions']['postgame_versions']),'before_hash':sha(json.dumps(before,sort_keys=True)),
                     'after_hash':sha(json.dumps(after,sort_keys=True))}}
    write(root/'summary.json',output)
    if not same:raise ValueError('protected_production_or_dataset_drift')
    print(json.dumps({'stage':'complete','summary':str(root/'summary.json'),'protected_files':len(before['files']),'protection_unchanged':same}),flush=True)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--artifacts',type=Path,default=ARTIFACT_DEFAULT);args=parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8');run(args.artifacts)
