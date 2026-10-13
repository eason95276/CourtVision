"""Bounded source-body fallback, independent of evidence qualification."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from .free_search_mcp_client import error_diagnostic
from .storage import now


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.jsonld = [], [], []
        self.skip, self.title, self.ld, self.published = 0, False, False, None
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag in {"script","style","noscript"}:
            self.skip += 1
            self.ld = tag=="script" and attrs.get("type","").casefold()=="application/ld+json"
        if tag=="title": self.title=True
        if tag=="meta" and (attrs.get("property") or attrs.get("name") or "").casefold() in {
            "article:published_time","datepublished","pubdate","date"}:
            self.published=attrs.get("content") or self.published
    def handle_endtag(self,tag):
        if tag in {"script","style","noscript"}: self.skip=max(0,self.skip-1);self.ld=False
        if tag=="title": self.title=False
        if tag in {"p","div","li","article","section","h1","h2","h3"}:self.parts.append("\n")
    def handle_data(self,data):
        if self.ld: self.jsonld.append(data)
        if self.title:self.title_parts.append(data)
        if not self.skip:self.parts.append(data)
    def result(self):
        article=None
        def inspect(value):
            nonlocal article
            if isinstance(value,list):
                for child in value:inspect(child)
            elif isinstance(value,dict):
                if value.get("articleBody"):
                    article=str(value["articleBody"])
                    self.published=value.get("datePublished") or self.published
                for child in value.values():
                    if isinstance(child,(dict,list)):inspect(child)
        for blob in self.jsonld:
            try:inspect(json.loads(blob))
            except (ValueError,TypeError):pass
        return {"title":" ".join(self.title_parts).strip(),
                "content":re.sub(r"[ \t]+"," ",article or "".join(self.parts)).strip(),
                "published_date":self.published}


def direct_fetch(url,timeout):
    from .web_search import _public_discovery_url
    if not _public_discovery_url(url):raise ValueError("unsafe_fetch_url")
    request=urllib.request.Request(url,headers={"User-Agent":"CourtVision-Research/2.0",
        "Accept":"text/html,application/xhtml+xml"})
    with urllib.request.urlopen(request,timeout=timeout) as response:
        returned=response.geturl()
        if not _public_discovery_url(returned):raise ValueError("unsafe_fetch_redirect")
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError("source_body_size_exceeded")
        charset=response.headers.get_content_charset() or "utf-8"
        html=raw.decode(charset,errors="replace")
        parser=PageText();parser.feed(html)
        value=parser.result();captured=now()
        from .source_temporal_metadata import extract_temporal_metadata
        temporal=extract_temporal_metadata(returned,html=html,body=value["content"],title=value["title"],fetched_at=captured)
        return {"url":returned,**value,"provider":"direct-http","method":"http-html",
                "http_status":response.status,"captured_at":captured,"temporal_metadata":temporal,
                "published_at":temporal["published_at"],"updated_at":temporal["updated_at"],
                "_temporal_html":html}


def tavily_extract(url,timeout):
    key=os.environ.get("TAVILY_API_KEY")
    if not key:raise ValueError("tavily_extract_not_configured")
    request=urllib.request.Request("https://api.tavily.com/extract",
        data=json.dumps({"api_key":key,"urls":[url],"extract_depth":"advanced","format":"text"}).encode("utf-8"),
        headers={"Content-Type":"application/json","User-Agent":"CourtVision-Research/2.0"},method="POST")
    with urllib.request.urlopen(request,timeout=timeout) as response:
        data=json.loads(response.read().decode("utf-8"))
    matches=[r for r in data.get("results") or [] if r.get("url","").rstrip("/")==url.rstrip("/")]
    if not matches:raise ValueError("tavily_extract_failed:"+json.dumps(data.get("failed_results") or [],ensure_ascii=False)[:600])
    row=matches[0]
    return {"url":row["url"],"content":row.get("raw_content") or "",
        "title":row.get("title"),"published_date":row.get("published_date"),
        **{k:row[k]for k in ("published_at","updated_at","datePublished","dateModified","date_published","updated_date")if k in row},
        "provider":"tavily-extract","method":"extract","captured_at":now()}


def readable(item):
    body=str(item.get("content") or "").strip()
    return len(body)>=100 and not re.search(r"^\s*(?:JavaScript is disabled|Access Denied|Just a moment)",body,re.I)


class SourceBodyFetcher:
    def __init__(self,primary,*,direct=direct_fetch,alternate=tavily_extract):
        self.primary,self.direct,self.alternate=primary,direct,alternate
        self.primary_unavailable=None
        from .free_search_mcp_client import FreeSearchMCPTransport
        self.metadata_probe_enabled=isinstance(primary,FreeSearchMCPTransport)

    @staticmethod
    def _temporal(item,deadline,*,probe_enabled=False):
        from .source_temporal_metadata import extract_temporal_metadata
        captured=item.get("captured_at") or now()
        temporal=item.get("temporal_metadata")
        if temporal is None:
            html=None;probe=None
            preliminary=extract_temporal_metadata(item["url"],provider=item,body=item.get("content",""),
                title=item.get("title",""),fetched_at=captured)
            provider_precise=(preliminary.get("published_at_precision")=="timestamp"
                              and preliminary.get("published_at_origin","").startswith("provider."))
            if probe_enabled and preliminary["page_kind"]=="article" and not provider_precise and deadline-time.perf_counter()>1:
                from .web_search import _public_discovery_url
                start=time.perf_counter()
                try:
                    request=urllib.request.Request(item["url"],headers={"User-Agent":"CourtVision-Research/2.0","Accept":"text/html"})
                    with urllib.request.urlopen(request,timeout=min(5,deadline-time.perf_counter())) as response:
                        if not _public_discovery_url(response.geturl()) or response.geturl().rstrip("/")!=item["url"].rstrip("/"):
                            raise ValueError("metadata_url_identity_mismatch")
                        raw=response.read(4*1024*1024)
                        html=raw.decode(response.headers.get_content_charset() or "utf-8",errors="replace")
                        probe={"operation":"temporal_metadata","url":item["url"],"http_status":response.status,
                               "fetched_at":now(),"seconds":time.perf_counter()-start}
                except Exception as exc:probe=error_diagnostic(exc,operation="temporal_metadata",url=item["url"],provider="direct-http-metadata")
            temporal=extract_temporal_metadata(item["url"],provider=item,html=html,body=item.get("content",""),
                title=item.get("title",""),fetched_at=captured)
            if probe:temporal["metadata_probe"]=probe
            if html:item={**item,"_temporal_html":html}
        return {**item,"captured_at":captured,"published_at":temporal["published_at"],
                "updated_at":temporal["updated_at"],"observed_at":temporal["observed_at"],
                "fetched_at":captured,"temporal_metadata":temporal}

    def fetch_many(self,urls,*,budget_seconds=60):
        urls=list(dict.fromkeys(str(url) for url in urls if url))
        deadline=time.perf_counter()+max(1,budget_seconds)
        original_timeout=getattr(self.primary,"timeout_seconds",None)
        started=time.perf_counter()
        if self.primary_unavailable:
            raw=[{"url":url,"content":"","diagnostic":{**self.primary_unavailable,"url":url,"skipped":True}} for url in urls]
        else:
            try:
                if original_timeout is not None:self.primary.timeout_seconds=min(original_timeout,20,max(1,budget_seconds/3))
                raw=self.primary.fetch_many(urls)
            except Exception as exc:
                raw=[{"url":url,"content":"","diagnostic":error_diagnostic(exc,operation="fetch",url=url)} for url in urls]
            finally:
                if original_timeout is not None:self.primary.timeout_seconds=original_timeout
        primary_seconds=time.perf_counter()-started
        by_url={r.get("url"):r for r in raw if isinstance(r,dict)}
        if any((r.get("diagnostic") or {}).get("failure_kind")=="process_exit" for r in by_url.values()):
            self.primary_unavailable=next(r["diagnostic"] for r in by_url.values()
                if (r.get("diagnostic") or {}).get("failure_kind")=="process_exit")

        def finish(url):
            item=by_url.get(url) or {"url":url,"content":"","diagnostic":{"failure_kind":"protocol","message":"primary_missing_result"}}
            attempts=[{"provider":"free-search-mcp","operation":"fetch","url":url,"seconds":primary_seconds,
                       "status":"success" if readable(item) else "failed",
                       "diagnostic":item.get("diagnostic") or item.get("errors")}]
            if readable(item):return self._temporal({**item,"fetch_attempts":attempts,"captured_at":item.get("captured_at") or now()},deadline,probe_enabled=self.metadata_probe_enabled)
            for provider,fetcher,cap in (("direct-http",self.direct,12),("tavily-extract",self.alternate,20)):
                if fetcher is None:continue
                remaining=deadline-time.perf_counter()
                if remaining<=0:
                    attempts.append({"provider":provider,"url":url,"status":"skipped","reason":"fetch_budget_exhausted"})
                    break
                start=time.perf_counter()
                try:
                    candidate=fetcher(url,min(cap,max(1,remaining)))
                    if not isinstance(candidate,dict) or not readable(candidate):
                        raise ValueError("source_body_empty_or_blocked")
                    attempts.append({"provider":provider,"url":url,"operation":"fetch","status":"success",
                                     "seconds":time.perf_counter()-start})
                    return self._temporal({**candidate,"fetch_attempts":attempts,"captured_at":candidate.get("captured_at") or now()},deadline,probe_enabled=self.metadata_probe_enabled)
                except Exception as exc:
                    attempts.append({"provider":provider,"url":url,"operation":"fetch","status":"failed",
                        "seconds":time.perf_counter()-start,
                        "diagnostic":error_diagnostic(exc,operation="fetch",url=url,provider=provider)})
            return {"url":url,"content":"","provider":"source-body-chain","captured_at":now(),
                    "errors":{"fetch":"all_body_providers_failed"},"fetch_attempts":attempts}
        with ThreadPoolExecutor(max_workers=min(4,max(1,len(urls)))) as pool:
            return list(pool.map(finish,urls))
