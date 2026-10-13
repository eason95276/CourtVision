import concurrent.futures,json,re,urllib.request,sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .metadata import AUDIT,ROOT,corroborate_pdf_clock
from evaluation.research_shadow.core import sha,stamp
from evaluation.research_shadow.runner import write

ALLOWED={'Out','Doubtful','Questionable','Probable','Available'}
def archive_name_matches(url,report_time):
    m=re.fullmatch(r'https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(?:_(\d{2}))?(AM|PM)\.pdf',url)
    if not m:return False
    d=datetime.fromisoformat(report_time).astimezone(ZoneInfo('America/New_York'))
    if d.strftime('%Y-%m-%d')!=m[1]or d.strftime('%I')!=m[2]or d.strftime('%p')!=m[4]:return False
    return (d.minute==int(m[3]))if m[3]is not None else d.minute==30

def validated_table_rows(tables,team_names):
    """Footer/header cannot poison inherited team across PDF page breaks."""
    compact=lambda s:re.sub(r'[^a-z0-9@]','',str(s or'').casefold())
    lookup={compact(name):name for name in team_names};lookup.update({'laclippers':'LA Clippers'})
    context={'game_date':None,'game_time':None,'matchup':None,'team':None};rows=[]
    for page,table in enumerate(tables,1):
        for raw in table or[]:
            cells=list(raw or[])+[None]*7;date,clock,matchup,team,player,status,reason=cells[:7]
            clean=lambda x:re.sub(r'\s+',' ',str(x or'')).strip()
            if re.fullmatch(r'\d{2}/\d{2}/\d{4}',clean(date)):context['game_date']=clean(date)
            if re.fullmatch(r'\d{2}:\d{2}\(ET\)',clean(clock).replace(' ','')):context['game_time']=clean(clock).replace(' ','')
            if re.fullmatch(r'[A-Z]{3}@[A-Z]{3}',clean(matchup)):context['matchup']=clean(matchup)
            if compact(team)in lookup:context['team']=lookup[compact(team)]
            elif clean(team):
                # Unknown explicit team or the "Injury Report" heading is not a data row.
                continue
            if clean(status)not in ALLOWED or','not in str(player or''):continue
            family,given=(clean(p)for p in str(player).split(',',1))
            if not given or not family or not all(context.values()):continue
            rows.append({**context,'player_name':given+' '+family,'player_name_official_raw':clean(player),
                'availability_status':clean(status),'reason':clean(reason),'page':page})
    return rows

def parse(path,url):
    import pdfplumber
    from v2.scripts.import_official_injury_report import _report_timestamp
    dbpath=ROOT/'analysis_v1/data/courtvision_analysis.sqlite'
    if not dbpath.exists():dbpath=ROOT.parent/'courtvision/analysis_v1/data/courtvision_analysis.sqlite'
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)as db:
        teams=[r[0]for r in db.execute('SELECT team_name FROM dim_team')]
    tables=[];t=None
    with pdfplumber.open(path)as document:
        for page in document.pages:
            heading=_report_timestamp(page)
            if t and heading!=t:raise ValueError('inconsistent_pdf_heading_times')
            t=heading;width=float(page.width)
            tables.append(page.extract_table({'vertical_strategy':'explicit','explicit_vertical_lines':[width*r for r in(0.024,0.137,0.232,0.307,0.500,0.690,0.785,0.980)],
                'horizontal_strategy':'lines','snap_tolerance':5,'join_tolerance':5}))
    rows=validated_table_rows(tables,teams)
    if not t or not rows:raise ValueError('official_pdf_has_no_valid_rows')
    if not archive_name_matches(url,t.isoformat()):raise ValueError('official_archive_name_and_header_mismatch')
    return t,rows

def obtain(url,folder):
    name=url.rsplit('/',1)[-1];cached=AUDIT/name;start=datetime.now(timezone.utc).isoformat()
    if cached.exists()and(cached.with_name(name+'.receipt.json')).exists():
        receipt=json.loads(cached.with_name(name+'.receipt.json').read_text(encoding='utf-8'))
        if receipt.get('sha256')==sha(cached.read_bytes()):return cached,receipt
    receipt={'source_url':url,'requested_at':start};target=folder/name
    try:
        request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://official.nba.com/'})
        with urllib.request.urlopen(request,timeout=12)as r:
            raw=r.read(5*1024*1024+1)
            if len(raw)>5*1024*1024 or not raw.startswith(b'%PDF'):raise ValueError('invalid_official_pdf')
        with target.open('xb')as f:f.write(raw)
        t,rows=parse(target,url);receipt.update(status='success',report_time=t.isoformat(),sha256=sha(raw),bytes=len(raw))
    except Exception as e:receipt.update(status='unavailable',error={'type':type(e).__name__,'message':str(e)[:250]})
    receipt['captured_at']=datetime.now(timezone.utc).isoformat();write(folder/(name+'.receipt.json'),receipt)
    return target if receipt['status']=='success'else None,receipt

def resolve(sample,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    cut=stamp(sample['cutoff_at']);local=cut.astimezone(ZoneInfo('America/New_York'));day=local.strftime('%Y-%m-%d')
    # Bound the latest-known scan; never infer publication from requested URL.
    candidates=set()
    for hours_back in(0,1,2):
        d=local-timedelta(hours=hours_back)
        candidates.add('https://ak-static.cms.nba.com/referee/injury/'+d.strftime('Injury-Report_%Y-%m-%d_%I%p.pdf'))
    floor=local.replace(minute=local.minute-local.minute%5,second=0,microsecond=0)
    for minutes_back in range(0,65,5):
        d=floor-timedelta(minutes=minutes_back)
        candidates.add('https://ak-static.cms.nba.com/referee/injury/'+d.strftime('Injury-Report_%Y-%m-%d_%I_%M%p.pdf'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4)as pool:
        results=list(pool.map(lambda u:obtain(u,folder),sorted(candidates)))
    known=[];rejected=[]
    for path,receipt in results:
        url=receipt.get('url')or receipt.get('source_url')
        if not path:rejected.append(receipt);continue
        try:
            t,rows=parse(path,url)
            if t.astimezone(timezone.utc)>cut:
                rejected.append({'source_url':url,'report_time':t.isoformat(),'reason':'after_cutoff'});continue
            matching=corroborate_pdf_clock(sample,rows)
            valid=[r for r in matching if r['team']in {sample['home_team'],sample['away_team']}and r['availability_status']in ALLOWED]
            teams={r['team']for r in valid}
            if not valid:raise ValueError('target_report_no_valid_player_status')
            known.append((t,path,receipt,valid,len(matching)-len(valid),teams,url))
        except Exception as e:rejected.append({'source_url':url,'reason':str(e)})
    write(folder/'discovery_receipt.json',{'cutoff':sample['cutoff_at'],'attempted_urls':sorted(candidates),'candidate_count':len(candidates),'rejected':rejected})
    if not known:return {'status':'UNAVAILABLE','records':[],'reason':'official_report_not_found_or_not_qualified','discovery_path':str(folder/'discovery_receipt.json')}
    t,path,receipt,records,dropped,teams,url=max(known,key=lambda x:x[0])
    copied=folder/'selected.pdf'
    with copied.open('xb')as f:f.write(path.read_bytes())
    data={'status':'COMPLETE'if teams=={sample['home_team'],sample['away_team']}else'PARTIAL',
          'report_time':t.astimezone(timezone.utc).isoformat(),'source_url':url,'source_path':str(copied),'document_hash':sha(copied.read_bytes()),
          'captured_at':receipt.get('captured_at'),'teams_covered':sorted(teams),'matched_game':sample['target_game_id'],'records':records,
          'temporal_grade':'TEMPORAL_A','confidence':'official_versioned_document_header_and_game_identity',
          'publication_basis':'PDF heading; legacy hour filenames carry half-hour heading, not invented minute',
          'latest_known_before_cutoff':True,'report_age_at_cutoff_minutes':(cut-t.astimezone(timezone.utc)).total_seconds()/60,
          'latest_edition_exhaustively_proven':False,'search_completeness':'legacy hourly + 5-minute candidate scan over final65minutes; official index lacks historical listing',
          'layout_non_data_rows_filtered':dropped,'historical_local_capture_not_required':True,'strict_captured_before_cutoff':False}
    write(folder/'selected-report.json',data);return data
