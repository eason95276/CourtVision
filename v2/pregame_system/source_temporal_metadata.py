"""Source-scoped publication/update metadata; retrieval is never publication."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def page_kind(url,title=""):
    parsed=urlparse(url)
    path=parsed.path.casefold()
    if re.search(r"/(?:articles?|news|story)(?:/|$)",path):return "article"
    # The Athletic's NYT-hosted URLs use /athletic/<id>/YYYY/MM/DD/slug,
    # which is an article route even though it does not contain /article/.
    if re.search(r"^/athletic/\d+/20\d{2}/\d{2}/\d{2}/",path):return "article"
    if (parsed.hostname or "").casefold().removeprefix("www.") == "si.com" and re.fullmatch(
            r"/nba/[^/]+/onsi/(?!author(?:/|$)|archive(?:/|$)|category(?:/|$))[^/]+/?", path):
        return "article"
    if "roster" in path and re.search(r"roster|名单",title,re.I):return "current_roster_snapshot"
    if "depth" in path and re.search(r"depth|lineup|阵容",title,re.I):return "current_depth_snapshot"
    if re.search(r"/(?:game|gametracker|team|injuries)(?:/|$)",path):return "current_status_snapshot"
    return "unknown"


def normalize_time(value):
    if value is None or isinstance(value,bool):return None,"unknown"
    raw=str(value).strip()
    try:
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}",raw):
            datetime.strptime(raw,"%Y-%m-%d");return raw,"date"
        parsed=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if parsed.tzinfo is not None:return parsed.astimezone(timezone.utc).isoformat(),"timestamp"
        return raw,"datetime_unzoned"
    except ValueError:pass
    try:
        parsed=parsedate_to_datetime(raw)
        if parsed.tzinfo is not None:return parsed.astimezone(timezone.utc).isoformat(),"timestamp"
    except (TypeError,ValueError):pass
    return None,"unknown"


def _canonical(url):
    p=urlparse(str(url));return p.netloc.casefold().removeprefix("www.")+p.path.rstrip("/")


class TemporalHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta=[];self.times=[];self.scripts=[];self.script=None;self.title=[];self.in_title=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="meta":self.meta.append(a)
        if tag=="time" and a.get("datetime"):self.times.append(a)
        if tag=="script":self.script={"attrs":a,"parts":[]}
        if tag=="title":self.in_title=True
    def handle_data(self,data):
        if self.script is not None:self.script["parts"].append(data)
        if self.in_title:self.title.append(data)
    def handle_endtag(self,tag):
        if tag=="script" and self.script is not None:self.scripts.append(self.script);self.script=None
        if tag=="title":self.in_title=False


def _byline_times(url,body):
    host=urlparse(url).hostname or ""
    if not any(host.endswith(d)for d in ("yahoo.com","espn.com","nba.com")):return []
    months={name:i for i,name in enumerate(["","january","february","march","april","may","june","july","august","september","october","november","december"])}
    found=[]
    pattern=re.compile(r"^(?P<label>Updated|Published)?\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s*)?(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<day>\d{1,2}),\s+(?P<year>20\d{2})(?:(?:,|\s+at)\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\s*(?P<zone>UTC|GMT|ET|EST|EDT|PT|PST|PDT))?",re.I)
    for line in str(body or "")[:6000].splitlines():
        match=pattern.match(line.strip())
        if not match:continue
        d=match.groupdict();month=next(i for name,i in months.items()if name and name.startswith(d["month"].casefold()))
        day=int(d["day"]);year=int(d["year"])
        try:
            if d["hour"]:
                hour=int(d["hour"])%12+(12 if d["ampm"].upper()=="PM" else 0)
                zone=d["zone"].upper()
                tz=timezone.utc if zone in {"UTC","GMT"} else ZoneInfo("America/New_York" if zone in {"ET","EST","EDT"} else "America/Los_Angeles")
                stamp=datetime(year,month,day,hour,int(d["minute"]),tzinfo=tz).astimezone(timezone.utc).isoformat()
            else:stamp=f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:continue
        found.append(("updated_at" if (d["label"] or "").casefold()=="updated" else "published_at",stamp,line.strip()))
    return found


def extract_temporal_metadata(url,*,provider=None,html=None,body="",title="",fetched_at=None):
    provider=provider or {};candidates=[]
    parser=TemporalHTML()
    if html:parser.feed(html)
    title=title or " ".join(parser.title)
    kind=page_kind(url,title+" "+str(body or "")[:300])
    def add(field,raw,origin,rank):
        value,precision=normalize_time(raw)
        if value:candidates.append({"field":field,"value":value,"precision":precision,"origin":origin,"rank":rank,"raw":str(raw)})
    # Generic date/timestamp keys have no publication meaning without a type.
    if kind=="article":
        for field,keys in (("published_at",("published_at","published_date","date_published","datePublished")),
                           ("updated_at",("updated_at","updated_date","modified_at","dateModified"))):
            for key in keys:
                if provider.get(key):add(field,provider[key],"provider."+key,0)
        if provider.get("timestamp_type") in {"published","updated"}:
            add(provider["timestamp_type"]+"_at",provider.get("timestamp") or provider.get("date"),"provider.typed_timestamp",0)
        for meta in parser.meta:
            key=(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").casefold()
            if key in {"article:published_time","datepublished","pubdate","publishdate"}:add("published_at",meta.get("content"),"html_meta."+key,1)
            if key in {"article:modified_time","datemodified","last-modified","modifieddate"}:add("updated_at",meta.get("content"),"html_meta."+key,1)
        articles=[]
        def walk(value):
            if isinstance(value,list):
                for child in value:walk(child)
            elif isinstance(value,dict):
                types=value.get("@type") or []
                if isinstance(types,str):types=[types]
                if any(t in {"Article","NewsArticle","BlogPosting"}for t in types):articles.append(value)
                for child in value.values():
                    if isinstance(child,(dict,list)):walk(child)
        for script in parser.scripts:
            if script["attrs"].get("type","").casefold()=="application/ld+json":
                try:walk(json.loads("".join(script["parts"])))
                except (ValueError,TypeError):pass
        for article in articles:
            entity=article.get("mainEntityOfPage")
            ident=article.get("url") or (entity.get("@id")if isinstance(entity,dict)else entity) or article.get("@id")
            if ident and _canonical(ident)!=_canonical(url):continue
            if not ident and len(articles)>1:continue
            add("published_at",article.get("datePublished"),"jsonld.article.datePublished",2)
            add("updated_at",article.get("dateModified"),"jsonld.article.dateModified",2)
        for node in parser.times:
            label=(node.get("itemprop","")+" "+node.get("class","")).casefold()
            if "modified" in label or "updated" in label:field="updated_at"
            elif "published" in label or len(parser.times)==1:field="published_at"
            else:continue
            add(field,node["datetime"],"html_time.datetime",3)
        for field,value,raw in _byline_times(url,body):
            add(field,value,"source_byline."+urlparse(url).hostname,4)
        match=re.search(r"/(20\d{2})[-/](\d{2})[-/](\d{2})(?:/|[-_.]|$)",url)
        if match:add("published_at","-".join(match.groups()),"source_url.date",4)
    if kind.startswith("current_"):
        for key in ("updated_at","updated_date","modified_at","dateModified"):
            if provider.get(key):add("updated_at",provider[key],"provider."+key,0)
        for meta in parser.meta:
            key=(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").casefold()
            if key in {"article:modified_time","datemodified","last-modified","modifieddate"}:
                add("updated_at",meta.get("content"),"html_meta."+key,1)
        def webpage(value):
            if isinstance(value,list):
                for child in value:webpage(child)
            elif isinstance(value,dict):
                typ=value.get("@type");ident=value.get("url") or value.get("@id")
                if typ=="WebPage" and ident and _canonical(ident)==_canonical(url):
                    add("updated_at",value.get("dateModified"),"jsonld.webpage.dateModified",2)
                for child in value.values():
                    if isinstance(child,(dict,list)):webpage(child)
        for script in parser.scripts:
            if script["attrs"].get("type","").casefold()=="application/ld+json":
                try:webpage(json.loads("".join(script["parts"])))
                except (TypeError,ValueError):pass
    result={"published_at":None,"updated_at":None,"observed_at":None,"fetched_at":fetched_at,
            "page_kind":kind,"temporal_candidates":candidates,
            "html_sha256":hashlib.sha256(html.encode("utf-8")).hexdigest() if html else None}
    for field in ("published_at","updated_at"):
        rows=sorted((c for c in candidates if c["field"]==field),key=lambda c:c["rank"])
        if not rows:continue
        selected=rows[0]
        if selected["precision"]=="date":
            finer=next((c for c in rows if c["precision"]=="timestamp" and c["value"][:10]==selected["value"]),None)
            if finer:selected=finer
        result[field]=selected["value"];result[field+"_precision"]=selected["precision"]
        result[field+"_origin"]=selected["origin"]
    if kind.startswith("current_") and fetched_at:
        result["observed_at"]=fetched_at
        result["observed_at_origin"]="actual_source_capture"
    return result
