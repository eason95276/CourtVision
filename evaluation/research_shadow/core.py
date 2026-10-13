"""Shadow-only qualification and descriptive retrieval metrics, no model calls.

An exact source excerpt is a research lead, NOT a certified basketball fact.
Nothing in this module returns a production AnalystContext/evidence record.
"""
from __future__ import annotations
import hashlib,json,re
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl,urlencode,urlparse,urlunparse

REQUIRED_FIELDS=set('source source_type channel title url published_at timestamp_source timestamp_confidence fetched_at cutoff cutoff_pass historical_snapshot_proof source_tier duplicate_with_existing fetch_status claim_extract_status eligible_for_hard_fact rejection_reason'.split())
PROFESSIONAL={'espn.com','apnews.com','reuters.com','cbssports.com','sports.yahoo.com','sportsnet.ca','tsn.ca','nbcsports.com','si.com','theathletic.com','nytimes.com','miamiherald.com','thestar.com','sun-sentinel.com'}
OFFICIAL={'nba.com','raptors.com'}
TOPICS={'roster_availability':r'\b(?:roster|injur\w*|ruled out|questionable|doubtful|availability|starters?|lineup|minutes?|return|rotation)\b|首发|伤病|出战|缺阵|轮换|分钟|名单',
        'coach_tactical':r'\b(?:coach|Kerr|Redick|Spoelstra|Rajakovic|interview|pick.and.roll|switch\w*|defen\w*|spacing|rim|rebound\w*|coverage|transition|screen\w*)\b|教练|采访|挡拆|换防|护框|篮板|防守|对位'}
DATE_REF=r'\b(?:Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?)\s+\d{1,2}\b|20\d\d[-/]\d\d[-/]\d\d|\d{1,2}月\d{1,2}日'

def sha(value):return hashlib.sha256(value.encode('utf-8')if isinstance(value,str)else value).hexdigest()
def stamp(value):
    if not value:return None
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except ValueError:
        try:d=parsedate_to_datetime(str(value))
        except (ValueError,TypeError):return None
    return d.astimezone(timezone.utc)if d.tzinfo is not None else None
def normalized_time(value):
    if value and re.fullmatch(r'20\d\d-\d\d-\d\d',str(value)):
        try:datetime.strptime(value,'%Y-%m-%d');return value,'date'
        except ValueError:return None,'unknown'
    d=stamp(value)
    return (d.isoformat(),'timestamp')if d else(None,'unknown')
def bounds(value):
    v,p=normalized_time(value)
    if p=='timestamp':return stamp(v),stamp(v)
    if p=='date':
        d=datetime.strptime(v,'%Y-%m-%d').replace(tzinfo=timezone.utc)
        return d-timedelta(hours=14),d+timedelta(days=1,hours=12)
    return None,None

def canonical_url(url):
    p=urlparse(str(url));host=(p.hostname or'').casefold().removeprefix('www.')
    q=sorted((k,v)for k,v in parse_qsl(p.query,keep_blank_values=True)if not(k.casefold().startswith('utm_')or k.casefold()in {'fr','ref','fbclid','gclid','f'}))
    return urlunparse(('https',host,p.path.rstrip('/')or'/', '',urlencode(q),''))
def source_tier(url):
    host=(urlparse(url).hostname or'').casefold().removeprefix('www.')
    if any(host==d or host.endswith('.'+d)for d in OFFICIAL):return 'TIER_A'
    if any(host==d or host.endswith('.'+d)for d in PROFESSIONAL):return 'TIER_B'
    return 'TIER_C'

def candidate(*,source,source_type,channel,title,url,cutoff,published_at=None,timestamp_source='unavailable',**extra):
    item={'source':source,'source_type':source_type,'channel':channel,'title':title,'url':url,
      'published_at':published_at,'updated_at':None,'timestamp_source':timestamp_source,
      'timestamp_confidence':'provider_unverified'if published_at else'unavailable',
      'source_timestamp_verified':False,'fetched_at':None,'cutoff':cutoff,'cutoff_pass':False,
      'publication_before_cutoff':False,'historical_snapshot_proof':{'verified':False,'reason':'no_pinned_pre_cutoff_snapshot'},
      'source_tier':source_tier(url),'duplicate_with_existing':False,'fetch_status':'not_attempted','fetch_attempted':False,
      'claim_extract_status':'not_attempted','eligible_for_hard_fact':False,'rejection_reason':None,
      'rejection_reasons':[],'shadow_only':True,'snippets_are_evidence':False,'shadow_claims':[]}
    item.update(extra)
    item['discovery_provenance']=[{'source':source,'channel':channel,'url':url,'published_at':published_at,'timestamp_source':timestamp_source,'query':extra.get('query')}]
    return item

def deduplicate(rows,*,existing=()):
    old={canonical_url(r['url'])for r in existing};kept={}
    for row in rows:
        key=canonical_url(row['url'])
        if key not in kept:kept[key]={**row,'discovery_provenance':list(row['discovery_provenance'])}
        else:
            kept[key]['discovery_provenance'].extend(row['discovery_provenance'])
            if not kept[key]['published_at']and row['published_at']:
                for f in ('published_at','timestamp_source','timestamp_confidence'):kept[key][f]=row[f]
        kept[key]['duplicate_with_existing']=key in old
        kept[key]['canonical_url']=key
    return list(kept.values())

def verify_snapshot(path,expected_hash,*,url,body,cutoff):
    """Verify an externally pinned immutable receipt; not a claimed flag."""
    failure={'verified':False,'reason':'snapshot_receipt_not_verified'}
    try:
        raw=Path(path).read_bytes()
        if sha(raw)!=expected_hash:return failure
        r=json.loads(raw);captured=stamp(r['captured_at'])
        if not captured or captured>stamp(cutoff)or canonical_url(r['url'])!=canonical_url(url)or r['body']!=body:return failure
        return {'verified':True,'verified_by':'pinned_receipt_bytes_v1','receipt_path':str(path),
                'receipt_sha256':expected_hash,'url':url,'captured_at':r['captured_at'],'content_sha256':sha(body)}
    except (OSError,ValueError,KeyError,TypeError):return failure

def qualify(row,*,game):
    reasons=[];cut=stamp(row['cutoff']);tip=stamp(game.get('scheduled_tipoff'));capture=stamp(row.get('fetched_at'))
    _,latest=bounds(row.get('published_at'));_,updated=bounds(row.get('updated_at'))
    row['publication_before_cutoff']=bool(cut and latest and latest<=cut)
    if not cut:reasons.append('invalid_cutoff')
    if not latest:reasons.append('publication_time_unavailable')
    elif latest>cut:reasons.append('publication_not_proven_before_cutoff')
    if updated and updated>cut:reasons.append('updated_after_cutoff')
    if not row.get('source_timestamp_verified'):reasons.append('publication_origin_unverified')
    proof=row.get('historical_snapshot_proof')or{}
    proven=bool(proof.get('verified')and proof.get('verified_by')=='pinned_receipt_bytes_v1'
                and proof.get('receipt_sha256')and proof.get('content_sha256')==row.get('content_sha256')
                and stamp(proof.get('captured_at'))and stamp(proof['captured_at'])<=cut
                and canonical_url(proof.get('url',''))==canonical_url(row['url']))
    if not capture:reasons.append('source_capture_unavailable')
    elif capture>cut and not proven:reasons.append('historical_snapshot_unproven')
    if tip and cut and cut>=tip:reasons.append('cutoff_not_pregame')
    row['cutoff_pass']=bool(cut and latest and latest<=cut and(not updated or updated<=cut)
        and row.get('source_timestamp_verified')and((capture and capture<=cut)or proven)
        and(not tip or cut<tip))
    if row['source_tier']=='TIER_C':reasons.append('source_tier_c')
    if row['fetch_status']!='success':reasons.append('original_body_not_available')
    if row.get('identity_match')is False:reasons.append('target_team_identity_not_in_body')
    if row.get('is_recap'):reasons.append('target_postgame_content')
    row['eligible_for_hard_fact']=bool(row['cutoff_pass']and row['source_tier']!='TIER_C'
          and row['fetch_status']=='success'and row.get('identity_match',True)and not row.get('is_recap'))
    row['rejection_reasons']=list(dict.fromkeys(reasons));row['rejection_reason']=';'.join(row['rejection_reasons'])or None
    row['hard_fact_status']='QUALIFICATION_CANDIDATE_NOT_CERTIFIED'if row['eligible_for_hard_fact']else'NOT SAFE FOR HARD FACT'
    return row

class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.meta=[];self.times=[];self.scripts=[]
        self.script=None;self.skip=[];self.title=[];self.in_title=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta':self.meta.append(a)
        if tag=='time'and a.get('datetime'):self.times.append(a)
        if tag=='script':self.script={'attrs':a,'parts':[]}
        if tag in {'script','style','noscript','nav','footer','header','aside'}:self.skip.append(tag)
        if tag=='title':self.in_title=True
        if tag in {'p','li','h1','h2','h3','div','article','section'}and not self.skip:self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag=='script'and self.script is not None:self.scripts.append(self.script);self.script=None
        if tag in self.skip:
            index=len(self.skip)-1-self.skip[::-1].index(tag);self.skip=self.skip[:index]
        if tag=='title':self.in_title=False
        if tag in {'p','li','div','article','section'}and not self.skip:self.parts.append('\n')
    def handle_data(self,data):
        if self.script is not None:self.script['parts'].append(data)
        if self.in_title:self.title.append(data)
        if not self.skip and not self.in_title:self.parts.append(data)

def extract_article(html,url):
    p=ArticleParser();p.feed(html);title=' '.join(p.title);times=[];article_bodies=[]
    def add(field,value,origin,rank):
        v,precision=normalized_time(value)
        if v:times.append({'field':field,'value':v,'precision':precision,'origin':origin,'rank':rank})
    for m in p.meta:
        key=(m.get('property')or m.get('name')or m.get('itemprop')or'').casefold()
        if key in {'article:published_time','datepublished','pubdate','publishdate'}:add('published_at',m.get('content'),'html_meta.'+key,0)
        if key in {'article:modified_time','datemodified','modifieddate'}:add('updated_at',m.get('content'),'html_meta.'+key,0)
    def walk(v):
        if isinstance(v,list):
            for child in v:walk(child)
        elif isinstance(v,dict):
            typ=v.get('@type');types=[typ]if isinstance(typ,str)else typ or[]
            if any(t in {'Article','NewsArticle','BlogPosting'}for t in types):
                entity=v.get('mainEntityOfPage');ident=v.get('url')or(entity.get('@id')if isinstance(entity,dict)else entity)or v.get('@id')
                headline=str(v.get('headline')or'').strip()
                identity=(bool(ident)and canonical_url(ident)==canonical_url(url))or(not ident and headline and headline in title)
                if identity:
                    add('published_at',v.get('datePublished'),'jsonld.article.datePublished',1)
                    add('updated_at',v.get('dateModified'),'jsonld.article.dateModified',1)
                    if v.get('articleBody'):article_bodies.append(str(v['articleBody']))
            for child in v.values():
                if isinstance(child,(dict,list)):walk(child)
    for s in p.scripts:
        if s['attrs'].get('type','').casefold()=='application/ld+json':
            try:walk(json.loads(''.join(s['parts'])))
            except (ValueError,TypeError):pass
    if len(p.times)==1:add('published_at',p.times[0]['datetime'],'html_time.single',2)
    result={'title':title,'body':re.sub(r'[ \t]+',' ',article_bodies[0]if article_bodies else ''.join(p.parts)).strip(),
        'published_at':None,'updated_at':None,'timestamp_source':'unavailable','timestamp_confidence':'unavailable','source_timestamp_verified':False,'temporal_candidates':times}
    for field in ('published_at','updated_at'):
        choices=sorted((t for t in times if t['field']==field),key=lambda x:x['rank'])
        if choices:
            t=choices[0];result[field]=t['value']
            if field=='published_at':result.update(timestamp_source=t['origin'],timestamp_confidence='original_precise'if t['precision']=='timestamp'else'original_date',source_timestamp_verified=True)
    return result

def aliases(game,side):
    full=game[side+'_team'];short=full.split()[-1]
    return [full,short]+({'76ers':['Philadelphia','76人'],'Lakers':['湖人'],'Warriors':['勇士'],'Heat':['热火'],'Raptors':['猛龙'],'Knicks':['尼克斯']}.get(short,[]))
def mentions(text,names):return any(re.search(r'(?<!\w)'+re.escape(n)+r'(?!\w)',text,re.I)if n.isascii()else n in text for n in names)
def extract_leads(row,body,game):
    if row.get('fetch_status')!='success':return []
    quotes=[]
    # Keep exact paragraph/sentence spans. No fabricated causal/statistical claim.
    for part in re.split(r'\n+',body):
        if len(part)<35 or len(part)>1600:continue
        home=mentions(part,aliases(game,'home'));away=mentions(part,aliases(game,'away'))
        if not(home or away):continue
        topics=[k for k,pattern in TOPICS.items()if re.search(pattern,part,re.I)]
        if home and away and re.search(DATE_REF,part,re.I)and re.search(r'preview|preseason|tonight|matchup|季前|赛前',part,re.I):topics.append('matchup_specific')
        if topics:
            quotes.append({'id':sha(row['url']+'\n'+part)[:20],'quote':part,'topics':topics,'url':row['url'],
                  'assertion_kind':'research_excerpt_not_certified_fact','eligible_for_hard_fact':row['eligible_for_hard_fact']})
        if len(quotes)>=6:break
    return quotes

def metrics(rows,*,raw_count,existing,latency_seconds=None):
    count=len(rows);fetched=[r for r in rows if r.get('fetch_attempted')];success=[r for r in fetched if r['fetch_status']=='success']
    quotes=[q for r in rows for q in r.get('shadow_claims',[])]
    temporal_reasons={'invalid_cutoff','publication_time_unavailable','publication_not_proven_before_cutoff','updated_after_cutoff','publication_origin_unverified','source_capture_unavailable','historical_snapshot_unproven','cutoff_not_pregame'}
    temporal_rejected=sum(bool(temporal_reasons&set(r.get('rejection_reasons',[])))for r in rows)
    result={'raw_candidate_count':raw_count,'candidate_count':count,'unique_new_urls':sum(not r['duplicate_with_existing']for r in rows),
       'duplicate_excess':max(0,raw_count-count),'duplicate_with_existing_count':sum(r['duplicate_with_existing']for r in rows),
       'duplicate_rate':(raw_count-count)/raw_count if raw_count else None,
       'published_at_count':sum(bool(r['published_at'])for r in rows),'reliable_published_at_count':sum(bool(r.get('source_timestamp_verified'))for r in rows),
       'published_at_coverage':sum(bool(r['published_at'])for r in rows)/count if count else None,
       'cutoff_verifiable_count':sum(r['cutoff_pass']for r in rows),'cutoff_verifiable_rate':sum(r['cutoff_pass']for r in rows)/count if count else None,
       'hard_fact_eligible_count':sum(r['eligible_for_hard_fact']for r in rows),
       'existing_accepted_style_evidence_count':sum(q['eligible_for_hard_fact']for q in quotes),
       'shadow_potentially_useful_evidence_count':sum(len(r.get('shadow_claims',[]))for r in rows if not r['duplicate_with_existing']and not r.get('is_recap')),
       'research_lead_only_count':sum(not r['eligible_for_hard_fact']for r in rows),
       'fetch_attempt_count':len(fetched),'fetch_success_count':len(success),'fetch_success_rate':len(success)/len(fetched)if fetched else None,
       'temporal_rejection_count':temporal_rejected,'temporal_rejection_rate':temporal_rejected/count if count else None,
       'latency_seconds':latency_seconds,'excerpts_are_semantically_certified':False}
    for tier in ('TIER_A','TIER_B','TIER_C'):result[tier+'_count']=sum(r['source_tier']==tier for r in rows)
    for topic in ('matchup_specific','roster_availability','coach_tactical'):
        result[topic+'_information_count']=sum(topic in q['topics']for q in quotes)
        result[topic+'_url_count']=sum(any(topic in q['topics']for q in r.get('shadow_claims',[]))for r in rows)
    return result

def compare_excerpts(existing,shadow):
    """Exact-normalized text overlap, explicitly not semantic novelty."""
    def fingerprint(q):return sha(' '.join(re.findall(r'\w+',q['quote'].casefold())))
    old={fingerprint(q)for r in existing if not r.get('is_recap')for q in r.get('shadow_claims',[])}
    seen=set();new=[];duplicates=0
    for row in shadow:
        if row.get('duplicate_with_existing')or row.get('is_recap'):continue
        for q in row.get('shadow_claims',[]):
            key=fingerprint(q);q['duplicate_text_with_existing']=key in old
            q['exact_text_fingerprint']=key
            if key in old or key in seen:duplicates+=1
            else:new.append({'url':q['url'],'quote':q['quote'],'topics':q['topics'],'fingerprint':key})
            seen.add(key)
    return {'unique_new_exact_excerpts':len(new),'duplicate_exact_excerpts':duplicates,'new_excerpts':new,
            'semantic_novel_information_count':None,'semantic_novelty_status':'NOT_EVALUATED_REQUIRES_HUMAN',
            'unique_verified_hard_facts':0}

def aggregate(items):
    total={}
    for key in items[0]if items else []:
        if key.endswith('_count')or key in {'unique_new_urls','duplicate_excess'}:total[key]=sum(m.get(key,0)for m in items)
    total['latency_seconds_sum']=sum(m.get('latency_seconds')or 0 for m in items)
    for field,numerator,denominator in (
        ('duplicate_rate','duplicate_excess','raw_candidate_count'),('published_at_coverage','published_at_count','candidate_count'),
        ('cutoff_verifiable_rate','cutoff_verifiable_count','candidate_count'),('fetch_success_rate','fetch_success_count','fetch_attempt_count'),
        ('temporal_rejection_rate','temporal_rejection_count','candidate_count')):
        total[field]=total.get(numerator,0)/total[denominator]if total.get(denominator)else None
    total['excerpts_are_semantically_certified']=False
    return total
