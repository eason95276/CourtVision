"""Manual CourtVision postgame, brief and global routing entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from v2.pregame_system.storage import clean, save

from .ai_review import generate_ai_brief, generate_ai_review
from .briefs import build_daily_brief, build_series_brief, build_series_brief_from_manifest, build_team_brief, build_topic_brief
from .pipeline import DEFAULT_IDENTITY_PATH, DEFAULT_OUTPUT_ROOT, load_records, run_postgame_pipeline
from .refresh import CachedRawClient, NBAApiClient
from .router import CourtVisionRouter


def _write_brief(value: dict, output_root: Path) -> Path:
    path = output_root / "briefs" / f"{value['brief_hash']}.json"
    if not path.exists():
        save(path, value)
    return path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    pipeline = commands.add_parser("pipeline", help="refresh one completed game and build its deterministic record")
    pipeline.add_argument("--nba-game-id", required=True)
    pipeline.add_argument("--game-date", required=True)
    pipeline.add_argument("--home-team-id", required=True, type=int)
    pipeline.add_argument("--away-team-id", required=True, type=int)
    pipeline.add_argument("--scheduled-tipoff")
    pipeline.add_argument("--espn-game-id")
    pipeline.add_argument("--official-prediction")
    pipeline.add_argument("--cached", action="store_true")
    pipeline.add_argument("--identity-path", default=str(DEFAULT_IDENTITY_PATH))
    pipeline.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))

    ai = commands.add_parser("ai-review", help="explicitly request an AI review for one deterministic record")
    ai.add_argument("--record", required=True)
    ai.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT / "ai_reviews"))
    ai.add_argument("--model", default="deepseek-v4-flash")

    ai_brief = commands.add_parser("ai-brief", help="explicitly request AI wording for one deterministic brief")
    ai_brief.add_argument("--brief", required=True)
    ai_brief.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT / "ai_briefs"))
    ai_brief.add_argument("--model", default="deepseek-v4-flash")

    brief = commands.add_parser("brief", help="build one deterministic bounded brief")
    brief.add_argument("--mode", required=True, choices=["daily", "team", "series", "topic"])
    brief.add_argument("--records-root", default=str(DEFAULT_OUTPUT_ROOT / "records"))
    brief.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    brief.add_argument("--date")
    brief.add_argument("--team-id", type=int)
    brief.add_argument("--start-date")
    brief.add_argument("--end-date")
    brief.add_argument("--recent-n", type=int)
    brief.add_argument("--team-ids", nargs=2, type=int)
    brief.add_argument("--game-ids", nargs="+")
    brief.add_argument("--manifest")
    brief.add_argument("--data-root")
    brief.add_argument(
        "--topic",
        choices=[
            "largest_prediction_error",
            "largest_team_change",
            "largest_model_market_disagreement",
        ],
    )

    route = commands.add_parser("route", help="route one global CourtVision question")
    route.add_argument("--question", required=True)
    route.add_argument("--records-root", default=str(DEFAULT_OUTPUT_ROOT / "records"))

    commands.add_parser("market-experiment", help="run the bounded historical market method comparison")
    return root


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "pipeline":
        client = CachedRawClient() if args.cached else NBAApiClient()
        result = run_postgame_pipeline(
            nba_game_id=args.nba_game_id,
            game_date=args.game_date,
            home_team_id=args.home_team_id,
            away_team_id=args.away_team_id,
            scheduled_tipoff=args.scheduled_tipoff,
            espn_game_id=args.espn_game_id,
            identity_path=args.identity_path,
            output_root=args.output_root,
            client=client,
            official_prediction=args.official_prediction,
        )
        print(
            json.dumps(
                {
                    "refresh_status": result.refresh.status,
                    "bundle_path": str(result.refresh.bundle_path),
                    "record_hash": result.postgame_record["record_hash"],
                    "record_output": result.postgame_record.get("output"),
                    "learning_record_hash": result.learning_record["learning_record_hash"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "ai-review":
        record = json.loads(Path(args.record).read_text(encoding="utf-8"))
        result = generate_ai_review(record, output_root=args.output_root, model=args.model)
        print(
            json.dumps(
                {
                    "status": result.status,
                    "output_path": str(result.output_path) if result.output_path else None,
                    "failure_path": str(result.failure_path) if result.failure_path else None,
                    "error_type": result.error_type,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.status == "success" else 2
    if args.command == "ai-brief":
        brief = json.loads(Path(args.brief).read_text(encoding="utf-8"))
        result = generate_ai_brief(brief, output_root=args.output_root, model=args.model)
        print(
            json.dumps(
                {
                    "status": result.status,
                    "output_path": str(result.output_path) if result.output_path else None,
                    "failure_path": str(result.failure_path) if result.failure_path else None,
                    "error_type": result.error_type,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.status == "success" else 2
    if args.command == "market-experiment":
        from v2.pregame_system.market_experiment import run_market_method_experiment

        print(run_market_method_experiment())
        return 0

    records = load_records(args.records_root)
    if args.command == "brief":
        if args.mode == "daily":
            value = build_daily_brief(args.date, records)
        elif args.mode == "team":
            if args.team_id is None:
                raise ValueError("team_id_required")
            value = build_team_brief(
                args.team_id,
                records,
                start_date=args.start_date,
                end_date=args.end_date,
                recent_n=args.recent_n,
            )
        elif args.mode == "series":
            if args.manifest:
                value = build_series_brief_from_manifest(
                    args.manifest,
                    selected_game_ids=args.game_ids,
                    data_root=args.data_root,
                )
            else:
                value = build_series_brief(
                    team_ids=args.team_ids or [],
                    ordered_game_ids=args.game_ids or [],
                    records=records,
                )
        else:
            value = build_topic_brief(args.topic, records)
        path = _write_brief(value, Path(args.output_root))
        print(json.dumps({"brief_hash": value["brief_hash"], "path": str(path)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "route":
        router = CourtVisionRouter(records=records)
        plan = router.plan(args.question)
        if plan.route == "pregame":
            from v2.pregame_system.agent import PregameAgent

            router.pregame_agent = PregameAgent()
        print(json.dumps(clean(router.ask(args.question, plan=plan)), ensure_ascii=False, indent=2, default=str))
        return 0
    raise ValueError("unknown_command")


if __name__ == "__main__":
    raise SystemExit(main())
