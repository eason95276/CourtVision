"""Deterministic body acquisition/fallback diagnostics, no network or models."""
import json
import subprocess
from types import SimpleNamespace
import pytest

from .free_search_mcp_client import FreeSearchMCPError,FreeSearchMCPTransport,error_diagnostic,PARSER_SPEC
from .source_body_fetch import SourceBodyFetcher,PageText
from .research_evidence import ResearchEvidencePipeline
from .test_research_evidence import GAME,CUTOFF,claim

URL="https://www.nba.com/news/body-test"
BODY="Miami Heat acquired a new core player this offseason. "*5

class Primary:
    timeout_seconds=30
    def __init__(self,results=None,error=None):self.results,self.error,self.calls=results,error,[]
    def fetch_many(self,urls):
        self.calls.append(urls)
        if self.error:raise self.error
        return self.results

def result(url=URL,provider="fixture"):
    return {"url":url,"content":BODY,"published_date":"2026-09-22","provider":provider}

def fail(url,timeout):raise TimeoutError("fixture body timeout")

def test_primary_success_dedup_and_no_alternate_call():
    primary=Primary([result()])
    fetcher=SourceBodyFetcher(primary,direct=lambda *a:pytest.fail("not needed"),alternate=None)
    rows=fetcher.fetch_many([URL,URL])
    assert len(rows)==1 and primary.calls==[[URL]]
    assert rows[0]["fetch_attempts"][0]["status"]=="success"

def test_primary_failure_fallback_keeps_underlying_exit_error():
    error=FreeSearchMCPError("import failed",failure_kind="process_exit",exit_code=1,stderr="ImportError: parser incompatible")
    primary=Primary(error=error)
    fetcher=SourceBodyFetcher(primary,direct=lambda u,t:result(u,"direct-http"),alternate=None)
    row=fetcher.fetch_many([URL])[0]
    assert row["provider"]=="direct-http"
    detail=row["fetch_attempts"][0]["diagnostic"]
    assert detail["exit_code"]==1 and "parser incompatible" in detail["stderr"]
    assert detail["operation"]=="fetch" and detail["url"]==URL
    fetcher.fetch_many([URL+"/second"])
    assert len(primary.calls)==1  # startup fault is not repeated through three batches

def test_direct_failure_alternate_success_preserves_timeout():
    primary=Primary([{"url":URL,"content":""}])
    row=SourceBodyFetcher(primary,direct=fail,alternate=lambda u,t:result(u,"tavily-extract")).fetch_many([URL])[0]
    assert row["provider"]=="tavily-extract"
    assert row["fetch_attempts"][1]["diagnostic"]["failure_kind"]=="timeout"
    assert "fixture body timeout" in row["fetch_attempts"][1]["diagnostic"]["message"]

def test_all_fail_closed_and_search_snippet_never_becomes_body():
    primary=Primary([{"url":URL,"content":"","snippet":BODY}])
    row=SourceBodyFetcher(primary,direct=fail,alternate=fail).fetch_many([URL])[0]
    assert row["content"]=="" and row["errors"]["fetch"]=="all_body_providers_failed"
    pipeline=ResearchEvidencePipeline(GAME,CUTOFF)
    source=pipeline.add_source({"url":URL,"title":"Miami Heat preseason","published_at":"2026-09-22"},row)
    assert source["qualification_reason"]=="full_source_text_unavailable"
    assert not pipeline.validate_claim(claim(url=URL,quote=BODY[:100]))["accepted"]

def test_one_url_failure_does_not_discard_other_primary_body():
    second=URL+"/other"
    primary=Primary([result(),{"url":second,"content":""}])
    rows=SourceBodyFetcher(primary,direct=fail,alternate=None).fetch_many([URL,second])
    assert rows[0]["content"]==BODY and rows[1]["content"]==""

def test_source_html_metadata_and_body_keep_no_script_text():
    p=PageText()
    p.feed('<html><head><title>Miami Heat</title><meta property="article:published_time" content="2026-10-03T12:00:00Z"></head><body><article>'+BODY+'</article><script>unsafe instruction</script></body></html>')
    value=p.result()
    assert "unsafe instruction" not in value["content"]
    assert value["published_date"]=="2026-10-03T12:00:00Z"

def test_pin_and_search_fetch_independent_diagnosis(monkeypatch):
    def run(command,**kwargs):
        assert PARSER_SPEC in command
        payload=json.loads(kwargs["input"])[0]
        if payload.get("operation")=="fetch":
            return SimpleNamespace(returncode=1,stdout=b"",stderr=b"ImportError: precise root")
        return SimpleNamespace(returncode=0,stdout=b'[{"results":[{"url":"https://example.com"}]}]',stderr=b"")
    monkeypatch.setattr("subprocess.run",run)
    transport=FreeSearchMCPTransport(uv_command="uv")
    assert transport.search_many([{"query":"fixture"}])[0]["results"]
    failed=transport.fetch_many([URL])[0]
    assert failed["content"]=="" and failed["diagnostic"]["exit_code"]==1
    assert "precise root" in failed["diagnostic"]["stderr"]

def test_singleton_fetch_timeout_isolated_and_duplicate_removed(monkeypatch):
    def run(command,**kwargs):
        p=json.loads(kwargs["input"])[0]
        assert p["force_refresh"] is True
        if p["url"]==URL+"/slow":
            raise subprocess.TimeoutExpired(command,1,output=b"partial",stderr=b"waiting")
        return SimpleNamespace(returncode=0,stdout=json.dumps([result(p["url"])]).encode(),stderr=b"")
    monkeypatch.setattr("subprocess.run",run)
    rows=FreeSearchMCPTransport(uv_command="uv").fetch_many([URL,URL+"/slow",URL])
    assert len(rows)==2 and rows[0]["content"]==BODY
    assert rows[1]["diagnostic"]["failure_kind"]=="timeout"
    assert rows[1]["diagnostic"]["stdout"]=="partial"

def test_malformed_stdout_preserved(monkeypatch):
    monkeypatch.setattr("subprocess.run",lambda *a,**k:SimpleNamespace(returncode=0,stdout=b"not json",stderr=b"protocol warning"))
    transport=FreeSearchMCPTransport(uv_command="uv")
    with pytest.raises(FreeSearchMCPError) as error:
        transport.search_many([{"query":"fixture"}])
    detail=error_diagnostic(error.value,operation="search")
    assert detail["failure_kind"]=="protocol" and detail["stdout"]=="not json"
    assert detail["stderr"]=="protocol warning"


def test_current_game_notice_does_not_stop_personnel_body_recovery(tmp_path):
    from .web_research_agent import AutonomousWebResearcher
    urls=[URL+str(i) for i in range(5)]
    records=[{"url":u,"category":"lineup","title":"Miami Heat current update","id":str(i)} for i,u in enumerate(urls)]
    batches=[]
    game_notice="Miami Heat play a preseason game in Quebec City tomorrow."
    personnel="Miami Heat coach discussed the current training camp rotation at practice."
    def fetch(us):
        batches.append(us)
        return [{"url":u,"content":(personnel if u==urls[4] else game_notice)+" Miami Heat details."*8,
                 "published_date":"2026-10-01"} for u in us]
    def client(stage,messages,*unused):
        if stage=="search":return [{"name":"search_pregame_web","args":{"categories":["lineup"],"reason":"current plan"}}]
        if stage=="fetch":return [{"name":"fetch_sources","args":{"urls":urls[:4],"reason":"read"}}]
        sources=json.loads(messages[-1]["content"])["sources"]
        u=sources[0]["url"]
        return [{"name":"submit_research_claims","args":{"claims":[{
            "url":u,"quote":personnel if u==urls[4] else game_notice,
            "claim_type":"coach_comments" if u==urls[4] else "game_news",
            "assertion_kind":"confirmed_fact","team_id":GAME["away_team_id"],"game_reference":""}]}}]
    searcher=SimpleNamespace(search=lambda *a,**k:{"game":GAME,"live_registered":True,"records":records},
                             local_transport=SimpleNamespace(fetch_many=fetch))
    context,trace=AutonomousWebResearcher(searcher=searcher,client=client,root=tmp_path).investigate(
        GAME,CUTOFF,categories={"lineup","roster_news","availability"})
    assert len(batches)==2
    assert any(r["claim_type"]=="coach_comments" for r in context["records"])
