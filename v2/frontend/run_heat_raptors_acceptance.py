"""One authorized real EARLY task, never a scheduler batch or automatic retry."""
from __future__ import annotations

import copy
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime,timezone

from v2.pregame_system.analyst import EvidenceBoundAnalyst
from v2.pregame_system.storage import new_run, save, now, content_hash, digest
from v2.pregame_system.web_research_agent import AutonomousWebResearcher
from v2.pregame_system.web_search import PregameWebSearch
from .analysis_service import SavedGameAnalysisService, prediction_layers
from .generation_input import merge_registered_games, prepare_generation_data
from .prediction_version_service import PredictionVersionService
from .server import load_local_env

TARGET = "espn:401902644"
OTHERS = ("espn:401914127", "espn:401918010")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-prior-attempts", type=int, choices=(0, 1, 2, 3, 4), default=0,
                        help="Explicit authorization guard; never executes an automatic task retry")
    parser.add_argument("--operator-authorized-preseason-acceptance", action="store_true",
                        help="One user-authorized fifth attempt; preserves all previous counts and receipts")
    parser.add_argument("--operator-after-research", type=Path,
                        help="Successful controlled Research-only receipt; authorizes one independent post-fix acceptance")
    parser.add_argument("--operator-after-roster", type=Path,
                        help="Verified current roster smoke; authorizes one fresh independent acceptance")
    args = parser.parse_args(argv)
    if args.operator_after_roster and args.operator_after_research:
        parser.error("choose one authorization basis")
    operator_basis = args.operator_after_roster or args.operator_after_research
    basis_kind = "current_roster_smoke" if args.operator_after_roster else "research_only"
    if args.expected_prior_attempts == 4 and not args.operator_authorized_preseason_acceptance and not operator_basis:
        parser.error("fifth attempt requires explicit preseason acceptance authorization")
    load_local_env()
    root = new_run("heat_raptors_operator_acceptance" if operator_basis else "heat_raptors_real_acceptance")
    started, clock = now(), time.perf_counter()
    report = {"status": "STARTED", "started_at": started, "target": TARGET, "root": str(root),
              "automatic_task_retries": 0, "cost": "unknown",
              **({"run_type":"operator_authorized_post_fix_acceptance", "authorization_basis":basis_kind} if operator_basis
                 else {"run_attempt": args.expected_prior_attempts + 1})}
    stage = {"current": "preflight"}
    metrics = {}
    researcher = None
    prior_receipts = {}
    previous_root=root.parent.parent/"heat_raptors_real_acceptance" if operator_basis else root.parent
    prior_limit=5 if operator_basis else args.expected_prior_attempts
    for previous in previous_root.iterdir():
        receipt = previous / "run_receipt.json"
        if previous != root and receipt.exists() and json.loads(receipt.read_text(encoding="utf-8")).get("run_attempt", 1) <= prior_limit:
            prior_receipts.update({str(p.resolve()): digest(p) for p in previous.rglob("*") if p.is_file()})
    if operator_basis:
        for previous in root.parent.iterdir():
            if previous!=root and (previous/"run_receipt.json").exists():
                prior_receipts.update({str(p.resolve()):digest(p) for p in previous.rglob("*") if p.is_file()})
    save(root / "prior_attempt_integrity_before.json", prior_receipts)

    class Researcher(AutonomousWebResearcher):
        def investigate(self, *args, **kwargs):
            stage["current"] = "Research"
            begin = time.perf_counter()
            try:
                context, trace = super().investigate(*args, **kwargs)
                save(root / "research_result.json", {"context": context, "trace": trace})
                return context, trace
            finally:
                metrics["research_seconds"] = time.perf_counter() - begin

    class StrictAnalysis(SavedGameAnalysisService):
        def _attach_player_knowledge(self, game, cutoff):
            stage["current"] = "Player Knowledge"
            before = content_hash(prediction_layers(game))
            super()._attach_player_knowledge(game, cutoff)
            knowledge = game["roster_context"]["player_knowledge"]
            save(root / "player_knowledge.json", knowledge)
            assert before == content_hash(prediction_layers(game)), "player_knowledge_changed_numeric_layer"
            if not knowledge.get("teams") or knowledge.get("status") == "unavailable":
                raise ValueError("player_knowledge_unavailable")
        def analyze(self, *args, before_writer=None, **kwargs):
            writer_clock = None
            def checked_writer():
                nonlocal writer_clock
                stage["current"] = "Forecasting/Editorial/validators"
                writer_clock = time.perf_counter()
                if before_writer: before_writer()
            try:
                result = super().analyze(*args, before_writer=checked_writer, **kwargs)
            finally:
                if writer_clock is not None: metrics["forecasting_editorial_seconds"] = time.perf_counter()-writer_clock
            save(root / "complete_analysis.json", result)
            stage["current"] = "publish"
            return result

    def prepare(data, game_id, *, cutoff):
        stage["current"] = "Production baseline"
        begin = time.perf_counter()
        result = prepare_generation_data(data, game_id, cutoff=cutoff)
        game = next(g for g in result["games"] if g["id"] == TARGET)
        if args.operator_after_roster:
            teams = game["roster_context"]["teams"]
            for side in ("home", "away"):
                players = teams[side].get("official_team_roster", {}).get("players", [])
                if not players:
                    raise ValueError("acceptance_current_roster_missing:" + side)
            if not any(p["name"] == "Immanuel Quickley" for p in teams["home"]["official_team_roster"]["players"]):
                raise ValueError("acceptance_quickley_current_identity_missing")
            save(root / "current_roster_input.json", game["roster_context"])
        save(root / "production_input.json", game)
        metrics["baseline_seconds"] = time.perf_counter() - begin
        return result

    try:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise ValueError("deepseek_credentials_missing")
        search = PregameWebSearch(root=root / "search")
        researcher = Researcher(searcher=search, root=root / "research")
        def factory(data, *, analyst):
            return StrictAnalysis(data, analyst=analyst, searcher=search, researcher=researcher, require_research_success=True)
        service = PredictionVersionService(service_factory=factory, input_preparer=prepare,
            analyst_factory=lambda request: EvidenceBoundAnalyst(root=root / "analyst", max_calls=6, include_forecasting_skill=True))
        with service.store.connect() as db:
            before_versions = [dict(row) for row in db.execute("SELECT * FROM prediction_versions")]
            other_tasks = [dict(row) for row in db.execute("SELECT * FROM generation_tasks WHERE game_id IN (?,?) ORDER BY task_id", OTHERS)]
            target_task_history = [dict(row) for row in db.execute("SELECT * FROM generation_tasks WHERE game_id=?", (TARGET,))]
            target_tasks = [dict(row) for row in db.execute("SELECT * FROM generation_tasks WHERE game_id=? AND policy_version='scheduler-v2' AND stage='EARLY_PREVIEW' AND trigger='scheduled'", (TARGET,))]
            if db.execute("SELECT COUNT(*) FROM prediction_versions WHERE game_id=?", (TARGET,)).fetchone()[0] != 0:
                raise ValueError("target_already_has_version_stop")
            required_status = "failed" if operator_basis else "pending" if args.expected_prior_attempts == 0 else "failed"
            expected_attempts=5 if operator_basis else args.expected_prior_attempts
            if len(target_tasks) != 1 or target_tasks[0]["status"] != required_status or target_tasks[0]["attempt_count"] != expected_attempts:
                raise ValueError("target_not_authorized_attempt_state")
        save(root / "before_receipt.json", {"versions": before_versions, "other_tasks": other_tasks,
            "target_task": target_tasks[0],"target_task_history":target_task_history})
        backup = sqlite3.connect(root / "before.sqlite")
        with service.store.connect() as source: source.backup(backup)
        backup.close()
        data = merge_registered_games({"games": []}, service.tasks)
        target = next(g for g in data["games"] if g["id"] == TARGET)
        if target["nba_game_id"] != "0012600009" or target["game_type"] != "preseason":
            raise ValueError("target_identity_or_type_invalid")
        if json.loads(target_tasks[0]["metadata_json"])["preview_kind"] != "special_preheat":
            raise ValueError("target_not_special_preheat")
        execution_task=target_tasks[0]
        if operator_basis:
            evidence=json.loads(operator_basis.read_text(encoding="utf-8"))
            if evidence.get("status")!="passed" or evidence.get("target")!=TARGET:
                raise ValueError("operator_basis_not_passed")
            if args.operator_after_roster:
                if evidence.get("mode") != "current_roster_smoke" or not evidence.get("current_identity_verified"):
                    raise ValueError("operator_requires_verified_current_roster")
                if set(evidence.get("teams", {})) != {"1610612748", "1610612761"} or not all(t.get("count",0)>0 for t in evidence["teams"].values()):
                    raise ValueError("operator_current_roster_team_scope_invalid")
                if evidence.get("quickley",{}).get("team_id") != 1610612761:
                    raise ValueError("operator_quickley_team_scope_invalid")
            elif not evidence.get("fetched_bodies") or not evidence.get("qualified_evidence"):
                raise ValueError("operator_requires_successful_research_only")
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(evidence["finished_at"].replace("Z","+00:00"))).total_seconds()
            if age<0 or age>1800:raise ValueError("operator_research_acceptance_stale")
            from v2.pregame_system.prediction_versions import _clock_instant,_utc
            logical_key="operator-acceptance:"+digest(operator_basis)
            with service.tasks.transaction() as db:
                if db.execute("SELECT 1 FROM generation_tasks WHERE game_id=? AND logical_key=?",(TARGET,logical_key)).fetchone():
                    raise ValueError("operator_acceptance_already_used")
                at=_clock_instant(service.clock)
                meta=json.loads(target_tasks[0]["metadata_json"])
                meta["operator_acceptance"]={"authorized_at":_utc(at),"run_id":root.name,
                    "reason":"user-authorized current roster recovery acceptance" if args.operator_after_roster else "user-authorized engineering recovery acceptance",
                    "basis_kind":basis_kind,"basis_receipt":str(operator_basis.resolve()),
                    "basis_receipt_sha256":digest(operator_basis),"automatic_retries":0}
                execution_task=service.tasks._insert(db,TARGET,"EARLY_PREVIEW","manual_refresh",_utc(at),
                    target_tasks[0]["deadline_at"],at,logical_key=logical_key,metadata=meta,
                    created_reason="operator-authorized post-fix acceptance")
                db.execute("UPDATE generation_tasks SET max_attempts=1 WHERE task_id=?",(execution_task["task_id"],))
            save(root/"operator_authorization.json",{"run_id":root.name,"task_id":execution_task["task_id"],
                "preserved_exhausted_task":target_tasks[0],"basis_kind":basis_kind,"basis_receipt":str(operator_basis.resolve()),
                "basis_receipt_sha256":digest(operator_basis),"max_attempts":1})
        elif args.expected_prior_attempts in (3, 4) and target_tasks[0]["max_attempts"] == args.expected_prior_attempts:
            # One explicit acceptance authorization extends this task only.
            # Count is never reset; the next attempt remains unauthorized.
            prior = args.expected_prior_attempts
            save(root / f"explicit_attempt{prior+1}_authorization.json", {"task_id": target_tasks[0]["task_id"],
                "old_max_attempts": prior, "new_max_attempts": prior+1, "expected_attempt_count": prior,
                "scope": "one operator-authorized preseason acceptance Heat EARLY execution; no automatic retry"})
            with service.store.connect() as db:
                changed = db.execute("""UPDATE generation_tasks SET max_attempts=?
                    WHERE task_id=? AND game_id=? AND status='failed' AND attempt_count=? AND max_attempts=?""",
                    (prior+1, target_tasks[0]["task_id"], TARGET, prior, prior)).rowcount
                if changed != 1: raise ValueError("operator_authorization_state_changed")
        print("Running one real Heat @ Raptors EARLY_PREVIEW; no other tasks will be executed.", flush=True)
        result = service.execute_task(data, TARGET, execution_task["task_id"])
        if result.get("update_status") != "published":
            raise ValueError("target_not_published:" + str(result.get("update_status")))
        save(root / "published_selection.json", result)
        report.update(status="SUCCESS", version_id=result["version_id"], task_id=execution_task["task_id"])
    except Exception as exc:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        detail = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        report.update(status="FAILED", failed_stage=stage["current"], error_type=type(exc).__name__, error=detail[:1600])
    finally:
        assert all(Path(p).is_file() and digest(p) == h for p, h in prior_receipts.items()), "prior_attempt_receipt_changed"
        report["prior_attempt_files_unchanged"] = len(prior_receipts)
        if "service" in locals() and "other_tasks" in locals():
            with service.store.connect() as db:
                after_others = [dict(row) for row in db.execute("SELECT * FROM generation_tasks WHERE game_id IN (?,?) ORDER BY task_id", OTHERS)]
                assert other_tasks == after_others, "other_matchup_tasks_changed"
                existing = {row["version_id"]: dict(row) for row in db.execute("SELECT * FROM prediction_versions")}
                assert all(existing.get(v["version_id"]) == v for v in before_versions), "old_immutable_version_changed"
                if operator_basis:
                    preserved=dict(db.execute("SELECT * FROM generation_tasks WHERE task_id=?",(target_tasks[0]["task_id"],)).fetchone())
                    assert preserved==target_tasks[0],"exhausted_task_changed"
                    for old_task in target_task_history:
                        row=dict(db.execute("SELECT * FROM generation_tasks WHERE task_id=?",(old_task["task_id"],)).fetchone())
                        assert row==old_task,"previous_task_history_changed"
            report["other_games_unchanged"] = True
            report["old_versions_unchanged"] = True
        report.update(finished_at=now(), total_seconds=time.perf_counter()-clock, metrics=metrics)
        save(root / "run_receipt.json", report)
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    return 0 if report["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
