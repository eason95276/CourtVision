"""Metadata-only replay of frozen Research bodies and claims; no search/models."""
import json
import time
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .source_temporal_metadata import extract_temporal_metadata
from .research_evidence import ResearchEvidencePipeline
from .storage import new_run,save,digest,content_hash,now

ROOT=Path(__file__).parent/"outputs"
RUNS={
    "research_only":ROOT/"heat_research_only_acceptance/20261003T170152_320a4d08e7",
    "operator":ROOT/"heat_raptors_operator_acceptance/20261003T170836_a25a788f31",
}

def main():
    root=new_run("source_temporal_replay");start=time.perf_counter()
    inventory={str(p):digest(p)for old in RUNS.values()for p in old.rglob("*")if p.is_file()}
    runs={};unique={}
    for name,old in RUNS.items():
        result=json.loads((old/"research_result.json").read_text(encoding="utf-8"))
        files=list(old.glob("research/*/source_fetch_receipt_*.json"))
        file=max(files,key=lambda p:int(p.stem.rsplit("_",1)[1]))
        receipt=json.loads(file.read_text(encoding="utf-8"))
        runs[name]={"context":result["context"],"sources":receipt["sources"],"decisions":receipt["decisions"]}
        for source in receipt["sources"]:
            if source["content_chars"]>=100:unique.setdefault(source["url"],source)
    cache={}
    for file in (ROOT/"source_temporal_metadata_probe").glob("*/*.json"):
        try:
            r=json.loads(file.read_text(encoding="utf-8"))
            if r.get("url") and r.get("raw_path"):cache[r["url"]]=r
        except (ValueError,OSError):pass
    def metadata(source):
        url=source["url"];record=cache.get(url)
        if record:
            html=Path(record["raw_path"]).read_text(encoding="utf-8",errors="replace")if record.get("http_status")==200 else None
        else:
            record={"url":url,"operation":"metadata_only","fetched_at":now()}
            try:
                response=requests.get(url,headers={"User-Agent":"CourtVision-Research/2.0"},timeout=(4,8))
                raw=root/(content_hash(url)[:12]+".html");raw.write_bytes(response.content)
                record.update(http_status=response.status_code,returned_url=response.url,
                    raw_path=str(raw),raw_sha256=digest(raw))
                html=response.text if response.status_code==200 and response.url.rstrip("/")==url.rstrip("/") else None
            except Exception as exc:record.update(exception_type=type(exc).__name__,message=str(exc)[:800]);html=None
        temporal=extract_temporal_metadata(url,provider={"published_date":source.get("published_at")},
            html=html,body=source["body"],title=source["title"],fetched_at=source["captured_at"])
        temporal["metadata_probe"]=record
        save(root/(content_hash(url)[:12]+".json"),{"original":source,"normalized":temporal,"metadata_probe":record})
        return url,(html,record)
    with ThreadPoolExecutor(max_workers=4)as pool:parsed=dict(pool.map(metadata,unique.values()))
    by_url={};results={}
    for name,run in runs.items():
        context=run["context"];pipeline=ResearchEvidencePipeline(context["game"],context["cutoff"])
        for source in run["sources"]:
            url=source["url"];html,probe=parsed.get(url,(None,None))
            temporal=extract_temporal_metadata(url,provider={"published_date":source.get("published_at")},
                html=html,body=source["body"],title=source["title"],fetched_at=source["captured_at"])
            if probe:temporal["metadata_probe"]=probe
            fetched={"url":url,"content":source["body"],"title":source["title"],
                "provider":source["fetch_provider"],"captured_at":source["captured_at"],"temporal_metadata":temporal}
            pipeline.add_source(source,fetched)
            if source["content_chars"]>=100:by_url.setdefault(url,temporal)
        for old in run["decisions"]:
            proposed={k:old.get(k)for k in ("url","quote","claim_type","assertion_kind","team_id","game_reference")}
            pipeline.validate_claim(proposed)
        result={"accepted":len(pipeline.claims),"temporal_classes":Counter(r["temporal_class"]for r in pipeline.claims),
            "temporal_bases":Counter(r["temporal_basis"]for r in pipeline.claims),"summary":pipeline.summary(),
            "sources":list(pipeline.sources.values()),"claims":pipeline.claims,"decisions":pipeline.decisions}
        save(root/(name+"_qualification.json"),result)
        results[name]={k:v for k,v in result.items()if k not in {"sources","claims","decisions"}}
    counts={
        "readable_unique_urls":len(by_url),
        "published_at":sum(bool(t["published_at"])for t in by_url.values()),
        "updated_at":sum(bool(t["updated_at"])for t in by_url.values()),
        "date_only":sum(bool(t["published_at"] or t["updated_at"])
            and all(t.get(f+"_precision")!="timestamp"for f in ("published_at","updated_at"))for t in by_url.values()),
        "observed_current_snapshots":sum(bool(t["observed_at"])for t in by_url.values()),
        "no_publication_update_or_observation":sum(not any(t[k]for k in ("published_at","updated_at","observed_at"))for t in by_url.values()),
    }
    unchanged=all(Path(p).is_file()and digest(p)==h for p,h in inventory.items())
    assert unchanged,"frozen_research_changed"
    passed=results["operator"]["accepted"]>0 and any(results["operator"]["temporal_classes"].get(k,0)for k in ("BACKGROUND","CURRENT"))
    report={"status":"passed"if passed else"failed","root":str(root),"metadata_counts":counts,
        "qualification":results,"search_calls":0,"model_calls":0,"old_files_verified":len(inventory),
        "finished_at":now(),"seconds":time.perf_counter()-start}
    save(root/"result.json",report)
    brief={**report,"qualification":{name:{k:v for k,v in values.items()if k!="summary"}for name,values in results.items()}}
    print(json.dumps(brief,ensure_ascii=True,indent=2))
    return 0 if passed else 1

if __name__=="__main__":raise SystemExit(main())
