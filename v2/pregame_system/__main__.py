"""CLI shared by report and conversational queries; no frontend changes."""
import argparse
import json
from pathlib import Path
from .agent import PregameAgent,Session,StructuredPlanner


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--game-id');parser.add_argument('--question')
    parser.add_argument('--intent',default='report',choices=['report','prediction','team_form','matchup','availability','market','provenance'])
    parser.add_argument('--cutoff');parser.add_argument('--historical-replay',action='store_true')
    parser.add_argument('--allow-lookup',action='store_true')
    parser.add_argument('--official-final',action='store_true',help='One final prediction per game, only during the last five pregame minutes')
    parser.add_argument('--analyst-judgment',action='store_true',help='Explicitly request evidence-bound structured AI analyst judgment')
    parser.add_argument('--analyst-ledger',help='Explicit analyst attempt ledger directory; default ledger has a bounded call budget')
    parser.add_argument('--session');parser.add_argument('--llm-ledger')
    args=parser.parse_args()
    if not args.question and not args.game_id:parser.error('Supply --game-id for deterministic report, or --question for structured LLM planning')
    session=Session.model_validate_json(Path(args.session).read_text(encoding='utf8')) if args.session else Session()
    analyst=None
    if args.analyst_judgment:
        from .analyst import EvidenceBoundAnalyst
        analyst=EvidenceBoundAnalyst(root=args.analyst_ledger) if args.analyst_ledger else EvidenceBoundAnalyst()
    agent=PregameAgent(planner=StructuredPlanner(args.llm_ledger),analyst=analyst)
    if args.official_final:
        if not args.game_id or args.historical_replay or args.cutoff or args.question or args.analyst_judgment:
            parser.error('--official-final requires --game-id and the actual clock; no replay, custom cutoff, question or AI judgment')
        from .official_final import OfficialFinalizer
        print(json.dumps(OfficialFinalizer(agent).finalize(args.game_id),ensure_ascii=False,indent=2));return
    plan={'intent':args.intent,'game_id':args.game_id} if args.game_id and not args.question else None
    report,_=agent.ask(args.question or args.intent,session,cutoff=args.cutoff,plan=plan,
        replay=args.historical_replay,allow_lookup=args.allow_lookup,
        generate_judgment=args.analyst_judgment)
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
