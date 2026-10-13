"""Frozen-source hard-fact diagnostic; optional quality feedback, never a publication gate."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from .storage import BASE, new_run, save, digest
from .editorial_contract import build_editorial_projection
from .editorial_hard_facts import validate_publication_hard_facts
from .editorial_pipeline import review_quality

OLD_ROOT = BASE / "outputs/heat_raptors_real_acceptance"
GOLD = BASE / "fixtures/editorial_semantic_gold.json"


def audit_old(root):
    inventory={};invalid=[]
    for folder in sorted(OLD_ROOT.iterdir()):
        receipt=folder/"run_receipt.json"
        if not receipt.exists():continue
        r=json.loads(receipt.read_text(encoding="utf-8"))
        if r.get("run_attempt",1)>4:continue
        for file in sorted(folder.rglob("*")):
            if not file.is_file():continue
            entry={"sha256":digest(file),"bytes":file.stat().st_size}
            if file.suffix==".json":
                try:
                    content=json.loads(file.read_text(encoding="utf-8"))
                    entry["json_parsed"]=True
                    entry["top_keys"]=list(content) if isinstance(content,dict) else None
                except Exception as exc:
                    entry["json_parsed"]=False;invalid.append({"path":str(file),"error":type(exc).__name__})
            inventory[str(file.resolve())]=entry
    save(root/"old_artifact_inventory.json",{"files":inventory,"invalid_json":invalid})
    return inventory


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-review", "--real-review", dest="quality_review", action="store_true",
                        help="Optional configured content feedback only; never determines publish eligibility")
    parser.add_argument("--scope",choices=("frozen","gold","all"),default="all")
    args=parser.parse_args(argv)
    if args.quality_review:
        from v2.frontend.server import load_local_env
        load_local_env()
    root=new_run("editorial_grounding_replay")
    inventory=audit_old(root)
    results={"root":str(root),"quality_review_requested":args.quality_review,"research_calls":0,"forecasting_calls":0,
             "frozen":[],"gold":[],"model_calls":0}
    try:
        if args.scope in {"frozen","all"}:
            for name in ("20261002T170352_76ca0bb0c7","20261002T183747_2b8de7d8cb"):
                folder=OLD_ROOT/name
                for file in sorted(folder.glob("analyst/*/parsed_unvalidated.json")):
                    context=json.loads((file.parent/"context.json").read_text(encoding="utf-8"))
                    judgment=json.loads(file.read_text(encoding="utf-8"))
                    # Normalize escaped newlines exactly as the legacy renderer did.
                    judgment["analysis_article"]=judgment["analysis_article"].replace("\\n","\n")
                    projection=build_editorial_projection(context,judgment["final_prediction"])
                    text={k:judgment.get(k) for k in ("subjective_rationale","analysis_article",
                        "pregame_story","watch_themes") if judgment.get(k)}
                    blockers=validate_publication_hard_facts(judgment,projection)
                    case=new_run("editorial_grounding_frozen_cases")
                    save(case/"input.json",{"source":str(file),"projection":projection,"text":text,
                        "hard_fact_findings":blockers})
                    result={"source":str(file),"case_path":str(case),"projection_characters":len(json.dumps(projection,ensure_ascii=False)),
                        "hard_fact_findings":blockers,"publication_status":"blocked" if blockers else "eligible"}
                    if args.quality_review:
                        try:
                            feedback,meta=review_quality(projection,text,case,client=None)
                            results["model_calls"]+=1
                            result["quality_feedback"]=feedback
                            result["quality_review_metadata"]={k:v for k,v in meta.items() if k!="raw"}
                        except Exception as exc:
                            result["quality_feedback"]={"status":"unavailable","warning":type(exc).__name__}
                    results["frozen"].append(result)
        if args.scope in {"gold","all"}:
            corpus=json.loads(GOLD.read_text(encoding="utf-8"))
            workspace=BASE.parents[1]
            context=json.loads((workspace/corpus["base_context"]["path"]).read_text(encoding="utf-8"))
            locked=corpus["base_context"]["locked_prediction"]
            # Gold labels are diagnostic expectations, never sent to a model.
            cases=corpus["cases"]
            projection=build_editorial_projection(context,locked)
            for item in cases:
                judgment={"analysis_article":item["text"]}
                blockers=validate_publication_hard_facts(judgment,projection)
                actual="reject" if blockers else "allow"
                results["gold"].append({"id":item["id"],"expected":item["expected"],"actual":actual,
                    "correct":actual==item["expected"],"text":item["text"],"hard_fact_findings":blockers})
            case=root/"gold";case.mkdir()
            save(case/"result.json",results["gold"])
            for index,item in enumerate(corpus.get("synthetic_context_delta_cases") or []):
                changed=copy.deepcopy(context)
                changed["objective_evidence"].extend(item["synthetic_context_delta"]["items"])
                projection=build_editorial_projection(changed,locked)
                unit={"id":"unit.synthetic","text":item["text"],"aliases":[]}
                case=root/f"synthetic_{index}";case.mkdir()
                save(case/"input.json",{"fixture_only":True,"projection":projection,"units":[unit]})
                blockers=validate_publication_hard_facts({"analysis_article":item["text"]},projection)
                actual="reject" if blockers else "allow"
                results["gold"].append({"id":item["id"],"fixture_only":True,"expected":item["expected"],"actual":actual,
                    "correct":actual==item["expected"],"text":item["text"],"hard_fact_findings":blockers})
        results["status"]="complete"
    except Exception as exc:
        results.update(status="failed",error_type=type(exc).__name__,error=str(exc)[:2000])
    finally:
        assert all(Path(p).is_file() and digest(p)==e["sha256"] for p,e in inventory.items()),"old_artifact_changed"
        results["old_files_verified"]=len(inventory)
        results["gold_correct"]=sum(c["correct"] for c in results["gold"])
        results["gold_count"]=len(results["gold"])
        save(root/"result.json",results)
        print(json.dumps({k:v for k,v in results.items() if k not in {"frozen","gold","gold_metadata"}},ensure_ascii=True,indent=2))
    return 0 if results["status"]=="complete" else 1


if __name__=="__main__":raise SystemExit(main())
