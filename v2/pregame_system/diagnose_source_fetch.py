"""Controlled transport smoke or one Heat Research-only acceptance; never publishes."""
import argparse
import json
import os
import time
from pathlib import Path
from collections import Counter

from .free_search_mcp_client import FreeSearchMCPTransport,error_diagnostic
from .source_body_fetch import SourceBodyFetcher
from .storage import new_run,save,now

TARGET="espn:401902644"

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-only",action="store_true")
    args=parser.parse_args(argv)
    from v2.frontend.server import load_local_env
    load_local_env()
    root=new_run("heat_research_only_acceptance" if args.research_only else "research_fetch_recovery")
    started=time.perf_counter()
    if not args.research_only:
        primary=FreeSearchMCPTransport(timeout_seconds=30)
        cases={}
        for name,call in [
            ("search",lambda:primary.search_many([{"query":"Miami Heat NBA official","max_results":2}])),
            ("single_fetch",lambda:primary.fetch_many(["https://example.com"])),
            ("batch_fetch",lambda:primary.fetch_many(["https://example.com","https://www.nba.com/news/2026-27-season-preview-mia"])),
        ]:
            at=time.perf_counter()
            try:rows=call();cases[name]={"responses":rows,"seconds":time.perf_counter()-at}
            except Exception as exc:cases[name]={"error":error_diagnostic(exc,operation=name),"seconds":time.perf_counter()-at}
            save(root/(name+".json"),cases[name])
            print(json.dumps({"case":name,"seconds":cases[name]["seconds"],
                "results":[{"url":r.get("url"),"body_characters":len(r.get("content") or ""),"search_results":len(r.get("results") or []),
                            "diagnostic":r.get("diagnostic"),"errors":r.get("errors")}for r in cases[name].get("responses",[])],
                "error":cases[name].get("error")},ensure_ascii=True),flush=True)
        passed=bool(cases.get("search",{}).get("responses")) and any(
            len(r.get("content") or "")>=100 for r in cases.get("single_fetch",{}).get("responses",[]))
        report={"status":"passed" if passed else "failed","mode":"smoke","root":str(root),"cases":cases,"seconds":time.perf_counter()-started}
    else:
        from v2.frontend.prediction_version_service import PredictionVersionService
        from v2.frontend.generation_input import merge_registered_games,prepare_generation_data
        from v2.frontend.analysis_service import SavedGameAnalysisService,game_identity,research_local_context,search_categories
        from .web_research_agent import AutonomousWebResearcher
        from .web_search import PregameWebSearch
        service=PredictionVersionService()
        cutoff=now()
        data=prepare_generation_data(merge_registered_games({"games":[]},service.tasks),TARGET,cutoff=cutoff)
        game=next(g for g in data["games"] if g["id"]==TARGET)
        SavedGameAnalysisService(data)._attach_player_knowledge(game,cutoff)
        search=PregameWebSearch(root=root/"search")
        researcher=AutonomousWebResearcher(searcher=search,root=root/"research")
        report={"target":TARGET,"root":str(root),"started_at":cutoff,"mode":"research_only"}
        try:
            context,trace=researcher.investigate(game_identity(game),cutoff,categories=search_categories(game),
                                               local_context=research_local_context(game))
            save(root/"research_result.json",{"context":context,"trace":trace,"input_frozen_at":now()})
            sources=list(researcher._evidence_pipeline.sources.values()) if hasattr(researcher,"_evidence_pipeline") else []
            bodies=[s for s in sources if s["fetch_status"]=="readable"]
            usages=trace.get("model_calls") or []
            discovery=json.loads(Path(context["discovery_receipt_path"]).read_text(encoding="utf-8"))
            attempts=[a for s in sources for a in s.get("fetch_attempts") or []]
            report.update(status="passed" if bodies and context.get("minimum_research",{}).get("sufficient") else "failed",
                query_count=len(discovery["queries"]),candidate_urls=len({r.get("url") for r in context.get("discovery_records",[])}),
                fetched_bodies=len(bodies),fetch_failures=len(sources)-len(bodies),
                fallback_usage=Counter(s["fetch_provider"] for s in bodies),provider_attempts=Counter(a["provider"] for a in attempts),
                extracted_claims=len(context.get("claim_decisions") or []),qualified_evidence=len(context.get("records") or []),
                temporal_classes=Counter(r["temporal_class"] for r in context.get("records") or []),
                temporal_bases=Counter(r.get("temporal_basis") or "unknown" for r in context.get("records") or []),
                temporal_metadata_counts={"published_at":sum(bool(s.get("published_at")) for s in sources),
                    "updated_at":sum(bool(s.get("updated_at")) for s in sources),
                    "observed_at":sum(bool(s.get("observed_at")) for s in sources),
                    "no_source_time":sum(not any(s.get(k)for k in ("published_at","updated_at","observed_at"))for s in sources)},
                unknown=[k for k,v in context.get("minimum_research",{}).get("coverage",{}).items() if v["status"]=="UNKNOWN"],
                model_calls=len(usages),tokens={k:sum(u.get("usage",{}).get(k,0) for u in usages) for k in ("input_tokens","output_tokens","total_tokens")},
                sources=[{"url":s["url"],"provider":s["fetch_provider"],"characters":s["content_chars"],
                          "published_at":s.get("published_at"),"updated_at":s.get("updated_at"),
                          "observed_at":s.get("observed_at"),"fetched_at":s.get("fetched_at"),
                          "temporal_metadata":s.get("temporal_metadata"),
                          "qualification_reason":s["qualification_reason"],"fetch_attempts":s["fetch_attempts"]}for s in sources])
        except Exception as exc:
            report.update(status="failed",error=error_diagnostic(exc,operation="research_only"))
        report.update(finished_at=now(),seconds=time.perf_counter()-started)
    save(root/"result.json",report)
    print(json.dumps({k:v for k,v in report.items() if k not in {"sources","cases"}},ensure_ascii=True,indent=2),flush=True)
    return 0 if report["status"]=="passed" else 1

if __name__=="__main__":raise SystemExit(main())
