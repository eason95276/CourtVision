"""Build a fact-only postgame package for a CourtVision V2 database game."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


V2_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
SQL_PATH = V2_DIR / "sql" / "02_single_game_facts.sql"
OUTPUTS_DIR = V2_DIR / "outputs"
VALIDATION_PATH = V2_DIR / "data" / "normalized" / "validation_results.json"

REQUIRED_QUERIES = {
    "game",
    "score_by_period",
    "score_by_half",
    "flow_summary",
    "maximum_leads",
    "scoring_runs",
    "maximum_run_events",
    "four_factors",
    "players",
    "player_leaders",
    "shot_zones",
    "shot_zone_share_top3",
    "shot_zone_fg_pct_largest",
    "primary_scoring_zones",
}


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a postgame fact package for a game already in the CourtVision V2 database."
    )
    parser.add_argument(
        "--game-id",
        required=True,
        help="Game ID that already exists in the games table.",
    )
    return parser.parse_args(argv)


def load_named_queries(path):
    sql_text = path.read_text(encoding="utf-8")
    markers = list(
        re.finditer(r"(?m)^-- query: ([a-z0-9_]+)\s*$", sql_text)
    )
    queries = {}
    for index, marker in enumerate(markers):
        section_end = markers[index + 1].start() if index + 1 < len(markers) else len(sql_text)
        name = marker.group(1)
        query = sql_text[marker.end():section_end].strip()
        if name in queries:
            raise ValueError(f"Duplicate SQL query name: {name}")
        queries[name] = query

    missing = REQUIRED_QUERIES - set(queries)
    if missing:
        raise ValueError(f"Missing SQL queries: {sorted(missing)}")
    return queries


def query_rows(connection, sql, game_id):
    cursor = connection.execute(sql, {"game_id": game_id})
    return [dict(row) for row in cursor.fetchall()]


def metric(raw, display):
    return {"raw": raw, "display": display}


def reshape_game(row):
    return {
        "game_id": row["game_id"],
        "date": row["game_date"],
        "season": row["season"],
        "season_type": row["season_type"],
        "away": {
            "team_id": row["away_team_id"],
            "tricode": row["away_team_tricode"],
            "name": row["away_team_name"],
            "points": row["away_points"],
        },
        "home": {
            "team_id": row["home_team_id"],
            "tricode": row["home_team_tricode"],
            "name": row["home_team_name"],
            "points": row["home_points"],
        },
        "winner_tricode": row["winner_tricode"],
        "final_margin": row["final_margin"],
        "actual_total": row["actual_total"],
    }


def reshape_four_factors(rows):
    output = {}
    for row in rows:
        output[row["team_tricode"]] = {
            "side": row["side"],
            "team_id": row["team_id"],
            "source_boxscore": {
                "fgm": row["fgm"],
                "fga": row["fga"],
                "fg3m": row["fg3m"],
                "fta": row["fta"],
                "tov": row["tov"],
                "orb": row["orb"],
                "opponent_drb": row["opponent_drb"],
            },
            "effective_field_goal_percentage": metric(
                row["efg_raw"], row["efg_display"]
            ),
            "turnover_rate": metric(
                row["tov_rate_raw"], row["tov_rate_display"]
            ),
            "offensive_rebound_rate": metric(
                row["orb_rate_raw"], row["orb_rate_display"]
            ),
            "free_throw_rate": metric(row["ftr_raw"], row["ftr_display"]),
        }
    return output


def reshape_players(rows, leader_rows):
    teams = defaultdict(list)
    for row in rows:
        player = {
            "person_id": row["person_id"],
            "name": row["full_name"],
            "starter": bool(row["starter"]),
            "minutes": row["minutes"],
            "points": row["points"],
            "rebounds": row["rebounds"],
            "assists": row["assists"],
            "steals": row["steals"],
            "blocks": row["blocks"],
            "turnovers": row["turnovers"],
            "field_goals": {"made": row["fgm"], "attempted": row["fga"]},
            "field_goal_percentage": metric(
                row["fg_pct_raw"], row["fg_pct_display"]
            ),
            "three_pointers": {"made": row["fg3m"], "attempted": row["fg3a"]},
            "three_point_percentage": metric(
                row["fg3_pct_raw"], row["fg3_pct_display"]
            ),
            "free_throws": {"made": row["ftm"], "attempted": row["fta"]},
            "free_throw_percentage": metric(
                row["ft_pct_raw"], row["ft_pct_display"]
            ),
            "plus_minus": row["plus_minus"],
            "true_shooting_percentage": metric(
                row["ts_pct_raw"], row["ts_pct_display"]
            ),
        }
        teams[row["team_tricode"]].append(player)

    leaders = defaultdict(lambda: defaultdict(list))
    for row in leader_rows:
        value = row["value"]
        if row["metric"] != "plus_minus" and value is not None and value.is_integer():
            value = int(value)
        leaders[row["team_tricode"]][row["metric"]].append(
            {
                "person_id": row["person_id"],
                "name": row["full_name"],
                "value": value,
            }
        )
    return {
        "appearance_definition": "player_boxscores.minutes IS NOT NULL; a recorded 0:00 appearance is retained, while NULL-minute DNP rows are excluded",
        "teams": dict(teams),
        "leaders": {team: dict(metrics) for team, metrics in leaders.items()},
    }


def reshape_shot_zones(rows, top_share_rows, largest_fg_rows, primary_rows, game):
    by_zone = defaultdict(dict)
    for row in rows:
        by_zone[row["zone"]][row["team_tricode"]] = {
            "attempts": row["attempts"],
            "team_fga": row["team_fga"],
            "attempt_share": metric(
                row["attempt_share_raw"], row["attempt_share_display"]
            ),
            "makes": row["makes"],
            "field_goal_percentage": metric(
                row["fg_pct_raw"], row["fg_pct_display"]
            ),
            "points": row["points"],
        }

    away = game["away"]["tricode"]
    home = game["home"]["tricode"]
    zones = []
    for zone in sorted(by_zone):
        away_data = by_zone[zone][away]
        home_data = by_zone[zone][home]
        away_share = away_data["attempt_share"]["raw"]
        home_share = home_data["attempt_share"]["raw"]
        away_pct = away_data["field_goal_percentage"]["raw"]
        home_pct = home_data["field_goal_percentage"]["raw"]
        zones.append(
            {
                "zone": zone,
                "teams": {away: away_data, home: home_data},
                "attempt_share_difference": {
                    "direction": f"{away}_minus_{home}",
                    "raw": None if away_share is None or home_share is None else away_share - home_share,
                    "absolute_raw": None if away_share is None or home_share is None else abs(away_share - home_share),
                },
                "field_goal_percentage_difference": {
                    "direction": f"{away}_minus_{home}",
                    "raw": None if away_pct is None or home_pct is None else away_pct - home_pct,
                    "absolute_raw": None if away_pct is None or home_pct is None else abs(away_pct - home_pct),
                },
            }
        )

    primary = defaultdict(list)
    for row in primary_rows:
        primary[row["team_tricode"]].append(
            {
                "zone": row["zone"],
                "attempts": row["attempts"],
                "makes": row["makes"],
                "points": row["points"],
            }
        )

    return {
        "grouping_field": "shots.shot_zone_basic",
        "supporting_fields": [
            "shots.shot_zone_area",
            "shots.shot_zone_range",
            "shots.shot_type",
            "shots.shot_made_flag",
        ],
        "zones": zones,
        "largest_attempt_share_differences": top_share_rows,
        "largest_field_goal_percentage_difference": (
            largest_fg_rows[0] if largest_fg_rows else None
        ),
        "primary_scoring_zones": dict(primary),
    }


def make_fact_package(results, database_hash_before, database_hash_after):
    if len(results["game"]) != 1:
        raise ValueError(f"Expected one game row, found {len(results['game'])}")
    game = reshape_game(results["game"][0])

    maximum_run_events = defaultdict(list)
    for row in results["maximum_run_events"]:
        maximum_run_events[row["run_id"]].append(row)

    key_runs = []
    for row in results["scoring_runs"]:
        if row["run_rank"] != 1:
            continue
        run = dict(row)
        run["events"] = maximum_run_events[row["run_id"]]
        key_runs.append(run)

    fact_package = {
        "schema_version": "1.0",
        "game": game,
        "score_by_period": results["score_by_period"],
        "game_flow": {
            "halves": results["score_by_half"],
            "maximum_leads": results["maximum_leads"],
            "lead_changes": results["flow_summary"][0]["lead_changes"],
            "ties": results["flow_summary"][0]["ties"],
            "scoring_event_count": results["flow_summary"][0]["scoring_event_count"],
            "definitions": {
                "scoring_event": "A play_by_play row with non-NULL scores where exactly one team gains 1 to 3 points versus the previous non-NULL score snapshot.",
                "lead_change": "After removing tied states, the leader after a scoring event differs from the leader after the previous non-tied scoring event. The opening 0-0 is excluded.",
                "tie": "A score-changing scoring event leaves score_away equal to score_home. The opening 0-0 is excluded.",
                "scoring_run": "A maximal sequence of consecutive score-changing events credited to one team, ending when the opponent scores. A run may cross a period boundary.",
            },
        },
        "four_factors": reshape_four_factors(results["four_factors"]),
        "player_facts": reshape_players(
            results["players"], results["player_leaders"]
        ),
        "shot_zone_facts": reshape_shot_zones(
            results["shot_zones"],
            results["shot_zone_share_top3"],
            results["shot_zone_fg_pct_largest"],
            results["primary_scoring_zones"],
            game,
        ),
        "key_runs": key_runs,
        "evidence": {
            "database": {
                "path": "v2/db/courtvision_v2.sqlite",
                "access_mode": "SQLite URI mode=ro with PRAGMA query_only=ON",
                "sha256_before": database_hash_before,
                "sha256_after": database_hash_after,
                "unchanged": database_hash_before == database_hash_after,
            },
            "sql_file": "v2/sql/02_single_game_facts.sql",
            "validation_file": "v2/data/normalized/validation_results.json",
            "metric_lineage": {
                "game_result": ["games", "team_boxscores"],
                "score_and_flow": [
                    "play_by_play.action_id",
                    "play_by_play.period",
                    "play_by_play.clock",
                    "play_by_play.score_away",
                    "play_by_play.score_home",
                ],
                "four_factors": [
                    "team_boxscores.field_goals_made",
                    "team_boxscores.field_goals_attempted",
                    "team_boxscores.three_pointers_made",
                    "team_boxscores.free_throws_attempted",
                    "team_boxscores.turnovers",
                    "team_boxscores.rebounds_offensive",
                    "team_boxscores.rebounds_defensive",
                ],
                "players": ["player_boxscores"],
                "shot_zones": [
                    "shots.shot_zone_basic",
                    "shots.shot_type",
                    "shots.shot_made_flag",
                ],
            },
            "key_run_events": results["maximum_run_events"],
        },
        "limitations": [
            "The database does not provide reliable period-opening five-player lineups after the first period, so full-game five-player lineup reconstruction cannot be completed reliably.",
            "These structured game facts can establish score, event sequence, box-score rates, and shot-zone distributions, but they cannot independently prove tactical causality.",
            "The local database contains three sampled regular-season games, so this single-game layer does not establish season-long tendencies or player ability.",
            "Single-game plus/minus is reported as an observed game fact and does not measure long-term player ability.",
            "Shot-zone field-goal percentage gaps include attempt counts because small samples can produce unstable percentages.",
            "No video, tracking data, defender-distance data, play-call labels, injury context, or reliable full-game lineup stints were used.",
        ],
    }
    return fact_package


def md_value(value):
    return "空值" if value is None else str(value)


def escape_cell(value):
    return md_value(value).replace("|", "\\|").replace("\n", " ")


def format_score(away, home, game):
    return (
        f"{game['away']['tricode']} {away}，"
        f"{game['home']['tricode']} {home}"
    )


def leader_text(leaders):
    return "；".join(f"{row['name']} {row['value']:g}" for row in leaders)


def game_result_sentence(game):
    away = game["away"]
    home = game["home"]
    if game["winner_tricode"] == away["tricode"]:
        outcome = "战胜"
    elif game["winner_tricode"] == home["tricode"]:
        outcome = "负于"
    else:
        outcome = "战平"
    return (
        f"{away['name']}客场 {away['points']} 比 {home['points']} "
        f"{outcome}{home['name']}。"
    )


def build_markdown(facts):
    game = facts["game"]
    away = game["away"]["tricode"]
    home = game["home"]["tricode"]
    factors = facts["four_factors"]
    run = facts["key_runs"][0]
    flow = facts["game_flow"]
    zone_facts = facts["shot_zone_facts"]
    lines = [
        "# CourtVision V2 单场赛后数据事实层",
        "",
        f"比赛：`{game['game_id']}`，{game['date']}，{game['season']} {game['season_type']}。",
        "",
        "本报告由只读 SQLite 查询生成。所有结论限于这场比赛的结构化数据，不使用比赛录像，也不输出战术因果或预测。",
        "",
        "## 1. 比赛结果",
        "",
        f"{game_result_sentence(game)}最终分差 {game['final_margin']} 分，实际总分 {game['actual_total']} 分。",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 比赛 ID | `{game['game_id']}` |",
        f"| 日期 | {game['date']} |",
        f"| 赛季 | {game['season']} {game['season_type']} |",
        f"| 客队 | {game['away']['name']}（{away}） |",
        f"| 主队 | {game['home']['name']}（{home}） |",
        f"| 最终比分 | {away} {game['away']['points']}，{home} {game['home']['points']} |",
        f"| 最终分差 | {game['final_margin']} |",
        f"| 实际总分 | {game['actual_total']} |",
        "",
        "## 2. 每节与半场走势",
        "",
        f"净胜分按 `{home} 得分减 {away} 得分` 计算，正数表示主队该节得分更多。",
        "",
        f"| 节次 | {away} | {home} | {home} 相对 {away} 净胜分 |",
        "|---:|---:|---:|---:|",
    ]
    for row in facts["score_by_period"]:
        lines.append(
            f"| 第 {row['period']} 节 | {row['away_points']} | {row['home_points']} | {row['home_minus_away']:+d} |"
        )
    lines.extend(
        [
            "",
            f"| 半场 | {away} | {home} | {home} 相对 {away} 净胜分 |",
            "|---:|---:|---:|---:|",
        ]
    )
    for row in flow["halves"]:
        label = "上半场" if row["half"] == 1 else "下半场"
        lines.append(
            f"| {label} | {row['away_points']} | {row['home_points']} | {row['home_minus_away']:+d} |"
        )

    max_leads = {row["team_tricode"]: row for row in flow["maximum_leads"]}
    lines.extend(
        [
            "",
            "## 3. 最大领先、领先交换与平分",
            "",
            "| 指标 | 数值 | 逐回合定位 |",
            "|---|---:|---|",
            f"| {away} 最大领先 | {max_leads[away]['maximum_lead']} | 第 {max_leads[away]['first_reached_period']} 节 {max_leads[away]['first_reached_clock']}，action_id {max_leads[away]['first_reached_action_id']}，比分 {format_score(max_leads[away]['score_away'], max_leads[away]['score_home'], game)} |",
            f"| {home} 最大领先 | {max_leads[home]['maximum_lead']} | 第 {max_leads[home]['first_reached_period']} 节 {max_leads[home]['first_reached_clock']}，action_id {max_leads[home]['first_reached_action_id']}，比分 {format_score(max_leads[home]['score_away'], max_leads[home]['score_home'], game)} |",
            f"| 领先交换 | {flow['lead_changes']} | 见下方口径 |",
            f"| 平分次数 | {flow['ties']} | 不含开场 0 比 0 |",
            "",
            "口径：领先交换先删除平分状态，再比较相邻两个非平分计分事件后的领先球队。首次出现领先不计为交换。平分次数统计计分事件完成后双方同分的次数，开场 0 比 0 不计。比分为空或比分没有变化的事件均不参与计算。",
            "",
            "## 4. 最大连续得分高潮及逐回合证据",
            "",
            f"最大高潮为 {run['scoring_team']} 连得 {run['run_points']} 分，从第 {run['start_period']} 节 {run['start_clock']} 的 action_id {run['start_action_id']} 开始，到第 {run['end_period']} 节 {run['end_clock']} 的 action_id {run['end_action_id']} 结束。比分从 {format_score(run['score_before_away'], run['score_before_home'], game)} 变为 {format_score(run['score_after_away'], run['score_after_home'], game)}。",
            "",
            "| action_id | 节次 | 时间 | 得分球队 | 本事件得分 | 事件前比分 | 事件后比分 | 描述 |",
            "|---:|---:|---|---|---:|---|---|---|",
        ]
    )
    for event in run["events"]:
        lines.append(
            "| {action_id} | {period} | {clock} | {team} | {points} | {before} | {after} | {description} |".format(
                action_id=event["action_id"],
                period=event["period"],
                clock=event["clock"],
                team=event["scoring_team"],
                points=event["points_added"],
                before=format_score(event["score_before_away"], event["score_before_home"], game),
                after=format_score(event["score_after_away"], event["score_after_home"], game),
                description=escape_cell(event["description"]),
            )
        )
    lines.extend(
        [
            "",
            "连续得分高潮口径：按 action_id 顺序读取比分确实变化的计分事件。同一球队连续完成的计分事件归为一段，直到对手得分为止。罚球逐次按实际增加的 1 分计入；换节本身不会切断高潮。",
            "",
            "## 5. 四因素对比",
            "",
            "| 球队 | eFG% | TOV% | ORB% | FTr |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for team in (away, home):
        row = factors[team]
        lines.append(
            f"| {team} | {row['effective_field_goal_percentage']['display']} | {row['turnover_rate']['display']} | {row['offensive_rebound_rate']['display']} | {row['free_throw_rate']['display']} |"
        )
    lines.extend(
        [
            "",
            "原始小数保存在 JSON 中。四项指标用于描述投篮、失误、进攻篮板和罚球出手的单场结果，单独一项或四项合计都不能自动证明确定的胜负原因。",
            "",
            "## 6. 实际出场球员数据",
            "",
            "实际出场口径为 `player_boxscores.minutes IS NOT NULL`。数据库记录为 `0:00` 且没有 DNP 标记的球员仍保留；minutes 为空的未出场名单已经排除。命中率分母为 0 时显示空值。",
        ]
    )
    for team in (away, home):
        lines.extend(
            [
                "",
                f"### {team}",
                "",
                "| 球员 | 时间 | 得分 | 篮板 | 助攻 | 抢断 | 盖帽 | 失误 | FG% | 3P% | FT% | 正负值 | TS% |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for player in facts["player_facts"]["teams"][team]:
            lines.append(
                f"| {escape_cell(player['name'])} | {player['minutes']} | {player['points']} | {player['rebounds']} | {player['assists']} | {player['steals']} | {player['blocks']} | {player['turnovers']} | {md_value(player['field_goal_percentage']['display'])} | {md_value(player['three_point_percentage']['display'])} | {md_value(player['free_throw_percentage']['display'])} | {player['plus_minus']:+g} | {md_value(player['true_shooting_percentage']['display'])} |"
            )

    lines.extend(
        [
            "",
            "### 队内单项最高",
            "",
            "| 球队 | 得分最高 | 助攻最多 | 篮板最多 | 正负值最高 |",
            "|---|---|---|---|---|",
        ]
    )
    for team in (away, home):
        leaders = facts["player_facts"]["leaders"][team]
        lines.append(
            f"| {team} | {leader_text(leaders['points'])} | {leader_text(leaders['assists'])} | {leader_text(leaders['rebounds'])} | {leader_text(leaders['plus_minus'])} |"
        )
    lines.extend(
        [
            "",
            "单场正负值只记录球员在场期间的球队净胜分结果，不能外推为长期能力。",
            "",
            "## 7. 投篮区域对比",
            "",
            "区域直接采用数据库字段 `shots.shot_zone_basic`。数据库还保留 `shot_zone_area` 和 `shot_zone_range`，本报告没有合并出新的自定义区域。出手占比以球队箱分 FGA 为分母。",
            "",
            f"| 区域 | {away} 出手/占比 | {away} 命中/命中率 | {away} 得分 | {home} 出手/占比 | {home} 命中/命中率 | {home} 得分 | 出手占比差 `{away} 减 {home}` |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for zone in zone_facts["zones"]:
        a = zone["teams"][away]
        h = zone["teams"][home]
        diff = zone["attempt_share_difference"]["raw"]
        lines.append(
            f"| {zone['zone']} | {a['attempts']} / {a['attempt_share']['display']} | {a['makes']} / {md_value(a['field_goal_percentage']['display'])} | {a['points']} | {h['attempts']} / {h['attempt_share']['display']} | {h['makes']} / {md_value(h['field_goal_percentage']['display'])} | {h['points']} | {diff * 100:+.1f} 个百分点 |"
        )

    lines.extend(
        [
            "",
            "### 差异最大的区域",
            "",
            f"出手占比差最大的三个区域按绝对值排序，方向仍以 `{away} 减 {home}` 表示：",
            "",
            f"| 区域 | {away} 出手/占比 | {home} 出手/占比 | 绝对差 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in zone_facts["largest_attempt_share_differences"]:
        lines.append(
            f"| {row['zone']} | {row['away_attempts']} / {row['away_share_raw'] * 100:.1f}% | {row['home_attempts']} / {row['home_share_raw'] * 100:.1f}% | {row['absolute_share_gap_display']} |"
        )
    fg_gap = zone_facts["largest_field_goal_percentage_difference"]
    lines.extend(
        [
            "",
            f"命中率差绝对值最大的区域是 {fg_gap['zone']}：{away} {fg_gap['away_makes']}/{fg_gap['away_attempts']}（{fg_gap['away_fg_pct_raw'] * 100:.1f}%），{home} {fg_gap['home_makes']}/{fg_gap['home_attempts']}（{fg_gap['home_fg_pct_raw'] * 100:.1f}%），相差 {fg_gap['absolute_fg_pct_gap_display']}。出手次数同时列出，避免忽略小样本。",
            "",
            f"{away} 主要得分区域：" + "；".join(f"{x['zone']}，{x['points']} 分，{x['makes']}/{x['attempts']}" for x in zone_facts['primary_scoring_zones'][away]) + "。",
            "",
            f"{home} 主要得分区域：" + "；".join(f"{x['zone']}，{x['points']} 分，{x['makes']}/{x['attempts']}" for x in zone_facts['primary_scoring_zones'][home]) + "。",
            "",
            "## 8. 三个值得后续分析的数据事实",
            "",
        ]
    )
    efg_high = max((away, home), key=lambda t: factors[t]["effective_field_goal_percentage"]["raw"])
    efg_low = home if efg_high == away else away
    orb_high = max((away, home), key=lambda t: factors[t]["offensive_rebound_rate"]["raw"])
    orb_low = home if orb_high == away else away
    lines.extend(
        [
            f"1. {efg_high} 的有效命中率为 {factors[efg_high]['effective_field_goal_percentage']['display']}，高于 {efg_low} 的 {factors[efg_low]['effective_field_goal_percentage']['display']}。这是投篮结果差异，现有字段没有直接标注形成差异的战术原因。",
            f"2. {orb_high} 的进攻篮板率为 {factors[orb_high]['offensive_rebound_rate']['display']}，高于 {orb_low} 的 {factors[orb_low]['offensive_rebound_rate']['display']}。该指标与终场结果需要同时保留，单项四因素无法充分解释整场胜负。",
            f"3. 全场最大连续得分高潮是 {run['scoring_team']} 连得 {run['run_points']} 分，比分从 {format_score(run['score_before_away'], run['score_before_home'], game)} 变为 {format_score(run['score_after_away'], run['score_after_home'], game)}。逐事件 action_id 已列在第 4 节。",
            "",
            "## 9. 指标公式和通俗解释",
            "",
            "| 指标 | 公式 | 通俗解释 |",
            "|---|---|---|",
            "| 有效命中率 eFG% | `(FGM + 0.5 × FG3M) / FGA` | 三分命中比两分命中多计半次价值，便于比较整体投篮效率。 |",
            "| 失误率 TOV% | `TOV / (FGA + 0.44 × FTA + TOV)` | 估计一次进攻以失误结束的频率。 |",
            "| 进攻篮板率 ORB% | `ORB / (ORB + 对手 DRB)` | 可争抢的进攻篮板机会中，本队拿到的比例。 |",
            "| 罚球率 FTr | `FTA / FGA` | 每次投篮出手对应多少次罚球出手。 |",
            "| 真实命中率 TS% | `PTS / [2 × (FGA + 0.44 × FTA)]` | 把两分、三分和罚球得分统一到一次终结效率框架中。 |",
            "",
            "所有比例在分母为 0 时返回空值。JSON 同时保存原始小数和展示百分比。",
            "",
            "## 10. 当前数据可以支持的判断",
            "",
            "1. 可以复核最终比分、每节和半场得分、最大领先、领先交换、平分次数与最大连续得分高潮。",
            "2. 可以比较两队单场四因素、实际出场球员箱分与数据库已有投篮区域分布。",
            "3. 可以使用 action_id、节次、时钟、事件前后比分追溯关键得分高潮。",
            "4. 可以确认区域出手和命中汇总与球队箱分 FGA、FGM 一致。",
            "",
            "## 11. 当前数据无法支持的判断",
            "",
            "1. 无法可靠重建全场每一时刻的五人阵容。第二节及以后节初阵容缺少完整显式记录。",
            "2. 无法仅凭这些事实证明战术因果、教练决策效果或某项指标决定了胜负。",
            "3. 无法用单场表现证明球员长期能力、球队长期趋势或未来预测效果。",
            "4. 无法回答防守人距离、战术跑位、伤病影响和录像语境问题，因为当前数据库没有这些字段。",
            "",
            "## 12. 可复核性与文件完整性",
            "",
            f"数据库运行前 SHA256：`{facts['evidence']['database']['sha256_before']}`",
            "",
            f"数据库运行后 SHA256：`{facts['evidence']['database']['sha256_after']}`",
            "",
            f"哈希一致：`{str(facts['evidence']['database']['unchanged']).lower()}`。查询使用 SQLite 只读 URI 和 `PRAGMA query_only=ON`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = parse_args(argv)
    game_id = args.game_id

    if not DATABASE_PATH.is_file():
        raise FileNotFoundError(DATABASE_PATH)
    if not VALIDATION_PATH.is_file():
        raise FileNotFoundError(VALIDATION_PATH)

    database_hash_before = sha256_file(DATABASE_PATH)
    queries = load_named_queries(SQL_PATH)
    database_uri = f"{DATABASE_PATH.resolve().as_uri()}?mode=ro"

    results = {}
    game_exists = False
    connection = sqlite3.connect(database_uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        game_exists = connection.execute(
            "SELECT 1 FROM games WHERE game_id = :game_id",
            {"game_id": game_id},
        ).fetchone() is not None
        if game_exists:
            for name in REQUIRED_QUERIES:
                results[name] = query_rows(connection, queries[name], game_id)
    finally:
        connection.close()

    database_hash_after = sha256_file(DATABASE_PATH)
    if database_hash_before != database_hash_after:
        raise RuntimeError("Database SHA256 changed during read-only fact generation")

    if not game_exists:
        print(
            f"Error: game_id '{game_id}' not found in the CourtVision V2 database.",
            file=sys.stderr,
        )
        return 2

    facts = make_fact_package(results, database_hash_before, database_hash_after)
    json_text = json.dumps(
        facts,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    markdown_text = build_markdown(facts)

    json_path = OUTPUTS_DIR / f"game_{game_id}_facts.json"
    markdown_path = OUTPUTS_DIR / f"game_{game_id}_facts.md"
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")

    print(f"Created {json_path.relative_to(V2_DIR.parent)}")
    print(f"Created {markdown_path.relative_to(V2_DIR.parent)}")
    print(f"Database SHA256 unchanged: {database_hash_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
