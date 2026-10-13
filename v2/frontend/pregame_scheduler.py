"""One-shot durable scheduler sharing the manual report generation coordinator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from v2.pregame_system.prediction_versions import DEFAULT_DATABASE, PredictionVersionStore, VersionStoreError, _clock_instant, _instant
from .export_data import DEFAULT_OUTPUT, read_json
from .generation_input import merge_registered_games
from .prediction_version_service import PredictionVersionService
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner


class PregameScheduler:
    def __init__(self, service: PredictionVersionService):
        self.service = service

    def run_due_tasks(self, data: dict[str, Any] | None = None, *, limit=20, plan_only=False):
        if data is None:
            data = read_json(DEFAULT_OUTPUT) if DEFAULT_OUTPUT.is_file() else {"games": []}
        registration_errors = []
        at = _clock_instant(self.service.store.clock)
        for game in data.get("games", []):
            try:
                if game.get("status") != "scheduled":
                    self.service.tasks.cancel_game(game)
                    self.service.store.select_current(game)
                    continue
                if _instant(game["tipoff"], "scheduled_tipoff") <= at:
                    self.service.store.select_current(game)
                    continue
                self.service.tasks.ensure_schedule(game)
            except VersionStoreError as exc:
                registration_errors.append({"game_id": game["id"], "error": str(exc)})
        runtime_data = merge_registered_games(data, self.service.tasks)
        for game in runtime_data["games"]:
            try:
                if self.service.tasks.registered_game(game["id"]):
                    SchedulerPolicyPlanner(self.service.tasks).plan_game(game)
            except VersionStoreError as exc:
                registration_errors.append({"game_id": game["id"], "error": str(exc)})
        due = self.service.tasks.list_due_tasks(limit=limit)
        results = []
        for task in due:
            if plan_only:
                results.append(self.service.tasks.summary(task))
                continue
            try:
                reply = self.service.execute_task(runtime_data, task["game_id"], task["task_id"])
                results.append({"task": reply["generation_task"], "update_status": reply["update_status"],
                                "version_id": reply.get("version_id")})
            except Exception as exc:
                results.append({"task": self.service.tasks.summary(self.service.tasks.get_task(task["task_id"])),
                                "update_status": "failed", "error_type": type(exc).__name__})
        return {"plan_only": plan_only, "due_count": len(due), "results": results,
                "registration_errors": registration_errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--once", action="store_true", help="Scan and execute due tasks once")
    parser.add_argument("--plan-only", action="store_true", help="Persist schedules and list due tasks without generation")
    parser.add_argument("--data", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=20)
    subparsers = parser.add_subparsers(dest="command")
    register = subparsers.add_parser("register-game")
    register.add_argument("--file", type=Path, required=True)
    configure = subparsers.add_parser("configure-policy")
    configure.add_argument("--game-id", required=True)
    configure.add_argument("--file", type=Path, required=True)
    update = subparsers.add_parser("material-update")
    update.add_argument("--file", type=Path, required=True)
    update.add_argument("--confirmed", action="store_true")
    revision = subparsers.add_parser("revise-schedule")
    revision.add_argument("--game-id", required=True)
    revision.add_argument("--file", type=Path, required=True)
    revision.add_argument("--evidence-file", type=Path, required=True)
    revision.add_argument("--expected-revision", type=int, required=True)
    revision.add_argument("--reason", required=True)
    revision.add_argument("--confirmed", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    from .server import load_local_env
    load_local_env()
    from .current_roster_preflight import prepare_current_roster_preflight
    service = PredictionVersionService(store=PredictionVersionStore(args.database),
        pre_generation_preparer=prepare_current_roster_preflight)
    if args.command == "register-game":
        registered = service.tasks.register_game(read_json(args.file))
        output = {"game": registered["game"], "tasks": [service.tasks.summary(task) for task in registered["tasks"]]}
    elif args.command == "configure-policy":
        output = SchedulerPolicyPlanner(service.tasks).configure(args.game_id, read_json(args.file))
    elif args.command == "material-update":
        output = SchedulerPolicyPlanner(service.tasks).material_update(read_json(args.file), admin_confirmed=args.confirmed)
    elif args.command == "revise-schedule":
        output = SchedulerPolicyPlanner(service.tasks).revise_schedule(args.game_id, read_json(args.file),
            expected_revision=args.expected_revision, reason=args.reason,
            evidence=read_json(args.evidence_file), admin_confirmed=args.confirmed)
    elif args.once:
        data = read_json(args.data) if args.data.is_file() else {"games": []}
        output = PregameScheduler(service).run_due_tasks(data, limit=args.limit, plan_only=args.plan_only)
    else:
        parser.error("Use register-game --file or --once")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
