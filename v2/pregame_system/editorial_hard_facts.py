"""Narrow, deterministic Editorial blockers for typed high-confidence facts.

This is not a prose entailment engine. It checks exact numeric claims when a
known statistic dimension is named, explicit game venue claims, and direct
availability/starter contradictions against structured, game-scoped records.
"""
from __future__ import annotations

import re
import json


_SAMPLE_RE = re.compile(r"(?:(?:近|最近|过去|last\s+)[ ]*)?(?P<count>\d{1,3}|[零〇一二三四五六七八九十百两]+)\s*(?:场(?:比赛)?|games?)", re.I)
_CHINESE_DIGITS = "零〇一二两三四五六七八九十百"
_NUMBER_RE = re.compile(rf"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d+)?|[{_CHINESE_DIGITS}]+(?:点[{_CHINESE_DIGITS}]+)?)")
_SEASON_RE = re.compile(r"(?P<start>20\d{2})\s*[-/～—]\s*(?P<end>\d{2,4})")

_METRIC_ALIASES = {
    "points_for_per_game": ("场均得分", "得分"),
    "assists_per_game": ("场均助攻", "助攻", "assists"),
    "points_against_per_game": ("场均失分", "失分", "丢分"),
    "point_margin_per_game": ("场均净胜分", "净胜分", "净胜", "净负", "净输"),
    "win_rate": ("胜率",),
    "win_pct": ("胜率",),
    "expected_total": ("总分",),
    "net_rating": ("净效率",),
    "off_rating": ("进攻效率",),
    "def_rating": ("防守效率",),
    "three_pct": ("三分命中率", "三分命中"),
    "rebounds_per_game": ("场均篮板", "篮板"),
    "turnovers_per_game": ("场均失误", "失误"),
    "turnover_rate": ("失误率", "失误比例"),
    "minutes_per_game": ("场均分钟", "场均上场时间", "场均出场时间", "minutes per game", "average minutes"),
    "games_played": ("出战", "出场", "登场"),
    "rebound_share": ("篮板份额", "篮板率"),
    "wins": ("胜场", "胜"),
    "losses": ("负场", "负"),
}
_PERCENT_METRICS = {"win_rate", "win_pct", "three_pct"}
_CANONICAL_METRICS = {"points": "points_for_per_game", "rebounds": "rebounds_per_game",
                      "assists": "assists_per_game", "minutes": "minutes_per_game",
                      "mp": "minutes_per_game"}
_CLAIM_TYPES = {
    "historical_current_factual_statistic", "locked_prediction_output", "forecast_or_game_script_estimate",
    "conditional_or_hypothetical_number", "structural_or_count_expression", "team_identity", "other_numeric",
}
_CITY_TRANSLATIONS = {
    "Toronto":"多伦多", "Quebec City":"魁北克城", "New York":"纽约", "Boston":"波士顿",
    "Brooklyn":"布鲁克林", "Charlotte":"夏洛特", "Chicago":"芝加哥", "Cleveland":"克利夫兰",
    "Dallas":"达拉斯", "Denver":"丹佛", "Detroit":"底特律", "San Francisco":"旧金山",
    "Houston":"休斯敦", "Indianapolis":"印第安纳波利斯", "Los Angeles":"洛杉矶",
    "Memphis":"孟菲斯", "Miami":"迈阿密", "Milwaukee":"密尔沃基", "Minneapolis":"明尼阿波利斯",
    "New Orleans":"新奥尔良", "Oklahoma City":"俄克拉荷马城", "Orlando":"奥兰多",
    "Philadelphia":"费城", "Phoenix":"菲尼克斯", "Portland":"波特兰", "Sacramento":"萨克拉门托",
    "San Antonio":"圣安东尼奥", "Salt Lake City":"盐湖城", "Washington":"华盛顿",
    "Atlanta":"亚特兰大", "Golden State":"旧金山",
}


def _chinese_integer(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value.isdigit():
        return int(value)
    if "点" in value:
        left, _, right = value.partition("点")
        whole = _chinese_integer(left)
        digit_values = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if whole is None or not right or any(char not in digit_values for char in right):
            return None
        return whole + int("".join(str(digit_values[c]) for c in right)) / (10 ** len(right))
    if all(char in digits for char in value):
        return int("".join(str(digits[c]) for c in value))
    if "百" in value:
        left, _, right = value.partition("百")
        hundreds = digits.get(left, 1) if left else 1
        tail = _chinese_integer(right) if right else 0
        return hundreds * 100 + (tail or 0)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return digits.get(value)


def _flexible_alias_pattern(alias: str) -> str:
    return r"\s*".join(re.escape(char) for char in str(alias) if not char.isspace())


def _team_name_spans(text: str, game: dict) -> list[tuple[int, int, str]]:
    spans = []
    for side in ("home", "away"):
        team = game.get(side) or {}
        aliases = {str(team.get(key) or "") for key in ("name_zh", "name", "abbreviation")}
        for alias in filter(None, aliases):
            for match in re.finditer(_flexible_alias_pattern(alias), text, re.I):
                spans.append((match.start(), match.end(), side))
    return spans


def classify_numeric_claim_type(text: str, start: int, end: int, metric: str | None,
                                projection: dict) -> str:
    """Classify numeric semantics before considering nearby statistic labels."""
    game = projection.get("game") or {}
    if text[max(0, start-2):end+2].casefold().replace(" ", "") == "第25秒":
        return "structural_or_count_expression"
    if any(a <= start and end <= b for a, b, _side in _team_name_spans(text, game)):
        return "team_identity"

    sentence_start = max((m.end() for m in re.finditer(r"[。！？；;\n]", text[:start])), default=0)
    sentence_end = next((m.start() for m in re.finditer(r"[。！？；;\n]", text[end:])), None)
    sentence_end = end + sentence_end if sentence_end is not None else len(text)
    sentence = text[sentence_start:sentence_end]
    prefix = text[max(sentence_start, start-80):start]
    suffix = text[end:min(sentence_end, end+60)]
    before = prefix + text[start:end] + suffix

    token = text[start:end]
    # Position labels and a count of scoring options do not assert box-score values.
    if (token in {"1", "2", "3", "4", "5", "一", "二", "三", "四", "五"}
            and suffix.startswith("号位")
            or re.match(r"个(?:稳定|可靠|主要|重要|外线|内线)?得分点", suffix)
            or token in {"一", "1"} and re.search(r"(?:多|少)$", prefix)
            and suffix.startswith("个") and "得分点" in prefix[-30:]):
        return "structural_or_count_expression"
    if (token in {"三", "3"} and re.match(r"分(?:命中|投|出手|线|球|手感|不稳|火力|威胁|空间|能力)", suffix)
            or re.match(r"人|名(?:球员|主力|首发|替补)|个(?:人|球员|主力)", suffix)):
        return "structural_or_count_expression"
    if token == "一" and prefix.endswith("之"):
        return "other_numeric"
    if token in {"一两", "两三", "三四", "四五", "五六", "六七", "七八", "八九"} and not re.search(
            r"场均|上季|上赛季|本季|本赛季|去年|命中率|效率|上一场|单场|昨天", sentence):
        return "forecast_or_game_script_estimate"
    if re.match(r"个(?:词|问题|重点|角度)|件(?:事|事情)|句", suffix):
        return "structural_or_count_expression"
    # A count of tactical options is not a score, even beside the word 得分.
    if re.match(r"条(?:得分|进攻|突破|组织)?(?:路|路线|路径)|套(?:进攻|防守)?(?:方案|打法|配合)|种(?:进攻|防守)?(?:办法|选择|打法)", suffix):
        return "structural_or_count_expression"
    # Qualitative degree and verb reduplication do not assert a value of one.
    # Keep actual decimals (一点五) and units (一分) on the numeric path.
    if token == "一" and (re.match(r"点(?![零〇一二两三四五六七八九十百\d])|些|旦|上来", suffix)
            or prefix[-1:] and re.fullmatch(r"[\u4e00-\u9fff]", prefix[-1:])
            and suffix.startswith(prefix[-1:])):
        return "other_numeric"

    # Ordinals and common idioms are not exact statistical claims.
    if (re.search(r"第\s*$", prefix) and re.match(r"\s*(?:节|场|轮|次|名|位|顺位|责任人)", suffix)
            or token in {"一", "两", "二", "三", "四"} and re.search(
                r"(?:一环|第一(?:道|个|节|步|阵)|两边|两侧|三节|二次进攻|二次得分|两三回合)", sentence)):
        return "structural_or_count_expression"

    if re.search(r"如果|假如|倘若|若是|若|一旦|只要|条件是", prefix, re.I):
        return "conditional_or_hypothetical_number"
    if re.search(r"(?:健康出战|出战并|承担.{0,12}(?:正常|接近正常)?负荷|全部确认出战|确认.{0,8}出战)", prefix) and re.search(
            r"(?:胜率|分差|总分).{0,18}(?:下调|上调|收窄|扩大|升至|降至)", prefix[-80:]):
        return "conditional_or_hypothetical_number"

    if (re.match(r"\s*分(?!钟|之)", suffix)
            and re.search(r"一次|单次|每次|每回合|一个回合", prefix)
            and re.search(r"可能|预计|有望|本场|覆盖让分", sentence)
            and not re.search(r"场均|上季|上赛季|去年|过去|最近\s*\d+场", sentence)):
        return "forecast_or_game_script_estimate"

    # Explicit final-forecast language is checked only against locked Final.
    if metric in {"win_rate", "win_pct", "point_margin_per_game", "expected_total"} and re.search(
            r"第25秒.{0,8}(?:预测|预计)|最终预测|我的最终预测|最终我(?:站|看好|认为|选)|我最终|我给|我选|第25秒看好", sentence):
        return "locked_prediction_output"

    range_expression = re.search(
        r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百两]+(?:点[零〇一二三四五六七八九]+)?)"
        r"\s*(?:[-–—~～到至])\s*(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百两]+(?:点[零〇一二三四五六七八九]+)?)",
        sentence)
    range_contains_token = bool(range_expression and sentence_start + range_expression.start() <= start
                                and end <= sentence_start + range_expression.end())
    forecast_cue = re.search(r"预计|预测|预期|可能|或许|也许|区间|上下|拉开|扩大|收窄|维持|领先|分差|估计|预计出战|上场|下调|上调|降至|升至", before)
    if range_contains_token and forecast_cue:
        return "forecast_or_game_script_estimate"
    if re.search(r"预计|预测|预期|可能|或许|也许|区间|上下|拉开|扩大|收窄|维持|领先|分差|预计出战|下调|上调|降至|升至", prefix[-36:], re.I):
        return "forecast_or_game_script_estimate"
    if re.search(r"(上季|上赛季|上个赛季|本季|本赛季|这赛季|去年).{0,20}(?:场均|胜率|命中率|净效率|篮板|助攻|得分|失分)", sentence):
        return "historical_current_factual_statistic"
    return "historical_current_factual_statistic" if metric else "other_numeric"


def _metric_rows(projection: dict) -> list[dict]:
    rows = []
    for fact in projection.get("facts", []):
        value = fact.get("value") or {}
        series = value.get("series_summary")
        if fact.get("kind") == "HISTORICAL" and isinstance(series, dict):
            # These deterministic opponent-series statistics are a distinct
            # sample, not the team's last-N form or its whole playoff run.
            for side in ("home", "away"):
                team_id = (projection.get("game", {}).get(side) or {}).get("id")
                metrics = (series.get("team_summaries") or {}).get(str(team_id), {})
                for metric, observed in metrics.items():
                    canonical = {"average_margin": "point_margin_per_game",
                                 "points_per_game": "points_for_per_game"}.get(metric, metric)
                    if canonical == "wins" or canonical not in _METRIC_ALIASES or not isinstance(observed, (int, float)) or isinstance(observed, bool):
                        continue
                    rows.append({"fact_id": fact["id"], "side": side, "metric": canonical,
                                 "value": float(observed), "sample": "h2h_series", "games": metrics.get("games"),
                                 "season_start": series.get("season_start"), "kind": "HISTORICAL", "player": None})
        if fact.get("kind") in {"MODEL_OUTPUT", "PUBLIC_DECISION"}:
            numbers = value.get("numbers") or {}
            numeric_metrics = {
                "win_rate": numbers.get("win_probability"),
                "point_margin_per_game": _winner_positive_margin(numbers),
                "expected_total": numbers.get("expected_total"),
            }
            for metric, observed in numeric_metrics.items():
                if isinstance(observed, (int, float)) and not isinstance(observed, bool):
                    rows.append({"fact_id": fact["id"], "side": None, "metric": metric,
                        "value": float(observed), "sample": None, "games": None,
                        "season_start": None, "kind": fact.get("kind"), "player": None})
        metrics = value.get("metrics")
        if isinstance(metrics, dict):
            for metric, observed in metrics.items():
                base_metric = metric[:-3] if metric.endswith("_15") else metric
                base_metric = _CANONICAL_METRICS.get(base_metric, base_metric)
                metric_detail = observed if isinstance(observed, dict) else {}
                diagnostic = bool(metric_detail.get("validation") == "mismatch")
                metric_value = metric_detail.get("value") if metric_detail else observed
                if (base_metric not in _METRIC_ALIASES or not isinstance(metric_value, (int, float))
                        or isinstance(metric_value, bool)):
                    continue
                games = metrics.get("games")
                if metric.endswith("_15") and value.get("sample") == "rolling_15":
                    games = value.get("requested_games", value.get("games"))
                rows.append({"fact_id": fact["id"], "side": value.get("side"), "metric": base_metric,
                             "value": float(metric_value), "sample": value.get("sample"),
                             "games": games, "season_start": value.get("season_start"),
                             "valid_games": metric_detail.get("valid_games"),
                             "missing_games": metric_detail.get("missing_games"),
                             "observed_games": value.get("observed_games"),
                             "unverified_value": diagnostic,
                             "kind": fact.get("kind"), "player": None})
        diagnostic = value.get("diagnostic_metric")
        if fact.get("kind") == "UNKNOWN" and isinstance(diagnostic, dict):
            name = str(diagnostic.get("name") or "")
            base_metric = name[:-3] if name.endswith("_15") else name
            base_metric = _CANONICAL_METRICS.get(base_metric, base_metric)
            observed = diagnostic.get("value")
            if base_metric in _METRIC_ALIASES and isinstance(observed, (int, float)) and not isinstance(observed, bool):
                rows.append({"fact_id": fact["id"], "side": value.get("side"), "metric": base_metric,
                    "value": float(observed), "sample": "rolling_15",
                    "games": diagnostic.get("requested_games"),
                    "observed_games": diagnostic.get("observed_games"),
                    "valid_games": diagnostic.get("valid_games"),
                    "missing_games": diagnostic.get("missing_games"),
                    "unverified_value": True, "season_start": None,
                    "kind": "UNKNOWN", "player": None})
        if fact.get("kind") == "HISTORICAL":
            for player in value.get("players") or []:
                averages = player.get("average_statistics") or {}
                counts = player.get("stat_sample_counts") or {}
                for metric, observed in averages.items():
                    base_metric = _CANONICAL_METRICS.get(metric, metric)
                    if base_metric not in _METRIC_ALIASES or not isinstance(observed, (int, float)) or isinstance(observed, bool):
                        continue
                    rows.append({"fact_id": fact["id"], "side": value.get("side"), "metric": base_metric,
                                 "value": float(observed), "sample": "previous_season_player",
                                 "games": counts.get(metric), "season_start": value.get("season_start"),
                                 "kind": fact.get("kind"), "player": player.get("player")})
                games_played = player.get("games_played", player.get("games"))
                if isinstance(games_played, (int, float)) and not isinstance(games_played, bool):
                    rows.append({"fact_id": fact["id"], "side": value.get("side"),
                        "metric": "games_played", "value": float(games_played),
                        "sample": "previous_season_player", "games": games_played,
                        "season_start": value.get("season_start"), "kind": fact.get("kind"),
                        "player": player.get("player") or player.get("player_name")})
    return rows


def _winner_positive_margin(numbers: dict) -> float | None:
    """Normalize winner-positive expected margin without confusing ATS sign."""
    margin = numbers.get("expected_margin")
    if isinstance(margin, (int, float)) and not isinstance(margin, bool):
        return abs(float(margin))
    home_margin = numbers.get("expected_home_margin")
    if not isinstance(home_margin, (int, float)) or isinstance(home_margin, bool):
        return None
    winner = numbers.get("winner_side")
    signed_for_winner = float(home_margin) if winner == "home" else (
        -float(home_margin) if winner == "away" else abs(float(home_margin)))
    return signed_for_winner


def _locked_prediction_source(projection: dict) -> tuple[dict, str | None] | tuple[None, None]:
    """Return only the locked Final/public decision, never Pure or Independent."""
    for fact in projection.get("facts", []):
        value = fact.get("value") or {}
        if fact.get("kind") == "PUBLIC_DECISION":
            numbers = value.get("numbers") or {}
            if numbers:
                return numbers, fact.get("id")
    for fact in projection.get("facts", []):
        value = fact.get("value") or {}
        if fact.get("kind") == "MODEL_OUTPUT" and value.get("owner") == "locked_final":
            numbers = value.get("numbers") or {}
            if numbers:
                return numbers, fact.get("id")
    numbers = projection.get("locked_prediction")
    if isinstance(numbers, dict) and numbers:
        return numbers, None
    return None, None


def _rounding_tolerance(token: str) -> float:
    places = len(token.partition(".")[2]) if "." in token else len(token.partition("点")[2]) if "点" in token else 0
    return max(0.005, 0.51 * (10 ** -places))


def _sample_dimension(text: str, match) -> str:
    before = text[max(0, match.start("count") - 24):match.start("count")]
    if re.search(r"(?:有效(?:观测|樣本|样本|数据)?|valid(?:\s+(?:observations?|samples?))?)\s*$", before, re.I):
        return "valid_games"
    if re.search(r"(?:实际(?:纳入|观察|计入|记录)?|纳入|观察到|observed|actual)\s*$", before, re.I):
        return "observed_games"
    return "requested_games"


def _metric_near_sample(text: str, match, rows: list[dict]) -> str | None:
    start = max(0, match.start() - 80)
    end = min(len(text), match.end() + 80)
    left = max(text.rfind(mark, 0, match.start()) for mark in "。！？；;\n")
    right_candidates = [text.find(mark, match.end()) for mark in "。！？；;\n"]
    right_candidates = [pos for pos in right_candidates if pos >= 0]
    clause_start = max(start, left + 1)
    clause_end = min(end, min(right_candidates) if right_candidates else len(text))
    clause = text[clause_start:clause_end]
    labels = []
    for metric in {row["metric"] for row in rows}:
        for alias in _METRIC_ALIASES.get(metric, ()):
            pos = clause.find(alias)
            if pos >= 0:
                labels.append((abs((clause_start + pos) - match.start()), metric))
    return min(labels)[1] if labels else None


def _sample_count_issues_for_text(text: str, projection: dict, unit_id: str) -> list[dict]:
    rows = [row for row in _metric_rows(projection) if row.get("sample") == "rolling_15"]
    if not rows:
        return []
    issues = []
    game = projection.get("game") or {}
    for match in _SAMPLE_RE.finditer(text):
        count = _chinese_integer(match.group("count"))
        if count is None:
            continue
        appearance_prefix = text[max(0, match.start("count") - 16):match.start("count")]
        if re.search(r"(?:(?:仅|共|只|曾)\s*)?(?:出战|出场|登场|参加)(?:了)?\s*(?:仅|共|约|多达)?\s*$",
                     appearance_prefix):
            # Player season appearances are a statistic dimension, not a rolling-window N.
            continue
        dimension = _sample_dimension(text, match)
        metric = _metric_near_sample(text, match, rows) if dimension == "valid_games" else None
        sentence_start = max((m.end() for m in re.finditer(r"[。！？；;\n]", text[:match.start()])), default=0)
        sentence_end = next((m.start() for m in re.finditer(r"[。！？；;\n]", text[match.end():])), None)
        sentence_end = match.end() + sentence_end if sentence_end is not None else len(text)
        sentence = text[sentence_start:sentence_end]
        side = _mentioned_side(sentence, game)
        if dimension == "valid_games" and metric is None:
            # Valid N is defined per metric. Do not guess which statistic an
            # unqualified count belongs to, or mistake a player's appearance N.
            continue
        window_hint = bool(re.search(r"窗口|滚动|window|rolling", sentence, re.I))
        sample_scope = bool(re.search(r"球队样本|球队窗口|两队窗口|双方窗口|team window|sample window", sentence, re.I))
        if dimension == "requested_games" and metric is None and not window_hint:
            continue
        if dimension == "observed_games" and side is None and not (window_hint or sample_scope):
            continue
        relevant = [row for row in rows if (side is None or row.get("side") in (None, side))
                    and (metric is None or row.get("metric") == metric)]
        if not relevant:
            continue
        field = "games" if dimension == "requested_games" else dimension
        observed_values = [row.get(field) for row in relevant]
        known = [int(value) for value in observed_values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        # Valid N is metric-specific. If its value is unknown or the sentence
        # does not identify a metric, do not guess which denominator it means.
        if dimension == "valid_games" and not known:
            mismatch = True
        elif side is None:
            mismatch = not known or set(known) != {count} or len(known) != len(observed_values)
        else:
            mismatch = count not in known
        if mismatch:
            issues.append({"unit_id": unit_id, "code": "exact_stat_sample_mismatch", "quote": text,
                "stated_sample_games": count, "sample_dimension": dimension,
                "metric": metric, "available_fact_ids": sorted({row["fact_id"] for row in relevant})})
    return issues


def _player_appearance_issues_for_text(text: str, projection: dict, unit_id: str,
                                       player_name: str | None = None) -> list[dict]:
    """Verify season appearances as a player statistic, never as rolling N."""
    claims = list(re.finditer(
        r"(?:出战|出场|登场|参加)(?:了)?\s*(?:仅|共|约|多达)?\s*"
        r"(?P<count>\d{1,3}|[零〇一二两三四五六七八九十百]+)\s*场", text))
    if not claims:
        return []
    facts = [f for f in projection.get("facts", []) if f.get("kind") == "HISTORICAL"]
    names = [player_name] if player_name else []
    if not names:
        for fact in projection.get("facts", []):
            if fact.get("kind") not in {"CURRENT", "HISTORICAL"}:
                continue
            value = fact.get("value") or {}
            roster = value.get("roster_snapshot") or []
            players = value.get("players") or []
            for player in roster:
                name = str(player.get("name") or player.get("player_name") or "").strip()
                if name and name.casefold() in text.casefold():
                    names.append(name)
            for player in players:
                name = str(player.get("player") or player.get("player_name") or "").strip()
                if name and name.casefold() in text.casefold():
                    names.append(name)
    names = list(dict.fromkeys(name for name in names if name))
    if not names:
        return []
    season_labeled = bool(re.search(
        r"上季|上赛季|上个赛季|本季|本赛季|这赛季|去年|20\d{2}\s*[-/～—]\s*\d{2,4}|"
        r"previous season|last season|current season", text, re.I))
    issues = []
    wanted = {name.casefold() for name in names}
    for match in claims:
        count = _chinese_integer(match.group("count"))
        if count is None:
            continue
        matching_rows = []
        candidate_fact_ids = []
        for fact in facts:
            value = fact.get("value") or {}
            rows = value.get("players") or []
            matching_players = [row for row in rows
                if str(row.get("player") or row.get("player_name") or "").casefold() in wanted]
            if matching_players:
                candidate_fact_ids.append(fact["id"])
                for player in matching_players:
                    observed = player.get("games_played", player.get("games"))
                    if isinstance(observed, (int, float)) and not isinstance(observed, bool):
                        matching_rows.append((float(observed), fact["id"]))
        if not matching_rows:
            issues.append({"unit_id": unit_id, "code": "exact_stat_value_unverified", "quote": text,
                "metric": "games_played", "stated_value": str(count),
                "available_fact_ids": sorted(set(candidate_fact_ids))})
            continue
        if not any(abs(value - count) <= 0.005 for value, _fact_id in matching_rows):
            issues.append({"unit_id": unit_id, "code": "exact_stat_value_mismatch", "quote": text,
                "metric": "games_played", "stated_value": str(count),
                "available_fact_ids": sorted({fact_id for _value, fact_id in matching_rows})})
        elif not season_labeled:
            issues.append({"unit_id": unit_id, "code": "exact_stat_season_unlabeled", "quote": text,
                "metric": "games_played", "available_fact_ids": sorted({fact_id for _value, fact_id in matching_rows})})
    return issues


def _number_value(token: str) -> float | None:
    token = token.lstrip("+-")
    if token.replace(".", "", 1).isdigit():
        return float(token)
    parsed = _chinese_integer(token)
    return float(parsed) if parsed is not None else None


def _matches_value(token: str, row: dict) -> bool:
    shown_value = _number_value(token)
    if shown_value is None:
        return False
    shown = abs(shown_value)
    expected = abs(row["value"])
    if row["metric"] in _PERCENT_METRICS:
        return (abs(shown - expected) <= _rounding_tolerance(token)
                or abs(shown - expected * 100) <= _rounding_tolerance(token))
    return abs(shown - expected) <= _rounding_tolerance(token)


def _matches_explicit_model_output(token: str, metric: str, text: str, projection: dict) -> bool:
    if not re.search(r"Pure|模型|基线|ML", text, re.I):
        return False
    shown_value = _number_value(token)
    if shown_value is None:
        return False
    shown = abs(shown_value)
    for fact in projection.get("facts", []):
        if fact.get("kind") != "MODEL_OUTPUT":
            continue
        value = fact.get("value") or {}
        numbers = value.get("numbers") or {}
        if metric in {"win_rate", "win_pct"} and "win_probability" in numbers:
            candidate = float(numbers["win_probability"]) * (1 if shown <= 1 else 100)
        elif metric == "point_margin_per_game":
            candidate = numbers.get("expected_margin", numbers.get("expected_home_margin"))
            if candidate is None:
                continue
            candidate = abs(float(candidate))
        else:
            continue
        if abs(shown - candidate) <= _rounding_tolerance(token):
            return True
    return False


def _matches_locked_output(token: str, metric: str, projection: dict) -> bool:
    shown_value = _number_value(token)
    if shown_value is None:
        return False
    shown = abs(shown_value)
    numbers, _fact_id = _locked_prediction_source(projection)
    numbers = numbers or {}
    if metric in {"win_rate", "win_pct"} and numbers.get("win_probability") is not None:
        value = float(numbers["win_probability"])
        return abs(shown-value) <= _rounding_tolerance(token) or abs(shown-value*100) <= _rounding_tolerance(token)
    if metric == "point_margin_per_game":
        value = _winner_positive_margin(numbers)
        return value is not None and value >= 0 and abs(shown-value) <= _rounding_tolerance(token)
    if metric == "expected_total" and numbers.get("expected_total") is not None:
        return abs(shown-float(numbers["expected_total"])) <= _rounding_tolerance(token)
    return False


def _mentioned_side(text: str, game: dict) -> str | None:
    hits = []
    for side in ("home", "away"):
        team = game.get(side) or {}
        aliases = [str(team.get("name_zh") or ""), str(team.get("name") or ""),
                   str(team.get("abbreviation") or "")]
        subject_mention = False
        for alias in filter(None, aliases):
            for mention in re.finditer(_flexible_alias_pattern(alias), text, re.I):
                prefix = text[max(0, mention.start()-12):mention.start()]
                if not re.search(r"(?:对阵|对|against|vs\.?)[\s]*$", prefix, re.I):
                    subject_mention = True
        if subject_mention:
            hits.append(side)
    return hits[0] if len(set(hits)) == 1 else None


def _nearest_metric_for_token(text: str, token, labels) -> str | None:
    nearest = []
    for start, end, metric in labels:
        if token.start() <= end and token.end() >= start:
            distance = 0
        elif token.end() <= start:
            gap = text[token.end():start]
            if re.search(r"[,，;；、。！？]", gap):
                continue
            distance = len(gap)
        else:
            gap = text[end:token.start()]
            if re.search(r"[,，;；、。！？]", gap):
                continue
            distance = len(gap)
        if distance <= 14:
            # Association needs statistical syntax, not just proximity. In
            # "23分钟20分15篮板" the other units/values cannot belong to rebounds;
            # "篮板端的另一半" is prose. Retain paired comparison notation.
            if distance and not (_NUMBER_RE.search(gap) and re.search(r"对(?!手|方|位|抗)|比(?!赛|较)|\bvs\b|versus", gap, re.I)) and not re.fullmatch(
                    r"(?:\s|[:：=()（）]|每百回合|每场|场均|数值|数字|数|为|是|达到|达|到|约|只有|仅|高达|低至|接近|超过|不到|大约|左右|个|次|记|上升至|下降至|降至|升至|低于|高于|少于|多于)*", gap):
                continue
            after_value = start >= token.end()
            nearest.append((distance, 0 if after_value else 1, metric))
    metric = min(nearest, key=lambda item: (item[0], item[1]))[2] if nearest else None
    if (metric in {"rebounds_per_game", "assists_per_game", "minutes_per_game"}
            and re.match(r"\s*分(?!钟|之)", text[token.end():])
            and not re.search(r"分差|总分|净胜|净负|净输|赢|输|让|受让", text[max(0,token.start()-16):token.start()])):
        return "points_for_per_game"
    return metric


def _h2h_comparison_side(text: str, token, metric: str, labels, projection: dict,
                         sample_spans: list[tuple[int, int]]) -> str | None:
    """Assign paired H2H values to teams by their comparison order."""
    if not re.search(r"系列赛|季后赛|交手|对阵|head.to.head|series", text, re.I):
        return None
    h2h_rows = [row for row in _metric_rows(projection) if row.get("sample") == "h2h_series"]
    if not any(row.get("metric") == metric for row in h2h_rows):
        return None
    if {row.get("side") for row in h2h_rows} != {"home", "away"}:
        return None
    mentions = sorted(_team_name_spans(text, projection.get("game") or {}), key=lambda item: item[0])
    team_order = list(dict.fromkeys(side for _start, _end, side in mentions))
    if len(team_order) == 1:
        team_order.append("away" if team_order[0] == "home" else "home")
    if len(team_order) < 2:
        return None

    def side_before(position):
        prior = [mention for mention in mentions if mention[1] <= position]
        return prior[-1][2] if prior else None

    comparison_order = None
    assigned = {}
    h2h_metrics = {row["metric"] for row in h2h_rows}
    numeric = [item for item in _NUMBER_RE.finditer(text)
               if not any(a <= item.start() and item.end() <= b for a, b, _side in mentions)
               and not any(a <= item.start() < b for a, b in sample_spans)]
    metric_tokens = [(item, _nearest_metric_for_token(text, item, labels)) for item in numeric]
    for (left, left_metric), (right, right_metric) in zip(metric_tokens, metric_tokens[1:]):
        if left_metric not in h2h_metrics or right_metric != left_metric:
            continue
        gap = text[left.end():right.start()]
        if not re.search(r"(?:对|比|vs\.?|versus)", gap, re.I) or len(gap) > 40:
            continue
        explicit_left, explicit_right = side_before(left.start()), side_before(right.start())
        if explicit_left and explicit_right and explicit_left != explicit_right:
            pair_order = [explicit_left, explicit_right]
            if comparison_order is None:
                comparison_order = pair_order
        elif explicit_left and comparison_order is None and re.search(r"对|vs\.?|versus", gap, re.I):
            pair_order = [explicit_left, "away" if explicit_left == "home" else "home"]
            if comparison_order is None:
                comparison_order = pair_order
        else:
            pair_order = comparison_order or team_order[:2]
        assigned[(left.start(), left.end())] = pair_order[0]
        assigned[(right.start(), right.end())] = pair_order[1]
    return assigned.get((token.start(), token.end()))


def _single_preseason_stat_issues(text, projection, unit_id, player_name=None):
    # This is source-bound event verification, never a season-average fallback.
    if not re.search(r"季前赛|preseason", text, re.I) or re.search(r"场均|per.game|averag", text, re.I):
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    units = {"points": "points", "point": "points", "分": "points", "篮板": "rebounds",
             "rebounds": "rebounds", "rebound": "rebounds", "助攻": "assists", "assists": "assists",
             "assist": "assists", "分钟": "minutes", "minutes": "minutes", "minute": "minutes"}
    pattern = re.compile(r"(?<![A-Za-z0-9.])(-?\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
                         r"(minutes?|分钟|points?|分(?!钟)|rebounds?|篮板|assists?|助攻)(?![A-Za-z])", re.I)
    def values(value):
        result = {}
        for match in pattern.finditer(value):
            raw, unit = match.group(1).lower(), match.group(2).lower()
            result.setdefault(units[unit], set()).add(float(words[raw]) if raw in words else float(raw))
        return result
    requested = values(text)
    if not requested:
        return None
    roster = {}
    for fact in projection.get("facts", []):
        if fact.get("kind") == "CURRENT":
            v = fact.get("value") or {}
            for player in v.get("roster_snapshot") or []:
                name = str(player.get("name") or player.get("player_name") or "").strip()
                if name:
                    roster[name] = v.get("team_id")
    surnames = {}
    for name in roster:
        surname = name.split()[-1]
        if len(surname) >= 5:
            surnames.setdefault(surname.casefold(), []).append(name)
    def actors(value):
        found = {name for name in roster if name.casefold() in value.casefold()}
        for surname, names in surnames.items():
            if len(names) == 1 and re.search(r"(?<![A-Za-z])" + re.escape(surname) + r"(?![A-Za-z])", value, re.I):
                found.add(names[0])
        return found
    explicit = {player_name} if player_name else actors(text)
    supporting = []
    for fact in projection.get("facts", []):
        v = fact.get("value") or {}
        quote = str(v.get("quote") or "")
        if (fact.get("kind") not in {"CURRENT", "BACKGROUND", "LIVE"} or not v.get("claim_type")
                or v.get("source_role") not in {"primary", "professional"}
                or not re.search(r"preseason|季前赛", quote, re.I) or re.search(r"场均|per.game|averag", quote, re.I)):
            continue
        owners = actors(quote)
        if len(owners) != 1 or roster[next(iter(owners))] != v.get("team_id"):
            continue
        if explicit and owners != explicit:
            continue
        # An unnamed subject requires a multi-metric signature and a unique source actor.
        if not explicit and len(requested) < 2:
            continue
        observed = values(quote)
        if all(metric in observed and numbers <= observed[metric] for metric, numbers in requested.items()):
            supporting.append((fact["id"], next(iter(owners))))
    spans = [(match.start(1), match.end(1)) for match in pattern.finditer(text)]
    if supporting and len({owner for _, owner in supporting}) == 1:
        return {"findings": [], "spans": spans}
    return {"findings": [{"unit_id": unit_id, "code": "exact_stat_value_unverified", "quote": text,
             "statistic_scope": "reported_single_preseason_game", "stated_statistics": {k: sorted(v) for k, v in requested.items()},
             "available_fact_ids": [fact_id for fact_id, _ in supporting]}], "spans": spans}


def _stat_issues_for_text(text: str, projection: dict, unit_id: str, *, player_name=None) -> list[dict]:
    event = _single_preseason_stat_issues(text, projection, unit_id, player_name) or {"findings": [], "spans": []}
    rows = _metric_rows(projection)
    game = projection.get("game") or {}
    team_mentions = _team_name_spans(text, game)
    name_spans = [(start, end) for start, end, _side in team_mentions]
    both_teams = {side for _start, _end, side in team_mentions} >= {"home", "away"}
    team_aliases = [str((game.get(side) or {}).get(key) or "") for side in ("home", "away")
                    for key in ("name_zh", "name", "abbreviation")]
    series_context = bool(re.search(r"系列赛|series", text, re.I) or
                          ("季后赛" in text and (both_teams or
                            any(alias and re.search(r"对(?:阵)?\s*" + _flexible_alias_pattern(alias), text, re.I)
                                for alias in team_aliases))))
    rows = [row for row in rows if row.get("sample") != "h2h_series" or series_context]
    side = _mentioned_side(text, game)
    side_rows = rows
    sample_matches = list(_SAMPLE_RE.finditer(text))
    samples = [(m.start(), m.end(), _chinese_integer(m.group("count"))) for m in sample_matches
               if _sample_dimension(text, m) == "requested_games"]
    samples += [(m.start(),m.end(),int(m.group(1))) for m in re.finditer(r"(?<![A-Za-z])rolling[_ ](\d{1,3})(?!\d)",text,re.I)]
    sample_spans = [(m.start(), m.end()) for m in sample_matches]
    sample_spans += [(m.start(),m.end()) for m in re.finditer(r"(?<![A-Za-z])rolling[_ ](\d{1,3})(?!\d)",text,re.I)]
    samples = [item for item in samples if item[2] is not None and (item[2] != 1 or
        re.search(r"(?:近|最近|过去|滚动|rolling|last\s*)$", text[max(0, item[0]-8):item[0]], re.I))]
    labels = []
    known_metrics = {row["metric"] for row in side_rows} | {"minutes_per_game"}
    for metric in known_metrics:
        for alias in _METRIC_ALIASES[metric]:
            start = 0
            while (pos := text.find(alias, start)) >= 0:
                labels.append((pos, pos + len(alias), metric))
                start = pos + len(alias)
    for percent_marker in re.finditer("百分之", text):
        labels.append((percent_marker.start(), percent_marker.end(), "win_rate"))
    # Common compact Chinese player-stat formatting places the value before
    # the unit ("18.1分 7.5篮板 5.9助攻"). Treat the unit as its own label.
    for token in _NUMBER_RE.finditer(text):
        tail = text[token.end():token.end()+5]
        unit_offset = len(tail) - len(tail.lstrip())
        tail = tail.lstrip()
        prefix = text[max(0, token.start()-14):token.start()]
        for metric, unit in (("points_for_per_game", "分"), ("rebounds_per_game", "篮板"),
                             ("assists_per_game", "助攻"), ("minutes_per_game", "分钟")):
            if metric == "points_for_per_game" and tail.startswith("分钟"):
                continue
            historical_compact = bool(re.search(r"上季|上赛季|去年|last season", text, re.I)
                and re.match(r"\s*分\s*\d+(?:\.\d+)?\s*(?:助攻|篮板)", text[token.end():]))
            if metric == "points_for_per_game" and (not (re.search(r"场均|得分|points", prefix, re.I) or historical_compact)
                                       or re.search(r"百分之|净胜|净负|净输|分差|总分|赢", prefix)):
                continue
            if metric == "minutes_per_game" and not re.search(r"场均|average|avg", prefix, re.I):
                continue
            if metric in known_metrics and tail.startswith(unit):
                start = token.end() + unit_offset
                labels.append((start, start+len(unit), metric))
    issues = list(event["findings"])
    seen = set()
    for token in _NUMBER_RE.finditer(text):
        if any(start <= token.start() and token.end() <= end for start, end in event["spans"]):
            continue
        # A product name is not a statistic, even next to a numeric metric.
        if any(start <= token.start() and token.end() <= end for start, end in name_spans):
            continue
        if text[max(0, token.start()-1):token.end()+1] == "第25秒":
            continue
        if any(a <= token.start() < b for a, b in sample_spans):
            continue
        if text[token.start():].startswith("百") and text[token.end():].startswith("分之"):
            continue
        token_text = token.group().lstrip("+-")
        if _number_value(token_text) is None:
            continue
        metric = _nearest_metric_for_token(text, token, labels)
        if not metric:
            continue
        # Market quotations are external prices, not another forecast/stat.
        # Only an explicitly market-labelled number can use the qualified quote.
        local_prefix=text[max(0,token.start()-10):token.start()]
        if metric=='expected_total' and re.search(r'市场|盘口|market',local_prefix,re.I):
            market_facts=[f for f in projection.get('facts',[])if f.get('kind')=='MARKET'
                and f.get('game_specific')and f.get('source_evidence_ids')and (f.get('value')or{}).get('source_url')]
            if market_facts:
                latest=max(market_facts,key=lambda f:str((f.get('value')or{}).get('observed_at')or(f.get('value')or{}).get('captured_at')or''))
                value=(latest.get('value')or{}).get('total')
                if isinstance(value,(int,float))and abs(_number_value(token.group())-value)<=_rounding_tolerance(token.group()):
                    continue
        claim_type = classify_numeric_claim_type(text, token.start(), token.end(), metric, projection)
        if claim_type in {"forecast_or_game_script_estimate", "conditional_or_hypothetical_number",
                          "structural_or_count_expression", "team_identity", "other_numeric"}:
            continue
        if claim_type == "locked_prediction_output":
            if _matches_explicit_model_output(token_text, metric, text, projection) or _matches_locked_output(
                    token_text, metric, projection):
                continue
            numbers, fact_id = _locked_prediction_source(projection)
            field = {"win_rate": "win_probability", "win_pct": "win_probability",
                     "point_margin_per_game": "expected_margin", "expected_total": "expected_total"}.get(metric, metric)
            issues.append({"unit_id": unit_id, "code": "locked_forecast_text_mismatch", "quote": text,
                "field": field, "stated_value": token_text,
                "locked_value": (numbers or {}).get(field),
                "fact_ids": [fact_id] if fact_id else []})
            continue
        if metric in _PERCENT_METRICS and re.match(r"\s*分(?!之)", text[token.end():]):
            # "58%胜率和3分优势": a point margin is not another win rate.
            continue
        # A team named later in a mixed-team paragraph must not own all earlier
        # statistics. Only use an explicit identity in the current/past clause.
        clause_start = max((m.end() for m in re.finditer(r"[。！？；;，,]", text[:token.start()])), default=0)
        token_side = _h2h_comparison_side(text, token, metric, labels, projection, sample_spans)
        if token_side is None:
            token_side = _mentioned_side(text[clause_start:token.end()], game)
        if token_side is None:
            token_side = _mentioned_side(text[:token.end()], game)
        relevant = [row for row in side_rows if row["metric"] == metric
                    and (not token_side or row["side"] in (None,token_side))]
        if not relevant:
            if metric == "minutes_per_game":
                issues.append({"unit_id": unit_id, "code": "exact_stat_value_unverified", "quote": text,
                    "metric": metric, "stated_value": token_text, "available_fact_ids": []})
            continue
        nearby_samples = [item for item in samples if abs(item[0]-token.start()) <= 32]
        sample = min(nearby_samples, key=lambda item: abs(item[0]-token.start())) if nearby_samples else None
        if sample:
            count = sample[2]
            scoped = [row for row in relevant if row["games"] == count]
            if not scoped and any(row["games"] is not None for row in relevant):
                issue = {"unit_id": unit_id, "code": "exact_stat_sample_mismatch", "quote": text,
                         "metric": metric, "stated_sample_games": count,
                         "available_fact_ids": sorted({row["fact_id"] for row in relevant})}
                if issue not in issues:
                    issues.append(issue)
                continue
            if scoped:
                relevant = scoped
        if (_matches_explicit_model_output(token_text, metric, text, projection)
                or _matches_locked_output(token_text, metric, projection)):
            continue
        verified = [row for row in relevant if not row.get("unverified_value")]
        def matches_observation(row):
            if row.get("sample") == "h2h_series" and metric == "point_margin_per_game":
                shown = _number_value(token.group())
                if shown is None:
                    return False
                if re.search(r"净负|净输", text[max(0, token.start()-8):token.start()]):
                    shown = -abs(shown)
                return abs(shown-row["value"]) <= _rounding_tolerance(token_text)
            return _matches_value(token_text, row)
        if any(matches_observation(row) for row in verified):
            historical = [row for row in verified if row["kind"] == "HISTORICAL"]
            current_label = bool(re.search(r"本季|本赛季|这赛季|当前赛季|current season", text, re.I))
            if historical and current_label and not any(row["kind"] != "HISTORICAL" for row in verified):
                issue = {"unit_id": unit_id, "code": "exact_stat_season_mismatch", "quote": text,
                         "metric": metric, "available_fact_ids": sorted({row["fact_id"] for row in historical})}
                if issue not in issues:
                    issues.append(issue)
            elif historical and not current_label and not re.search(
                    r"上季|上赛季|上个赛季|去年|20\d{2}\s*[-/～—]\s*\d{2,4}|previous season|last season", text, re.I):
                issue = {"unit_id": unit_id, "code": "exact_stat_season_unlabeled", "quote": text,
                         "metric": metric, "available_fact_ids": sorted({row["fact_id"] for row in historical})}
                if issue not in issues:
                    issues.append(issue)
            continue
        if any(row.get("unverified_value") for row in relevant):
            issue = {"unit_id": unit_id, "code": "exact_stat_value_unverified", "quote": text,
                     "metric": metric, "stated_value": token_text,
                     "available_fact_ids": sorted({row["fact_id"] for row in relevant})}
            if issue not in issues:
                issues.append(issue)
            continue
        issue = {"unit_id": unit_id, "code": "exact_stat_value_mismatch", "quote": text,
                 "metric": metric, "stated_value": token_text,
                 "available_fact_ids": sorted({row["fact_id"] for row in relevant})}
        if issue not in issues:
            issues.append(issue)
    return issues


def _venue_issues_for_text(text: str, projection: dict, unit_id: str) -> list[dict]:
    import re
    game = projection.get("game") or {}
    venue = game.get("venue") or {}
    # Registry/catalog facts may contain the arena name alone. It is a name
    # fact, not a source for an inferred city; legacy structured venues retain
    # their explicitly captured location.
    if isinstance(venue, str):
        actual_name, actual_city = venue.strip(), ""
    elif isinstance(venue, dict):
        actual_name = str(venue.get("fullName") or "").strip()
        address = venue.get("address") or {}
        actual_city = str(address.get("city") or "").strip() if isinstance(address, dict) else ""
    else:
        actual_name, actual_city = "", ""
    issues = []
    title_case_venues = re.findall(r"\b(?:[A-Z][A-Za-z0-9.'&+-]*\s+)+(?:Arena|Center|Centre|Fieldhouse)\b", text)
    for claimed in title_case_venues:
        if actual_name and claimed.casefold() != actual_name.casefold():
            issues.append({"unit_id": unit_id, "code": "specific_venue_mismatch", "quote": text,
                           "claimed_venue": claimed, "actual_venue": actual_name})
    # A missing venue location cannot prove that a named city is wrong. Do not
    # infer the arena city from the scheduled home team; neutral-site games are
    # a real case, and absent venue metadata is an unknown rather than a clash.
    if not actual_city:
        return issues
    # The home club can be called the home team while playing at a different
    # venue. Only an explicit location construction asserts a city.
    home = game.get("home") or {}
    team_name = str(home.get("name") or "")
    team_name_zh = str(home.get("name_zh") or "")
    city = str(home.get("city") or (team_name.split(" ")[0] if team_name else "")).strip()
    city_aliases = [city]
    city_aliases.extend(zh for en, zh in _CITY_TRANSLATIONS.items()
                        if en.casefold() == city.casefold() or en.casefold() == actual_city.casefold())
    if city.casefold() == "quebec city" or actual_city.casefold() == "quebec city":
        city_aliases.append("魁北克市")
    actual_aliases = {actual_city.casefold()}
    actual_aliases.update(zh.casefold() for en, zh in _CITY_TRANSLATIONS.items() if en.casefold() == actual_city.casefold())
    if actual_city.casefold() == "quebec city":
        actual_aliases.add("魁北克市")
    location_markers = r"在|位于|举行|举办|设在|移师|落户|作客|来到|比赛城市|located\s+in|in\s+$|at\s+$|hosts?\s+"
    for alias in filter(None, city_aliases):
        if alias.casefold() in actual_aliases:
            continue
        city_pattern = (re.compile(re.escape(alias), re.I) if re.search(r"[\u4e00-\u9fff]", alias)
                        else re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.I))
        for match in city_pattern.finditer(text):
            prefix = text[max(0, match.start()-22):match.start()]
            suffix = text[match.end():match.end()+24]
            locative_prefix = bool(re.search(location_markers, prefix, re.I))
            negated_location = bool(re.search(r"不是在|并非在|不在|not\s+in|isn't\s+in", prefix, re.I))
            team_label = not locative_prefix and ((team_name and suffix.lstrip().startswith(team_name.removeprefix(alias).strip()))
                          or (team_name_zh and suffix.lstrip().startswith(team_name_zh)))
            explicit_location = (locative_prefix
                                 or bool(re.search(r"(举行|举办|球馆|场馆|主场|arena|center|centre|hosts?)", suffix, re.I)))
            if explicit_location and not team_label and not negated_location:
                issues.append({"unit_id": unit_id, "code": "specific_city_mismatch", "quote": text,
                               "claimed_city": alias, "actual_city": actual_city})
                break
    return issues


def _known_people(projection):
    facts = projection.get("facts", [])
    game = projection.get("game") or {}
    names = set()
    team_names = set()
    team_tokens = set()
    for side in ("home", "away"):
        team = game.get(side) or {}
        aliases = [str(team.get(key) or "") for key in ("name", "name_zh", "abbreviation")]
        team_names.update(aliases)
        team_tokens.update(part.casefold() for name in aliases for part in name.split() if part)
    for fact in facts:
        value = fact.get("value") or {}
        if fact.get("kind") == "CURRENT":
            names.update(str(item.get("name") or item.get("player_name") or "")
                         for item in value.get("roster_snapshot") or [])
            names.update(str(item.get("name") or item.get("player_name") or item)
                         for item in value.get("confirmed_starters") or [])
        elif fact.get("kind") == "HISTORICAL":
            names.update(str(item.get("player") or item.get("player_name") or "")
                         for item in value.get("players") or [])
        elif fact.get("kind") in {"LIVE", "PROJECTED", "TRANSACTION", "SOURCE_OPINION"}:
            names.add(str(value.get("player_name") or ""))
        quote = str(value.get("quote") or value.get("title") or "")
        names.update(re.findall(r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,2}", quote))
    people = {name for name in names if name and name not in team_names
              and name.casefold() not in {"the heat", "the raptors", "the knicks", "the lakers", "the celtics"}}
    aliases = set()
    for name in people:
        parts = name.split()
        if len(parts) > 1:
            aliases.update(part for part in parts if len(part) >= 6 and part.casefold() not in team_tokens)
    return people | aliases


def _normalized_source_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _coach_quote_issues_for_text(text: str, projection: dict, unit_id: str) -> list[dict]:
    """A direct coach quote must appear in frozen, quote-bound coach-comment evidence."""
    issues = []
    # visible_units splits at sentence punctuation, which may sit inside a
    # Chinese closing quotation mark; allow the unit boundary to close it.
    quote_pattern = re.compile(r"[\u201c\"]([^\u201d\"]{1,500})(?:[\u201d\"]|$)")
    facts = projection.get("facts", [])
    for match in quote_pattern.finditer(text):
        prefix = text[max(0, match.start()-60):match.start()]
        suffix = text[match.end():match.end()+45]
        neighborhood = prefix + suffix
        coach = bool(re.search(r"主帅|主教练|教练(?:员|组)?|head\s+coach|coach", neighborhood, re.I))
        speech = bool(re.search(r"说|表示|称|说道|坦言|透露|said|says|told|stated|noted", neighborhood, re.I))
        if not (coach and speech):
            continue
        quoted = _normalized_source_text(match.group(1))
        supported = any(
            str((fact.get("value") or {}).get("claim_type") or "").casefold() == "coach_comments"
            and quoted in _normalized_source_text((fact.get("value") or {}).get("quote"))
            for fact in facts if fact.get("kind") in {"CURRENT", "LIVE", "BACKGROUND", "SOURCE_OPINION"})
        if not supported:
            issues.append({"unit_id": unit_id, "code": "unverified_coach_quote", "quote": text,
                           "quoted_text": match.group(1)})
    return issues


def _minute_values(text: str) -> set[float]:
    values = set()
    pattern = re.compile(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*(?:[-–~～至]\s*\d{1,2}(?:\.\d+)?\s*)?(?:分钟|[- ]?minutes?|mins?)", re.I)
    for match in pattern.finditer(text):
        span = text[match.start():match.end()]
        values.update(float(value) for value in re.findall(r"\d{1,2}(?:\.\d+)?", span))
    return values


def _mentioned_people(text,projection):
    known=_known_people(projection)
    names=[name for name in known if re.search(r'(?<![A-Za-z])'+re.escape(name)+r'(?![A-Za-z])',text,re.I)]
    aliases={}
    for row in projection.get('player_display_names')or[]:
        canonical=row.get('canonical_name_en');zh=row.get('display_name_zh')
        if canonical not in known or not zh:continue
        for alias in {zh,zh.split('·')[-1]}:
            if len(alias)>=2:aliases.setdefault(alias,set()).add(canonical)
    for alias,owners in aliases.items():
        if len(owners)==1 and alias in text:names.append(next(iter(owners)))
    return list(dict.fromkeys(names))


def _supported_long_term_background(text, projection):
    if not re.search(r"背景|此前|休赛期|恢复期|background|offseason|previously", text, re.I):
        return False
    if not re.search(r"长期|数月|数周|long.term|months|weeks", text, re.I):
        return False
    if re.search(r"本场|今晚|今天|已经确认|已确认|官方宣布|确认缺阵|不会出场|首发|分钟|限时|this game|tonight|confirmed|minutes", text, re.I):
        return False
    named = {name for name in _known_people(projection) if " " in name and name.casefold() in text.casefold()}
    for row in projection.get("player_display_names") or []:
        canonical, zh = row.get("canonical_name_en"), row.get("display_name_zh")
        if canonical in _known_people(projection) and zh and zh in text:
            named.add(canonical)
    if not named:
        return False
    for name in named:
        supported = any(fact.get("kind") == "BACKGROUND"
            and (fact.get("value") or {}).get("claim_type") == "long_term_injury"
            and (fact.get("value") or {}).get("source_role") in {"primary", "professional"}
            and name.casefold() in str((fact.get("value") or {}).get("quote") or "").casefold()
            and re.search(r"长期|数月|数周|sideline.{0,50}(?:months|weeks)|out.{0,30}(?:months|weeks)",
                str((fact.get("value") or {}).get("quote") or ""), re.I)
            for fact in projection.get("facts", []))
        if not supported:
            return False
    return True


def _availability_issues_for_text(text: str, projection: dict, unit_id: str,
                                  player_name=None, *, _subjects=None, _scoped=False) -> list[dict]:
    from .availability_text import claim_scopes, supported_team_absence
    if not _scoped:
        issues = []
        for clause, subjects, has_status in claim_scopes(text, projection, player_name):
            if not has_status and not re.search(r'核心齐整|全员健康|完整核心|全员可用|全队健康|全员出战|全部正常出战', clause):
                continue
            issues.extend(_availability_issues_for_text(clause, projection, unit_id,
                _subjects=subjects, _scoped=True))
        return issues
    clauses = [text]
    status_clauses = [text]
    # Match assertions, not words inside a negated/unknown-status disclosure.
    # Mask only that clause; an actual assertion later in the unit still counts.
    text_for_claims = re.sub(
        r"(?:未|尚未|没有|并未)(?:正式)?确认[^。！？；，,]{0,12}(?:缺阵|出战)"
        r"|(?:缺阵|出战)(?:的)?(?:消息|状态)?(?:尚未|还未|未获|未被)(?:正式)?确认"
        r"|(?:没有|并无|尚无|暂无|未有|均无)(?:可靠)?(?:分钟限制|首发|出战)(?:来源|信息|消息)"
        r"|(?:没有|未见|暂无|均无|尚无)(?:明确的?|可靠的?)?缺阵(?:报告|消息|记录|通知)"
        r"|(?:没有|并无|尚无|暂无|未有|均无)(?:可靠)?(?:已)?确认(?:的)?(?:分钟限制|首发|出战(?:信息|名单)?)"
        r"|(?:未确认|尚未确认|尚未公布|not confirmed|unconfirmed)[^。！？；，,;.!?]{0,14}(?:分钟限制|限时出场|首发|minutes? restriction)",
        lambda m:" "*len(m.group()), text, flags=re.I)
    issues = []
    facts = projection.get("facts", [])
    names = list(_subjects or [])
    speculative = bool(re.search(r"如果|假如|若|我预计|预计|可能|或许|我认为|if\b|might\b|could\b", text, re.I))
    full_strength=re.search(r'核心齐整|全员健康|完整核心|全员可用|全队健康|全员出战|全部正常出战',text)
    if full_strength and (projection.get('limits')or{}).get('unconfirmed_player_availability')and not speculative:
        prefix=text[max(0,full_strength.start()-12):full_strength.start()]
        if not re.search(r'并非|不是|未能确认|不意味着|不能说|不能据此|尚未确认',prefix):
            issues.append({'unit_id':unit_id,'code':'current_availability_unverified','quote':text,
                'reason':'unknown_availability_cannot_be_full_strength'})
    minutes_claim = re.search(r"限时\s*\d{1,2}\s*分钟|限制(?:在)?\s*\d{1,2}\s*分钟|受限.{0,8}\d{1,2}\s*分钟|(?:有|存在|受到|处于).{0,5}(?:分钟限制|限时出场)|(?:分钟限制|限时出场).{0,5}(?:已确认|确有|正式宣布)|(?:has|is on|was placed on|has been placed on).{0,12}(?:a )?minutes? restriction|\d{1,2}[- ]minute restriction|limited to\s*\d{1,2}\s*minutes?|capped at\s*\d{1,2}\s*minutes?", text_for_claims, re.I)
    unconfirmed_minutes = re.search(r"(?:未确认|尚未公布|尚无|暂无|未知|不确定|没有可靠信息|unconfirmed|not confirmed|unknown|unavailable).{0,14}(?:分钟限制|限时出场|minutes? restriction)|(?:分钟限制|限时出场|minutes? restriction).{0,14}(?:未确认|尚未公布|尚无|暂无|未知|不确定|unconfirmed|not confirmed|unknown|unavailable)", text, re.I)
    if unconfirmed_minutes and minutes_claim and unconfirmed_minutes.start() <= minutes_claim.start() < unconfirmed_minutes.end():
        minutes_claim = None
    status_claim = re.search(r"(?:已确认|官方宣布|正式确认).{0,10}(?:缺阵|缺席|出战|上场|首发|\bout\b|\bavailable\b)|已获准|获准|可以出战|确认出战|可打|cleared to play|can play|不能出战|不可以出战|不可出战|缺阵|无法出战|不会出场|出战成疑|\bquestionable\b|\bdoubtful\b|\bprobable\b|\bavailable\b|ruled out|\binactive\b|\bdnp\b|\bout\b", text_for_claims, re.I) or minutes_claim
    if status_claim and not minutes_claim and _supported_long_term_background(text, projection):
        status_claim = None
    if minutes_claim and not speculative and not names:
        issues.append({"unit_id": unit_id, "code": "current_minutes_restriction_unverified", "quote": text})
    unbound_confirmation=any(re.match(r'^(?:已经确认|已确认|官方宣布|正式确认)',part.strip())
        for part in status_clauses)
    aggregate_supported = bool(status_claim and re.search(r'缺阵|\bout\b|inactive', text_for_claims, re.I)
        and supported_team_absence(text, projection, _mentioned_side(text, projection.get('game') or {})))
    if status_claim and not names and not aggregate_supported and (not speculative or unbound_confirmation):
        issues.append({"unit_id": unit_id, "code": "current_availability_unverified", "quote": text})
    for player in names:
        matching = [fact for fact in facts if fact.get("kind") == "LIVE" and fact.get("game_specific")
                    and str((fact.get("value") or {}).get("player_name") or "").strip().casefold() == player.strip().casefold()]
        availability_matching = [fact for fact in matching
            if (fact.get("value") or {}).get("status")
            or (fact.get("value") or {}).get("claim_type") == "availability"]
        if status_claim and not minutes_claim and not speculative and not availability_matching:
            issues.append({"unit_id": unit_id, "code": "current_availability_unverified",
                           "quote": text, "player": player})
        if minutes_claim and not speculative:
            minute_facts = [fact for fact in matching
                            if str((fact.get("value") or {}).get("claim_type") or "").casefold() == "minutes_restriction"]
            if not minute_facts:
                issues.append({"unit_id": unit_id, "code": "current_minutes_restriction_unverified",
                               "quote": text, "player": player})
            else:
                claimed_minutes = _minute_values(text)
                sourced_minutes = set()
                for fact in minute_facts:
                    sourced_minutes.update(_minute_values(str((fact.get("value") or {}).get("quote") or "")))
                if claimed_minutes and (not sourced_minutes or not claimed_minutes.issubset(sourced_minutes)):
                    issues.append({"unit_id": unit_id, "code": "confirmed_minutes_restriction_conflict",
                                   "quote": text, "player": player,
                                   "claimed_minutes": sorted(claimed_minutes),
                                   "source_minutes": sorted(sourced_minutes)})
        for fact in availability_matching:
            if re.search(r"如果|假如|若|\bif\b", text, re.I):
                continue
            value = fact.get("value") or {}
            status = str(value.get("status") or "").casefold()
            if not status and str(value.get("claim_type") or "").casefold() == "availability":
                quote = str(value.get("quote") or "").casefold()
                if re.search(r"(?:is|listed as) available|cleared to play|can play", quote):
                    status = "available"
                elif re.search(r"(?:is|listed as) out|ruled out|will miss", quote):
                    status = "out"
                elif "questionable" in quote:
                    status = "questionable"
            if not status and not status_claim:
                continue
            claims_out = bool(status_claim and re.search(r"缺阵|无法出战|不会出场|ruled out|\binactive\b|\bdnp\b|\bout\b", text_for_claims, re.I))
            claims_available = bool(status_claim and re.search(r"获准|可以出战|确认出战|可打|\bavailable\b|cleared to play|can play", text_for_claims, re.I))
            claims_questionable = bool(status_claim and re.search(r"出战成疑|\bquestionable\b|\bdoubtful\b|\bprobable\b", text_for_claims, re.I))
            negative_out = bool(re.search(r"(?:不会|不是|并非|没有|并未)(?:已经|已确认)?缺阵|\bnot\s+(?:ruled\s+)?out\b", text_for_claims, re.I))
            negative_available = bool(re.search(r"不能出战|不可以出战|不可出战|\bnot\s+(?:available|cleared to play)\b", text_for_claims, re.I))
            claims_out = (claims_out and not negative_out) or bool(status_claim and negative_available)
            claims_available = (claims_available and not negative_available) or bool(status_claim and negative_out)
            out_status = {"out", "inactive", "suspended", "dnp"}
            if ((claims_out and status not in out_status)
                    or (claims_available and status not in {"available", "active"})
                    or (claims_questionable and status not in {"questionable", "doubtful", "probable"})):
                issues.append({"unit_id": unit_id, "code": "confirmed_availability_source_conflict",
                               "quote": text, "fact_ids": [fact["id"]], "player": player, "status": status})
            if re.search(r"确认.{0,8}首发|正式.{0,8}首发|confirmed.{0,16}starter|starting lineup", text, re.I) and status in {
                    "out", "inactive", "suspended", "dnp"}:
                issues.append({"unit_id": unit_id, "code": "confirmed_status_starter_conflict",
                               "quote": text, "fact_ids": [fact["id"]], "player": player, "status": status})

    starter_word = re.search(r"首发|starter|starting lineup", text, re.I)
    confirm_words = list(re.finditer(r"已确认|已经确认|官方(?:前瞻|公告)?确认|确认|确定|正式公布|officially confirmed|confirmed", text, re.I))
    explicit_confirmed = bool(starter_word and any(
        abs(item.start() - starter_word.start()) <= 40
        and not re.search(r"(?:未|尚未|无法|不能|没有|并未|无|缺|缺少|not|never|unconfirmed)[ ]*$",
                          text[max(0, item.start()-12):item.start()], re.I)
        for item in confirm_words))
    if explicit_confirmed and names:
        game = projection.get("game") or {}
        side = _mentioned_side(text, game)
        starters = [fact for fact in facts if fact.get("kind") == "CURRENT"
                    and (fact.get("value") or {}).get("confirmed_starters")
                    and (not side or (fact.get("value") or {}).get("side") == side)]
        if not starters:
            unsourced = []
            for player in names:
                source = any(f.get("game_specific") and f.get("kind") in {"CURRENT", "LIVE"}
                    and "starter" in str((f.get("value") or {}).get("claim_type") or "").casefold()
                    and player.casefold() in json.dumps(f.get("value") or {}, ensure_ascii=False).casefold()
                    for f in facts)
                if not source:
                    unsourced.append(player)
            if unsourced:
                issues.append({"unit_id": unit_id, "code": "confirmed_starter_source_unverified",
                               "quote": text, "players": unsourced})
        else:
            confirmed = {str(item.get("name") or item.get("player_name") or item)
                         for fact in starters for item in (fact.get("value") or {}).get("confirmed_starters") or []}
            for player in names:
                if player not in confirmed:
                    issues.append({"unit_id": unit_id, "code": "confirmed_starter_source_conflict",
                                   "quote": text, "player": player, "fact_ids": [fact["id"] for fact in starters]})
    return issues


def _locked_forecast_issues(text: str, projection: dict, unit_id: str) -> list[dict]:
    marker = re.search(r"第25秒(?:最终|当前)?(?:预测|预计)|最终预测|我的(?:最终)?预测|最终判断|最终我(?:站|看好|认为|选)|我最终|我给|我选|第25秒看好", text)
    if not marker:
        return []
    numbers, fact_id = _locked_prediction_source(projection)
    if not numbers:
        return []
    fact_ids = [fact_id] if fact_id else []
    issues = []
    game = projection.get("game") or {}
    snippet = text[marker.start():]
    for side in ("home", "away"):
        if side == numbers.get("winner_side"):
            continue
        team = game.get(side) or {}
        aliases = [str(team.get("name_zh") or ""), str(team.get("name") or "")]
        for alias in filter(None, aliases):
            if re.search(re.escape(alias) + r".{0,12}(?:胜|赢|获胜|赢下)", snippet, re.I):
                issues.append({"unit_id": unit_id, "code": "locked_forecast_text_mismatch", "quote": text,
                               "field": "winner_side", "claimed_value": side,
                               "locked_value": numbers.get("winner_side"), "fact_ids": fact_ids})
                break
    winner = game.get(str(numbers.get("winner_side")) or "") or {}
    winner_aliases = [str(winner.get("name_zh") or ""), str(winner.get("name") or "")]
    for alias in filter(None, winner_aliases):
        loss = re.search(re.escape(alias) + r".{0,10}(?:净负|净输|输掉|落败|lose[s]? by|lost by)", snippet, re.I)
        if loss:
            prefix = snippet[max(0, loss.start()-8):loss.start()]
            if not re.search(r"不|没|未|not|never", prefix, re.I):
                issues.append({"unit_id": unit_id, "code": "locked_forecast_text_mismatch", "quote": text,
                    "field": "winner_side", "claimed_value": "lost", "locked_value": numbers.get("winner_side"),
                    "fact_ids": fact_ids})
    number = r"([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)"
    checks = (("win_probability", rf"(?:胜率|胜出概率|获胜概率)(?:百分之)?\s*[:：为约]?\s*{number}\s*[%％]?", 100),
              ("expected_margin", rf"(?:预计|预测|净胜|分差|赢(?!球)|获胜)[^。！？!?，,；;]{{0,14}}?{number}\s*分", 1),
              ("expected_total", rf"(?:预计|预测)?总分\s*[:：为约]?\s*{number}", 1))
    for field, pattern, scale in checks:
        if field == "expected_margin":
            expected_value = _winner_positive_margin(numbers)
            if expected_value is None:
                continue
        else:
            expected_value = numbers.get(field)
        if not isinstance(expected_value, (int, float)) or isinstance(expected_value, bool):
            continue
        expected = float(expected_value) * scale
        for match in re.finditer(pattern, text):
            claimed_value = _number_value(match.group(1))
            if claimed_value is None:
                continue
            claimed = claimed_value
            if abs(claimed - expected) > 0.011:
                issues.append({"unit_id": unit_id, "code": "locked_forecast_text_mismatch", "quote": text,
                               "field": field, "claimed_value": claimed, "locked_value": expected,
                               "fact_ids": fact_ids})
    return issues


def _model_attribution_issues(text: str, unit_id: str) -> list[dict]:
    # Catch only direct causal statements linking ML/Pure to a result. Merely
    # reporting a baseline number or disagreeing with it remains free to write.
    direct = re.search(r"(?:Pure|纯模型|机器学习模型|ML模型|模型).{0,24}(?:因为|由于|基于|看重|依据|因此|所以).{0,48}(?:预测|给出|给了|输出|判断|胜率|分差)|(?:因为|由于|基于).{0,32}(?:净效率|胜率|篮板|阵容|伤病).{0,32}(?:Pure模型|纯模型|机器学习模型|模型).{0,12}(?:预测|给出|给了)", text, re.I)
    if direct:
        return [{"unit_id": unit_id, "code": "unsupported_ml_attribution", "quote": text}]
    return []


def _transaction_issues_for_text(text: str, projection: dict, unit_id: str) -> list[dict]:
    if re.search(r"如果|假如|可能|或许|传闻|没有证据|未确认", text):
        return []
    verbs = ("被交易到", "交易至", "交易得到", "通过交易", "换来", "换走",
             "签下", "签约", "加盟", "引进", "离队", "裁掉", "traded", "trade", "signed", "acquired")
    if not any(verb in text for verb in verbs) and not re.search(r'用[^。！？；]{1,80}换',text):
        return []
    facts = projection.get("facts", [])
    names = set()
    game = projection.get("game") or {}
    team_aliases = {str((game.get(side) or {}).get(key) or "").casefold()
                    for side in ("home", "away") for key in ("name", "name_zh", "abbreviation")}
    names.update(name for name in re.findall(r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,2}", text)
                 if name.casefold() not in team_aliases)
    for fact in facts:
        value = fact.get("value") or {}
        if fact.get("kind") == "CURRENT":
            names.update(str(p.get("name") or p.get("player_name") or "") for p in value.get("roster_snapshot") or [])
        elif fact.get("kind") == "HISTORICAL":
            names.update(str(p.get("player") or p.get("player_name") or "") for p in value.get("players") or [])
        elif fact.get("kind") == "TRANSACTION":
            names.update(re.findall(r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,2}",
                                    str(value.get("quote") or value.get("title") or "")))
        elif fact.get("kind") == "BACKGROUND" and value.get("claim_type") in {"personnel_change", "transaction"}:
            names.update(re.findall(r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,2}",
                                    str(value.get("quote") or value.get("snippet") or value.get("title") or "")))
    for fact in facts:
        value = fact.get("value") or {}
        if fact.get("kind") == "TRANSACTION":
            quote = json.dumps(value, ensure_ascii=False).casefold()
            if any(name and name.casefold() in text.casefold() and name.casefold() in quote for name in names):
                return []
        elif fact.get("kind") == "BACKGROUND":
            # Personnel-change background may verify a transaction only when it
            # is an accepted, source-backed confirmed fact for the named team.
            if (value.get("claim_type") not in {"personnel_change", "transaction"}
                    or value.get("assertion_kind") != "confirmed_fact"
                    or value.get("temporal_scope") not in {"current_season_background", "current_pregame"}
                    or not fact.get("source_evidence_ids")):
                continue
            team_id = value.get("team_id")
            team_ids = {int((game.get(side) or {}).get("id") or 0): side for side in ("home", "away")}
            fact_side = team_ids.get(int(team_id)) if team_id is not None else None
            claimed_side = _mentioned_side(text, game)
            if fact_side and claimed_side and fact_side != claimed_side:
                continue
            quote = str(value.get("quote") or value.get("snippet") or value.get("title") or "").casefold()
            mentioned = [name for name in names if name and name.casefold() in text.casefold()]
            if not mentioned or not all(name.casefold() in quote for name in mentioned):
                continue
            locations = [quote.find(name.casefold()) for name in mentioned]
            if any(pos < 0 for pos in locations):
                continue
            relation_start = max(0, min(locations) - 40)
            relation_end = min(len(quote), max(locations) + max(len(name) for name in mentioned) + 40)
            relation = quote[relation_start:relation_end]
            if re.search(r"trad(?:e|ed|ing)|sign(?:ed|ing)?|acquir(?:e|ed|ing)|exchange|waiv(?:e|ed|ing)", relation):
                return []
    if names and any(name and name.casefold() in text.casefold() for name in names):
        return [{"unit_id": unit_id, "code": "transaction_claim_unverified", "quote": text}]
    return []


def _roster_claim_issues_for_text(text: str, projection: dict, unit_id: str) -> list[dict]:
    if not re.search(r"目前效力|当前效力|现效力|目前在.{0,8}打球|当前名单|现有阵容|阵容中|currently on|currently with|roster includes|in the rotation", text, re.I):
        return []
    game = projection.get("game") or {}
    mentioned_side = _mentioned_side(text, game)
    facts = projection.get("facts", [])
    names = set()
    roster_memberships = {}
    for fact in facts:
        value = fact.get("value") or {}
        if fact.get("kind") == "CURRENT":
            side = value.get("side")
            for player in value.get("roster_snapshot") or []:
                name = str(player.get("name") or player.get("player_name") or "")
                if name:
                    names.add(name)
                    roster_memberships[name] = side
        elif fact.get("kind") == "HISTORICAL":
            names.update(str(player.get("player") or player.get("player_name") or "")
                         for player in value.get("players") or [])
    issues = []
    for name in sorted(item for item in names if item and item in text):
        source_side = roster_memberships.get(name)
        if source_side and mentioned_side and source_side != mentioned_side:
            issues.append({"unit_id": unit_id, "code": "current_roster_team_mismatch",
                           "quote": text, "player": name, "claimed_side": mentioned_side,
                           "source_side": source_side})
            continue
        if source_side is None and not roster_memberships.get(name):
            issues.append({"unit_id": unit_id, "code": "current_roster_claim_unverified",
                           "quote": text, "player": name})
    return issues


def _rest_day_issues_for_text(text: str, projection: dict, unit_id: str, *,
                              article_context: bool = False) -> list[dict]:
    """Check affirmative team-rest numbers, using the containing article as scope."""
    game = projection.get("game") or {}
    team_names = []
    for side in ("home", "away"):
        team = game.get(side) or {}
        for key in ("name", "name_zh", "abbreviation"):
            value = str(team.get(key) or "").strip()
            if value and value not in team_names:
                team_names.append(value)
    clauses = []
    for match in re.finditer(
        r"休息(?:了|時間|时间)?\s*(?P<days>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*天|"
        r"多休息\s*(?P<more>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*天", text,
    ):
        start, end = match.span()
        number = match.group("days") or match.group("more")
        left = max((text.rfind(mark, 0, start) for mark in "。！？；;\n"), default=-1) + 1
        right_candidates = [text.find(mark, end) for mark in "。！？；;\n"]
        right_candidates = [pos for pos in right_candidates if pos >= 0]
        right = min(right_candidates) if right_candidates else len(text)
        clause = text[left:right]
        claim_prefix = text[left:start]
        neighborhood = text[max(left, start - 36):min(right, end + 12)]
        claim_suffix = text[end:right].lstrip(" ，,、")
        # Unknown/negated/conditional statements are not assertions. Player
        # workload and coach-originated speech remain under their existing
        # independent checks; this rule does not grant them quote eligibility.
        if re.search(r"无法确认|不能确认|未能确认|不能确定|不确定|尚未确认|尚不清楚|不清楚|无法判断|是否|"
                     r"cannot confirm|not confirmed|uncertain|unclear|whether|unknown", claim_prefix, re.I):
            continue
        if re.search(r"如果|假如|若|倘若|假设|若是|要是|假若|if\b|unless\b|assuming\b|would\b|could\b|might\b", claim_prefix, re.I):
            continue
        if re.match(r"(?:的话|时|则|就会|就可能|也许|可能|吗|么|would\b|could\b|might\b)", claim_suffix, re.I):
            continue
        if text[right:right + 1] in {"？", "?"}:
            continue
        # These are negative claims, not affirmative rest-day facts. Treat a
        # double negative (e.g. “并非没有休息”) as affirmative instead.
        negative_prefix = re.search(
            r"(?:并没有|没有|并未|未曾|不曾|没|未|不是|并非|并不)"
            r"[^。！？；;，,]{0,8}$",
            claim_prefix,
        )
        double_negative = re.search(
            r"(?:并非|不是)[^。！？；;，,]{0,3}(?:没有|未曾|不曾)[^。！？；;，,]{0,3}$",
            claim_prefix,
        )
        if negative_prefix and not double_negative:
            continue
        if re.search(r"(?:球员|球星|某名球员|某球员).{0,10}休息|休息.{0,10}(?:球员|球星)|"
                     r"让.{0,10}(?:某名球员|某球员|球员|球星)|"
                     r"(?:let|allow).{0,25}\b(?:a\s+)?player\b.{0,15}\brest\b", clause, re.I):
            continue
        if re.search(r"教练|主帅|主教练|head coach|coach", clause, re.I) and re.search(r"表示|称|说|said|says|told|stated", clause, re.I):
            continue
        subject = bool(re.search(r"双方|兩隊|两队|球队|球隊|本场球队|本場球隊", neighborhood))
        subject = subject or any(name in neighborhood for name in team_names)
        pregame_scope = bool(re.search(
            r"本场|这场比赛|本次比赛|赛前|比赛前|开赛前|开打前|今晚|before\s+(?:this\s+game|tonight|tip.?off)|"
            r"prior\s+to\s+(?:the\s+game|tip.?off)|pregame|pre.game", clause, re.I,
        ))
        comparative = bool(re.search(r"多休息|比.{0,12}休息|more\s+rest|rested\s+more", clause, re.I))
        number_value = _chinese_integer(number)
        if subject and (article_context or pregame_scope or comparative) and number_value is not None:
            clauses.append((int(number_value), clause))
    if not clauses:
        return []
    # The Phase 1 fact contract deliberately emits no numeric rest-day facts.
    # A future explicit source fact can be added here without broadening the matcher.
    return [{"unit_id": unit_id, "code": "exact_rest_days_unverified", "quote": text,
             "stated_rest_days": days} for days, _ in clauses]


def _historical_score_issues(text: str, projection: dict, unit_id: str) -> list[dict]:
    is_record = bool(re.search(r"战绩|胜负记录|record", text, re.I))
    if not is_record and not re.search(r"比分|交手|季后赛|系列赛|上一场|上次|最近一次|last meeting", text, re.I):
        return []
    score_pattern = r"(?<!\d)(\d{2,3})\s*[-–比:：]\s*(\d{2,3})(?!\d)"
    if is_record:
        expected = []
        for fact in projection.get("facts", []):
            value = fact.get("value") or {}
            metrics = value.get("metrics") or {}
            if isinstance(metrics.get("wins"), (int, float)) and isinstance(metrics.get("losses"), (int, float)):
                expected.append((int(metrics["wins"]), int(metrics["losses"]), fact["id"]))
        findings = []
        patterns = (score_pattern, r"(?<!\d)(\d{1,3})\s*胜\s*(\d{1,3})\s*负")
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                pair = (int(match.group(1)), int(match.group(2)))
                if not any(pair == values[:2] for values in expected):
                    findings.append({"unit_id": unit_id, "code": "exact_record_mismatch", "quote": text,
                                     "claimed_record": list(pair), "available_fact_ids": [row[2] for row in expected]})
        return findings
    pairs = {(int(row.get("home_score")), int(row.get("away_score")))
             for fact in projection.get("facts", []) if fact.get("kind") == "HISTORICAL"
             for row in ((fact.get("value") or {}).get("meetings") or [])
             if isinstance(row.get("home_score"), (int, float)) and isinstance(row.get("away_score"), (int, float))}
    for fact in projection.get("facts", []):
        if fact.get("kind")!="OBSERVATION" or not re.fullmatch(r"/season_form/teams/(home|away)/recent_games",fact.get("source_path")or""):
            continue
        for row in (fact.get("value")or{}).get("recent_games")or[]:
            if (row.get("captured_at") and (row.get("source_payload_sha256")or row.get("source_url"))
                    and isinstance(row.get("team_score"),(int,float)) and isinstance(row.get("opponent_score"),(int,float))):
                pairs.add((int(row["team_score"]),int(row["opponent_score"])))
    findings = []
    for match in re.finditer(score_pattern, text):
        pair = (int(match.group(1)), int(match.group(2)))
        if pair not in pairs and pair[::-1] not in pairs:
            findings.append({"unit_id": unit_id, "code": "historical_score_mismatch", "quote": text,
                             "claimed_score": list(pair)})
    return findings


def _season_scope_issues(text: str, projection: dict, unit_id: str) -> list[dict]:
    facts = projection.get("facts", [])
    issues = []
    observations = [fact.get("value") or {} for fact in facts if fact.get("kind") == "OBSERVATION"]
    global_zero = re.search(r"本赛季.{0,12}(?:一场|任何比赛).{0,8}(?:没打|没进行|未进行|没有)|(?:played|played in) zero games this season", text, re.I)
    incomplete_global_coverage = any(
        (row.get("local_window_composition") or {}).get("current_season_games_confirmed") is None
        or (row.get("local_window_composition") or {}).get("current_season_coverage") in {"not_verified", None}
        for row in observations
    )
    if global_zero and incomplete_global_coverage:
        issues.append({"unit_id": unit_id, "code": "unverified_global_season_count", "quote": text})

    opener = re.search(r"本场.{0,10}(?:揭幕战|首战)|(?:揭幕战|本赛季首战|季前赛首战).{0,12}(?:就是|是|开启|到来|本场)|opening night|season opener|first game of (?:the )?season", text, re.I)
    denied=re.search(r"误写.{0,18}(?:揭幕战|首战)|并非.{0,12}(?:揭幕战|首战)|不是.{0,12}(?:揭幕战|首战)",text)
    if opener and not denied and not re.search(r"可能|或许|潜在|值得观察.*是否|question mark|potential", text, re.I):
        verified = any(f.get("game_specific") and re.search(
            r"揭幕战|首战|opening night|season opener|first game of (?:the )?season",
            json.dumps(f.get("value") or {}, ensure_ascii=False), re.I) for f in facts)
        if not verified:
            issues.append({"unit_id": unit_id, "code": "unsupported_season_opener_claim", "quote": text})

    training = re.search(
        r"(?:Pure|ML|模型|model).{0,24}(?:(?:训练|training).{0,24}(?:\d+|十|百).{0,8}(?:场|games?)|(?:只用|仅用|基于).{0,18}(?:近)?(?:\d+|十|百).{0,8}(?:场|games?).{0,12}(?:训练|training))",
        text, re.I)
    training = training or re.search(
        r"(?:Pure|ML|\u6a21\u578b).{0,24}(?:\u53ea\u7528|\u4ec5\u7528|\u57fa\u4e8e).{0,18}(?:\u8fd1)?(\d+|\u5341|\u767e).{0,8}(?:\u573a|games?).{0,12}(?:\u8bad\u7ec3|training)",
        text, re.I)
    training = training or re.search(
        r"(?:Pure|ML|模型).{0,24}(?:只用|仅用|基于).{0,18}(?:近)?([0-9]+|十|百).{0,8}(?:场|games?).{0,12}(?:训练)",
        text, re.I)
    if training:
        has_count = any((f.get("kind") == "MODEL_IDENTITY" and any(
            (f.get("value") or {}).get(key) is not None
            for key in ("training_games", "training_samples", "training_sample_game_count")))
            for f in facts)
        if not has_count:
            issues.append({"unit_id": unit_id, "code": "unsupported_model_training_sample", "quote": text})

    h2h = next(((f.get("value") or {}) for f in facts
                if f.get("source_path") == "/matchup_history"), None)
    if h2h:
        details = int(h2h.get("provided_meeting_rows") or 0)
        detail_claim = re.search(r"(?:最近|近|过去|provided|提供了)\s*(\d+)\s*(?:次|场)?\s*(?:交锋|对战|meeting).{0,20}(?:逐场|明细|比分明细|game by game)", text, re.I)
        detail_claim = detail_claim or re.search(
            r"(?:\u6700\u8fd1|\u8fd1|\u8fc7\u53bb|provided|\u63d0\u4f9b\u4e86)\s*(\d+)\s*(?:\u6b21|\u573a)?\s*(?:\u4ea4\u950b|\u5bf9\u6218|meeting).{0,20}(?:\u9010\u573a|\u660e\u7ec6|\u6bd4\u5206\u660e\u7ec6|game by game)",
            text, re.I)
        detail_claim = detail_claim or re.search(
            r"(?:最近|近|过去|provided|提供了)[ ]*([0-9]+)[ ]*(?:次|场)?[ ]*(?:交锋|对战|meeting).{0,20}(?:逐场|明细|比分明细|game by game)",
            text, re.I)
        if detail_claim and int(detail_claim.group(1)) != details:
            issues.append({"unit_id": unit_id, "code": "h2h_detail_count_mismatch", "quote": text,
                           "stated_rows": int(detail_claim.group(1)), "available_rows": details})
    return issues


def _scheduled_date_issues(text: str, projection: dict, unit_id: str) -> list[dict]:
    if not re.search(r"开打|开赛|本场比赛|比赛时间|scheduled|tipoff|game starts", text, re.I):
        return []
    fact = next((f for f in projection.get("facts", []) if f.get("source_path") == "/game/scheduled_tipoff"), None)
    if not fact:
        return []
    expected = str((fact.get("value") or {}).get("tipoff_beijing") or "")
    match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    match = match or re.search(r"(\d{1,2})\s*\u6708\s*(\d{1,2})\s*\u65e5", text)
    match = match or re.search(r"([0-9]{1,2})[ ]*月[ ]*([0-9]{1,2})[ ]*日", text)
    if match and expected:
        expected_match = re.match(r"\d{4}-(\d{2})-(\d{2})", expected)
        expected_match = expected_match or re.match(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", expected)
        if expected_match and (int(match.group(1)), int(match.group(2))) != (
                int(expected_match.group(1)), int(expected_match.group(2))):
            return [{"unit_id": unit_id, "code": "game_date_timezone_mismatch", "quote": text,
                     "claimed_month_day": [int(match.group(1)), int(match.group(2))], "beijing_tipoff": expected}]
    return []


def _debut_and_reunion_issues(text: str, projection: dict, unit_id: str) -> list[dict]:
    conditional = re.search(r"如果.{0,30}(?:确实|真|真的是|是).{0,16}(?:首秀|首战)|if this (?:is|were) (?:his|her|their )?debut", text, re.I)
    explicitly_unknown = re.search(r"尚不能确认|目前不能确认|未确认|无法确认|not confirmed|cannot confirm", text, re.I)
    if conditional and explicitly_unknown:
        return []
    claim = re.search(r"首秀|加盟.{0,12}首场|复出首战|对阵老东家|面对旧主|重返.{0,8}首战|debut|reunion with (?:his|her|their) former team", text, re.I)
    if not claim:
        return []
    facts = projection.get("facts", [])
    supported = any(f.get("game_specific") and
        re.search(r"首秀|加盟.{0,12}首场|复出首战|对阵老东家|面对旧主|重返.{0,8}首战|debut|reunion with (?:his|her|their) former team",
                  json.dumps(f.get("value") or {}, ensure_ascii=False), re.I) for f in facts)
    if supported:
        return []
    return [{"unit_id": unit_id, "code": "unsupported_debut_claim", "quote": text}]


def validate_publication_hard_facts(judgment: dict, projection: dict) -> list[dict]:
    """Return only explicit, mechanically checkable factual conflicts."""
    from .editorial_contract import visible_units
    issues = []
    for unit in visible_units(judgment):
        text, unit_id = unit["text"], unit["id"]
        player_name = None
        player_match = re.fullmatch(r"player_analysis\.(\d+)\.statement", unit_id)
        if player_match:
            players = (judgment.get("player_analysis") or [])
            index = int(player_match.group(1))
            if index < len(players):
                player_name = players[index].get("player_name")
        issues.extend(_player_appearance_issues_for_text(text, projection, unit_id, player_name))
        issues.extend(_sample_count_issues_for_text(text, projection, unit_id))
        issues.extend(_stat_issues_for_text(text, projection, unit_id, player_name=player_name))
        issues.extend(_venue_issues_for_text(text, projection, unit_id))
        issues.extend(_coach_quote_issues_for_text(text, projection, unit_id))
        from .player_identity import identity_issues
        issues.extend(identity_issues(text, projection, unit_id, player_name))
        issues.extend(_availability_issues_for_text(text, projection, unit_id, player_name))
        issues.extend(_transaction_issues_for_text(text, projection, unit_id))
        issues.extend(_roster_claim_issues_for_text(text, projection, unit_id))
        issues.extend(_rest_day_issues_for_text(
            text, projection, unit_id, article_context=unit_id.startswith("article.")))
        issues.extend(_historical_score_issues(text, projection, unit_id))
        issues.extend(_season_scope_issues(text, projection, unit_id))
        issues.extend(_scheduled_date_issues(text, projection, unit_id))
        issues.extend(_debut_and_reunion_issues(text, projection, unit_id))
        issues.extend(_locked_forecast_issues(text, projection, unit_id))
        issues.extend(_model_attribution_issues(text, unit_id))
    return issues
