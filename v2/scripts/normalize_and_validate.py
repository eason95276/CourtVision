from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from v2.pregame_system.player_boxscore_normalizer import normalize_team_player_boxscores


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "nba_stats"
NORMALIZED_DIR = ROOT / "data" / "normalized"
NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)

GAMES = [
    {
        "phase": "early",
        "game_id": "0022500166",
        "game_date": "2025-11-04",
        "away_tricode": "ORL",
        "home_tricode": "ATL",
        "expected_away_points": 112,
        "expected_home_points": 127,
    },
    {
        "phase": "middle",
        "game_id": "0022500564",
        "game_date": "2026-01-13",
        "away_tricode": "PHX",
        "home_tricode": "MIA",
        "expected_away_points": 121,
        "expected_home_points": 127,
    },
    {
        "phase": "late",
        "game_id": "0022501099",
        "game_date": "2026-03-31",
        "away_tricode": "PHX",
        "home_tricode": "ORL",
        "expected_away_points": 111,
        "expected_home_points": 115,
    },
]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(name: str, payload: object) -> None:
    (NORMALIZED_DIR / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def shot_rows(payload: dict) -> list[dict]:
    result_set = next(item for item in payload["resultSets"] if item["name"] == "Shot_Chart_Detail")
    return [dict(zip(result_set["headers"], row)) for row in result_set["rowSet"]]


def match_incoming_player(label: str, team_players: list[dict]) -> tuple[int | None, str]:
    target = clean_name(label)
    exact = [
        player
        for player in team_players
        if target
        in {
            clean_name(player["family_name"]),
            clean_name(player["full_name"]),
            clean_name(player["name_i"]),
        }
    ]
    if len(exact) == 1:
        return exact[0]["person_id"], "exact"
    contains = [
        player
        for player in team_players
        if target and target in clean_name(player["full_name"])
    ]
    if len(contains) == 1:
        return contains[0]["person_id"], "contains"
    return None, "ambiguous" if exact or contains else "unmatched"


def main() -> None:
    normalized_games: list[dict] = []
    normalized_teams: list[dict] = []
    normalized_players: list[dict] = []
    normalized_actions: list[dict] = []
    normalized_shots: list[dict] = []
    lineup_results: list[dict] = []
    validations: list[dict] = []

    for selected in GAMES:
        game_id = selected["game_id"]
        box = load_json(RAW_DIR / f"{game_id}_boxscore_traditional_v3.json")
        pbp = load_json(RAW_DIR / f"{game_id}_play_by_play_v3.json")
        shots_payload = load_json(RAW_DIR / f"{game_id}_shot_chart_detail.json")
        box_game = box["boxScoreTraditional"]
        actions = pbp["game"]["actions"]
        shots = shot_rows(shots_payload)

        game_players: list[dict] = []
        teams_by_id: dict[int, dict] = {}
        for side in ("awayTeam", "homeTeam"):
            team = box_game[side]
            team_row = {
                "game_id": game_id,
                "side": "away" if side == "awayTeam" else "home",
                "team_id": team["teamId"],
                "team_city": team["teamCity"],
                "team_name": team["teamName"],
                "team_tricode": team["teamTricode"],
                **team["statistics"],
            }
            teams_by_id[team["teamId"]] = team_row
            normalized_teams.append(team_row)

            player_rows = normalize_team_player_boxscores(game_id, team)
            game_players.extend(player_rows)
            normalized_players.extend(player_rows)

        player_ids = {player["person_id"] for player in game_players}
        players_by_team: dict[int, list[dict]] = {
            team_id: [player for player in game_players if player["team_id"] == team_id]
            for team_id in teams_by_id
        }

        for action in actions:
            normalized_actions.append({"game_id": game_id, **action})
        for shot in shots:
            normalized_shots.append({key.casefold(): value for key, value in shot.items()})

        final_action = next(
            action
            for action in reversed(actions)
            if str(action.get("scoreHome", "")).strip()
            and str(action.get("scoreAway", "")).strip()
        )
        away_team = box_game["awayTeam"]
        home_team = box_game["homeTeam"]
        game_row = {
            "phase": selected["phase"],
            "game_id": game_id,
            "game_date": selected["game_date"],
            "season": "2025-26",
            "season_type": "Regular Season",
            "away_team_id": away_team["teamId"],
            "away_team_tricode": away_team["teamTricode"],
            "away_points": away_team["statistics"]["points"],
            "home_team_id": home_team["teamId"],
            "home_team_tricode": home_team["teamTricode"],
            "home_points": home_team["statistics"]["points"],
        }
        normalized_games.append(game_row)

        action_ids = [action["actionId"] for action in actions]
        action_numbers = [action["actionNumber"] for action in actions]
        shot_keys = [
            (shot["GAME_ID"], shot["GAME_EVENT_ID"], shot["PLAYER_ID"])
            for shot in shots
        ]
        pbp_person_ids = {
            int(action["personId"])
            for action in actions
            if str(action.get("personId", "")).isdigit() and int(action["personId"]) > 0
        }
        shot_person_ids = {int(shot["PLAYER_ID"]) for shot in shots}
        shot_team_ids = {int(shot["TEAM_ID"]) for shot in shots}
        box_team_ids = set(teams_by_id)
        unmatched_pbp_person_actions = [
            action
            for action in actions
            if int(action.get("personId") or 0) > 0
            and int(action.get("personId") or 0) not in player_ids
            and int(action.get("personId") or 0) not in box_team_ids
            and action.get("actionType", "").casefold() != "instant replay"
        ]

        substitutions = [
            action for action in actions if action.get("actionType", "").casefold() == "substitution"
        ]
        current_lineups: dict[int, set[int]] = {
            team_id: {
                player["person_id"]
                for player in players
                if player["starter"]
            }
            for team_id, players in players_by_team.items()
        }
        unmatched_substitutions: list[dict] = []
        lineup_count_violations: list[dict] = []
        for action in substitutions:
            description = action.get("description", "")
            parsed = re.match('SU<local-configured-path>', description, flags=re.IGNORECASE)
            if not parsed:
                unmatched_substitutions.append(
                    {"action_id": action["actionId"], "reason": "description_not_parsed", "description": description}
                )
                continue
            incoming_label = parsed.group(1).strip()
            outgoing_id = int(action.get("personId") or 0)
            team_id = int(action["teamId"])
            incoming_id, match_method = match_incoming_player(incoming_label, players_by_team[team_id])
            if incoming_id is None or outgoing_id not in current_lineups[team_id]:
                unmatched_substitutions.append(
                    {
                        "action_id": action["actionId"],
                        "period": action["period"],
                        "clock": action["clock"],
                        "team_id": team_id,
                        "incoming_label": incoming_label,
                        "incoming_id": incoming_id,
                        "outgoing_id": outgoing_id,
                        "match_method": match_method,
                        "reason": "incoming_unmatched_or_outgoing_not_in_current_lineup",
                    }
                )
                continue
            current_lineups[team_id].remove(outgoing_id)
            current_lineups[team_id].add(incoming_id)
            if len(current_lineups[team_id]) != 5:
                lineup_count_violations.append(
                    {"action_id": action["actionId"], "team_id": team_id, "lineup_size": len(current_lineups[team_id])}
                )

        lineup_results.append(
            {
                "game_id": game_id,
                "initial_starters_identified": all(
                    len([player for player in players if player["starter"]]) == 5
                    for players in players_by_team.values()
                ),
                "substitution_events": len(substitutions),
                "substitutions_applied": len(substitutions) - len(unmatched_substitutions),
                "unmatched_or_inconsistent_substitutions": unmatched_substitutions,
                "lineup_count_violations": lineup_count_violations,
                "full_game_lineup_reconstruction_status": "partial",
                "limitation": "The feed identifies the game starters and in-game substitutions. It does not explicitly identify each later-period opening lineup, so carrying the prior period lineup forward cannot prove the five players on court after quarter breaks.",
            }
        )

        per_team_attempt_checks = []
        for team_id, team in teams_by_id.items():
            team_shots = [shot for shot in shots if int(shot["TEAM_ID"]) == team_id]
            per_team_attempt_checks.append(
                {
                    "team_id": team_id,
                    "team_tricode": team["team_tricode"],
                    "box_fga": team["fieldGoalsAttempted"],
                    "shot_rows": len(team_shots),
                    "box_fgm": team["fieldGoalsMade"],
                    "shot_makes": sum(int(shot["SHOT_MADE_FLAG"]) for shot in team_shots),
                    "player_points_sum": sum(
                        int(player.get("points") or 0) for player in players_by_team[team_id]
                    ),
                    "team_points": team["points"],
                }
            )

        null_counts = {
            "pbp_blank_team_id": sum(not action.get("teamId") for action in actions),
            "pbp_blank_person_id": sum(not action.get("personId") for action in actions),
            "pbp_blank_score_home": sum(str(action.get("scoreHome", "")).strip() == "" for action in actions),
            "pbp_blank_score_away": sum(str(action.get("scoreAway", "")).strip() == "" for action in actions),
            "player_blank_statistics": sum(not player.get("minutes") for player in game_players),
            "shot_blank_player_id": sum(not shot.get("PLAYER_ID") for shot in shots),
            "shot_blank_coordinates": sum(shot.get("LOC_X") is None or shot.get("LOC_Y") is None for shot in shots),
        }

        checks = {
            "game_id_consistent": (
                box_game["gameId"] == game_id
                and pbp["game"]["gameId"] == game_id
                and {str(shot["GAME_ID"]) for shot in shots} == {game_id}
            ),
            "teams_match_selection": (
                away_team["teamTricode"] == selected["away_tricode"]
                and home_team["teamTricode"] == selected["home_tricode"]
            ),
            "box_score_matches_expected": (
                away_team["statistics"]["points"] == selected["expected_away_points"]
                and home_team["statistics"]["points"] == selected["expected_home_points"]
            ),
            "pbp_final_score_matches_box": (
                int(final_action["scoreAway"]) == away_team["statistics"]["points"]
                and int(final_action["scoreHome"]) == home_team["statistics"]["points"]
            ),
            "shot_players_join_box_players": shot_person_ids <= player_ids,
            "shot_teams_join_box_teams": shot_team_ids == box_team_ids,
            "pbp_person_ids_are_polymorphic": True,
            "pbp_player_like_person_ids_join_box_players": not unmatched_pbp_person_actions,
            "shot_counts_match_team_box": all(
                item["box_fga"] == item["shot_rows"]
                and item["box_fgm"] == item["shot_makes"]
                for item in per_team_attempt_checks
            ),
            "player_points_sum_matches_team_points": all(
                item["player_points_sum"] == item["team_points"]
                for item in per_team_attempt_checks
            ),
            "no_duplicate_action_ids": len(action_ids) == len(set(action_ids)),
            "no_duplicate_action_numbers": len(action_numbers) == len(set(action_numbers)),
            "no_duplicate_shot_keys": len(shot_keys) == len(set(shot_keys)),
        }
        validations.append(
            {
                "game_id": game_id,
                "checks": checks,
                "counts": {
                    "teams": len(teams_by_id),
                    "players": len(game_players),
                    "pbp_actions": len(actions),
                    "shots": len(shots),
                    "substitutions": len(substitutions),
                    "pbp_person_ids_not_in_box": sorted(pbp_person_ids - player_ids),
                    "pbp_player_like_actions_not_in_box": len(unmatched_pbp_person_actions),
                    "shot_person_ids_not_in_box": sorted(shot_person_ids - player_ids),
                },
                "per_team_attempt_checks": per_team_attempt_checks,
                "missing_field_counts": null_counts,
                "duplicate_action_id_values": sorted(
                    key for key, count in Counter(action_ids).items() if count > 1
                ),
                "duplicate_action_number_values": sorted(
                    key for key, count in Counter(action_numbers).items() if count > 1
                ),
                "duplicate_shot_keys": [
                    list(key) for key, count in Counter(shot_keys).items() if count > 1
                ],
                "pbp_player_like_actions_not_in_box_examples": [
                    {
                        "action_id": action["actionId"],
                        "person_id": action["personId"],
                        "action_type": action["actionType"],
                        "description": action["description"],
                    }
                    for action in unmatched_pbp_person_actions[:10]
                ],
            }
        )

    save_json("games.json", normalized_games)
    save_json("team_boxscores.json", normalized_teams)
    save_json("player_boxscores.json", normalized_players)
    save_json("play_by_play.json", normalized_actions)
    save_json("shots.json", normalized_shots)
    save_json("lineup_reconstruction.json", lineup_results)
    save_json("validation_results.json", validations)
    print(json.dumps({"games": normalized_games, "validations": validations, "lineups": lineup_results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
