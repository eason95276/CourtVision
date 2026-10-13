"""Run from repository root: python -m v2.day4.run_analysis --help."""
import argparse
import json
from pathlib import Path

from .analysis import AnalysisError, SeriesAnalysis


def render(result):
    label = '完整系列赛球队汇总' if result['complete_series'] else '所选比赛球队汇总（非完整系列赛）'
    lines = [label, f"实际分析 {result['analyzed_games']} 场；整轮应有 {result['expected_series_games']} 场。"]
    if result['missing_game_ids']:
        lines.append('缺失或不完整：'+', '.join(result['missing_game_ids']))
    for t in result['teams']:
        s=t['shooting']['three_point']
        percentage = f"{s['value']:.2%}" if s['value'] is not None else '不可计算'
        lines.append(f"{t['team']}：{t['wins']}胜{t['losses']}负；得分 {t['totals']['points']}，失分 {t['totals']['opponent_points']}，净胜 {t['totals']['margin']}；场均得分 {t['per_team_game']['points']:.2f}（分母 {t['games']} 场）；三分 {s['numerator']}/{s['denominator']}，{percentage}。")
    if result.get('delta_second_minus_first') is not None:
        a,b=result['perspective_games']
        d=result['delta_second_minus_first']
        lines.append(f"固定球队 {result['perspective_team_id']}：{a['game_id']} {'主场' if a['is_home'] else '客场'} {a['points']}:{a['opponent_points']}；{b['game_id']} {'主场' if b['is_home'] else '客场'} {b['points']}:{b['opponent_points']}。后一场减前一场：得分 {d['points']:+d}，失分 {d['opponent_points']:+d}，净胜分 {d['margin']:+d}。")
    lines += ['球员逐场数据未取得，球员场均不可用；不会以未取得记录推断未出场。',
              '全部属于赛后描述，含加时；篮板和失误的球员合计与球队项分别保留。统计变化不证明因果。',
              '数值来源：原始 Games.csv、TeamStatistics.csv；逐场编号与来源见同名 JSON。']
    return '\n\n'.join(lines)+'\n'


def main():
    p=argparse.ArgumentParser(description='Offline postgame team comparison / series summary')
    p.add_argument('--compare', nargs=2, metavar=('FIRST_GAME','SECOND_GAME'))
    p.add_argument('--team-id', type=int, default=1610612747)
    p.add_argument('--games', nargs='+', help='Explicit subset; omission selects full verified series')
    p.add_argument('--output', type=Path, required=True, help='New JSON file; sibling Markdown also created')
    args=p.parse_args()
    if args.compare and args.games:
        p.error('--compare and --games are mutually exclusive')
    md=args.output.with_suffix('.md')
    if args.output.suffix != '.json' or args.output.exists() or md.exists():
        p.error('Supply a new .json path with no existing .md sibling')
    try:
        engine=SeriesAnalysis.load()
        result=engine.compare(*args.compare,args.team_id) if args.compare else engine.summarize(args.games)
    except (AnalysisError,OSError,KeyError,ValueError) as exc:
        p.exit(2, f'Analysis refused: {exc}\n')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    with md.open('x',encoding='utf-8') as f:
        f.write(render(result))
    print(render(result))


if __name__=='__main__':
    main()
