from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from .analyst import (
    AnalystJudgment,
    EvidenceBoundAnalyst,
    ProductAnalystOutput,
    build_analyst_context,
    validate_judgment,
)
from .storage import content_hash


LAYERS = {
    "prediction_method": "market_residual_v1",
    "pure_prediction": {
        "win_probability": 0.61,
        "expected_margin": 5.0,
        "expected_total": 238.0,
    },
    "market_benchmark": {
        "market_implied_win_probability": 0.55,
        "market_expected_margin": 2.0,
        "market_total": 230.0,
    },
    "divergence": {
        "pure_market_win_probability_gap": 0.06,
        "pure_market_margin_gap": 3.0,
        "pure_market_total_gap": 8.0,
    },
    "residual_prediction": {
        "win_probability_residual": -0.02,
        "expected_margin_residual": -1.0,
        "expected_total_residual": -3.0,
    },
    "official_numeric_prediction": {
        "win_probability": 0.53,
        "expected_margin": 1.0,
        "expected_total": 227.0,
    },
    "official_market_adjustment": {
        "win_probability": -0.02,
        "expected_margin": -1.0,
        "expected_total": -3.0,
    },
}

EVIDENCE = [
    {"id": "evidence-prediction", "section": "prediction", "kind": "ModelForecast", "value": LAYERS},
    {"id": "evidence-market", "section": "market", "kind": "Fact", "value": {"status": "available"}},
    {"id": "evidence-form", "section": "team_form", "kind": "Fact", "value": {"status": "available"}},
]

ANALYSIS_ARTICLE = (
    "这场比赛的第一层判断来自双方当前可见的球队状态，但真正决定走势的仍是阵容如何落到比赛里。"
    "主队在半场组织、篮板保护和关键回合执行上更稳定，客队则拥有改变节奏和制造错位的能力。"
    "如果主队能够减少失误并让核心持球人持续进入擅长区域，比赛会更接近主队熟悉的节奏。"
    "客队想改变方向，需要提高转换频率，并通过外线投射迫使主队扩大防守范围。"
    "当前盘口体现出市场认可主队方向，同时对优势幅度保持谨慎，这与阵容信息仍不完整有关。"
    "比赛最值得看的部分，是双方如何处理核心球员之间的空间关系，以及替补阶段能否维持首发创造的优势。"
    "伤病和最终首发公布后，预测幅度仍可能变化，但当前材料足以给出明确方向。"
    "综合这些因素，第25秒选择主队获胜，并预计比赛在最后阶段才真正拉开。"
    "从对位角度看，外线第一道防守能否延误挡拆，会影响内线协防是否需要提前收缩，也会改变弱侧射手得到空位的频率。"
    "替补阵容进入比赛后，控球稳定性和防守沟通可能比单个球员的纸面能力更重要。"
    "如果客队能够在前场篮板和罚球线上持续制造回合，主队的优势会被压缩；反过来，主队一旦控制防守篮板并减少无谓失误，比赛会更快进入预期路径。"
)


@pytest.fixture
def context():
    return build_analyst_context(
        LAYERS,
        EVIDENCE,
        missing_data=[{"section": "availability", "reason": "missing"}],
    )


def judgment_payload():
    return {
        "winner_lean": "home",
        "side_lean": "away",
        "total_lean": "under",
        "confidence": "low",
        "supporting_evidence_ids": ["evidence-prediction"],
        "conflicting_evidence_ids": ["evidence-form"],
        "domain_analysis_signals": ["pure_market_divergence", "surface_narrative_pricing_tension"],
        "unresolved_uncertainty": ["球员可用性证据缺失"],
        "baseline_agreement": {
            "pure": "disagree",
            "market": "mixed",
            "residual": "agree",
        },
        "subjective_rationale": "综合证据后倾向市场总分下方，置信度有限。",
        "pregame_story": {
            "title": "方向明确，比赛仍有变数",
            "statement": "主队方向清晰，近期状态与赛前信息让这场比赛仍值得继续观察。",
            "classification": "data_inference",
            "evidence_ids": [],
        },
        "watch_themes": [
            {
                "title": "谁能先控制比赛节奏",
                "statement": "双方如何争夺比赛节奏，是判断优势能否兑现的关键。",
                "classification": "subjective_analysis",
                "evidence_ids": [],
            },
            {
                "title": "纸面优势会不会被现实变量压缩",
                "statement": "当前伤病信息仍不完整，实际轮换可能改变赛前判断的幅度。",
                "classification": "data_inference",
                "evidence_ids": [],
            },
        ],
        "prediction_perspective": {
            "direction_view": "agree",
            "magnitude_view": "uncertain",
            "market_relation": "divergent",
            "statement": "第25秒认同主队方向，但阵容与临场信息可能让实际优势小于模型表达。",
            "classification": "subjective_analysis",
            "evidence_ids": [],
        },
        "final_prediction": {
            "winner_side": "home",
            "win_probability": 0.58,
            "expected_margin": 3.5,
            "expected_total": 228.5,
            "evidence_ids": [],
        },
        "analysis_article": ANALYSIS_ARTICLE,
        "analysis_article_evidence_ids": [],
        "roster_analysis": [{
            "statement": "当前官方名单是分析时点的球队归属证据，不代表最终出场名单。",
            "classification": "source_fact",
            "evidence_ids": ["evidence-prediction"],
        }],
        "player_analysis": [],
        "cold_start_analysis": [],
        "historical_data_relevance": [{
            "statement": "近期窗口仍应结合阵容时点解释。",
            "classification": "data_inference",
            "evidence_ids": ["evidence-form"],
        }],
        "key_matchup_variables": [],
        "supporting_evidence": [{
            "statement": "正式预测提供当前判断的数值基线。",
            "classification": "source_fact",
            "evidence_ids": ["evidence-prediction"],
        }],
        "conflicting_evidence": [{
            "statement": "近期状态证据使该判断保留不确定性。",
            "classification": "data_inference",
            "evidence_ids": ["evidence-form"],
        }],
        "uncertainty": [{
            "statement": "当前没有符合时间要求的伤病证据。",
            "classification": "source_fact",
            "evidence_ids": [],
        }],
        "classification": "subjective_analyst_judgment",
    }


def test_judgment_can_disagree_without_changing_official_forecast(context):
    before = content_hash(context.official_numeric_prediction)
    judgment = AnalystJudgment.model_validate(judgment_payload())
    validated = validate_judgment(judgment, context)
    assert validated.total_lean == "under"
    assert validated.baseline_agreement["pure"] == "disagree"
    assert content_hash(context.official_numeric_prediction) == before


def test_unknown_top_level_llm_explanations_are_ignored(context):
    payload = judgment_payload()
    payload["winner_lean_reason"] = "额外解释不会进入有效 Judgment。"
    validated = validate_judgment(payload, context)
    assert "winner_lean_reason" not in validated.model_dump()


def test_known_deepseek_classification_alias_is_repaired_without_touching_facts():
    payload = judgment_payload()
    payload["prediction_perspective"]["classification"] = "subjective_analyst_judgment"
    payload["winner_lean_reason"] = "schema 外解释"
    raw = SimpleNamespace(tool_calls=[{"args": payload}])
    repaired, paths = EvidenceBoundAnalyst._repair_structured_args(raw)
    judgment = AnalystJudgment.model_validate(repaired)
    assert judgment.prediction_perspective.classification == "subjective_analysis"
    assert judgment.final_prediction.expected_margin == 3.5
    assert paths == ["prediction_perspective.classification"]


def test_text_format_repair_preserves_editorial_wording_and_prediction():
    payload = judgment_payload()
    payload["analysis_article"] = ANALYSIS_ARTICLE + "这不是偶然手感，而是对位持续落后。冠军归来—赛季开始。"
    original = payload["analysis_article"]
    payload["analysis_article"] += "\\n第二段。"
    judgment, repairs = EvidenceBoundAnalyst._repair_public_style(
        AnalystJudgment.model_validate(payload)
    )
    assert judgment.analysis_article == original + "\n第二段。"
    assert repairs == ["analysis_article"]
    assert judgment.final_prediction.expected_margin == 3.5
    assert judgment.final_prediction.evidence_ids == []


def test_product_request_requires_story_watch_themes_and_prediction_perspective():
    current = build_analyst_context(
        LAYERS,
        EVIDENCE,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前内容。",
    )
    payload = judgment_payload()
    payload["pregame_story"] = None
    with pytest.raises(ValueError, match="judgment_product_content_incomplete"):
        validate_judgment(payload, current)
    payload = judgment_payload()
    payload["watch_themes"] = payload["watch_themes"][:1]
    with pytest.raises(ValueError, match="judgment_product_content_incomplete"):
        validate_judgment(payload, current)
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["prediction_perspective"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction", "evidence-form"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    assert validate_judgment(payload, current)


def test_product_prediction_requires_clear_direction_and_half_point_margin():
    current = build_analyst_context(
        LAYERS,
        EVIDENCE,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    payload["winner_lean"] = "no_lean"
    with pytest.raises(ValueError, match="judgment_final_prediction_direction_mismatch"):
        validate_judgment(payload, current)
    payload["winner_lean"] = "home"
    payload["final_prediction"]["expected_margin"] = 3.7
    with pytest.raises(ValueError, match="judgment_final_prediction_not_half_point"):
        validate_judgment(payload, current)


def test_product_layer_mentions_are_editorial_choices_not_validation_errors():
    current = build_analyst_context(
        LAYERS,
        EVIDENCE,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    payload["analysis_article"] = ANALYSIS_ARTICLE + "纯模型原本给出了另一组分差。"
    assert validate_judgment(payload, current).analysis_article == payload["analysis_article"]
    assert current.official_numeric_prediction == LAYERS["official_numeric_prediction"]


def test_product_market_total_language_matches_final_prediction():
    evidence = [
        *EVIDENCE,
        {
            "id": "public-market",
            "section": "web_context",
            "kind": "Fact",
            "value": {"market_reference": {"total": 233.5}},
        },
    ]
    current = build_analyst_context(
        LAYERS,
        evidence,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction", "public-market"]
    payload["final_prediction"]["expected_total"] = 228.5
    payload["analysis_article"] = ANALYSIS_ARTICLE + "综合判断后，我略微偏向大分。"
    payload["analysis_article_evidence_ids"] = ["evidence-prediction", "public-market"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    with pytest.raises(
        ValueError,
        match="judgment_total_lean_conflicts_with_final_prediction",
    ):
        validate_judgment(payload, current)


def test_series_total_generalization_must_match_actual_games():
    evidence = [
        *EVIDENCE,
        {
            "id": "series",
            "section": "matchup_history",
            "kind": "Fact",
            "value": {
                "matchup_relevance": "high",
                "series_summary": {"game_totals": [235, 210, 202, 258], "average_total": 226.25, "game_margins": [39, 6, 14, 30]},
            },
        },
    ]
    current = build_analyst_context(
        LAYERS,
        evidence,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["series"]
    payload["final_prediction"]["evidence_ids"] = ["series"]
    payload["analysis_article_evidence_ids"] = ["series"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["series"]
    payload["analysis_article"] = ANALYSIS_ARTICLE + "两队系列赛总分多在210分上下。"
    with pytest.raises(ValueError, match="unsupported_series_total_generalization"):
        validate_judgment(payload, current)
    payload["analysis_article"] = ANALYSIS_ARTICLE + "两队系列赛四场平均总分226.25分。"
    assert validate_judgment(payload, current)
    payload["analysis_article"] = ANALYSIS_ARTICLE + "客队四场都没能把差距压到个位数以内。"
    with pytest.raises(ValueError, match="series_margin_claim_conflicts_with_scores"):
        validate_judgment(payload, current)


def test_story_selection_is_free_but_claimed_facts_still_require_support():
    evidence = [
        *EVIDENCE,
        {
            "id": "story-champion",
            "section": "web_context",
            "kind": "Fact",
            "value": {
                "category": "game_story",
                "title": "2026 NBA Champions",
                "url": "https://www.nba.com/playoffs/2026/nba-finals",
            },
        },
        {
            "id": "story-meeting",
            "section": "web_context",
            "kind": "Fact",
            "value": {
                "category": "game_story",
                "title": "Previous meeting",
                "snippet": "The home team won 108-102.",
                "url": "https://www.nba.com/game/example",
            },
        },
    ]
    current = build_analyst_context(
        LAYERS,
        evidence,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    assert validate_judgment(payload, current)

    payload["pregame_story"]["evidence_ids"] = ["story-meeting"]
    payload["pregame_story"]["statement"] = "卫冕冠军回到主场，迎来这场再次交锋。"
    with pytest.raises(ValueError, match="judgment_story_ignores_verified_championship"):
        validate_judgment(payload, current)

    payload["pregame_story"]["evidence_ids"] = ["story-champion"]
    payload["pregame_story"]["evidence_ids"] = ["evidence-form"]
    payload["pregame_story"]["statement"] = "双方近15场状态是本次讨论的主题。"
    assert validate_judgment(payload, current)

    payload["pregame_story"] = {
        "title": "冠军戒指之夜",
        "statement": "卫冕冠军回到主场迎接新赛季。",
        "classification": "source_fact",
        "evidence_ids": ["story-champion"],
    }
    with pytest.raises(ValueError, match="unsupported_championship_ceremony_claim"):
        validate_judgment(payload, current)

    payload["pregame_story"] = {
        "title": "费城归来复仇",
        "statement": "76人回到花园争取扳回一场。",
        "classification": "source_fact",
        "evidence_ids": ["story-champion"],
    }
    with pytest.raises(ValueError, match="subjective_motivation_labeled_as_fact"):
        validate_judgment(payload, current)

    payload["pregame_story"] = {
        "title": "再次相遇",
        "statement": "76人整个夏天都在研究如何解决这组对位。",
        "classification": "source_fact",
        "evidence_ids": ["story-champion"],
    }
    with pytest.raises(ValueError, match="subjective_motivation_labeled_as_fact"):
        validate_judgment(payload, current)

    payload["pregame_story"] = {
        "title": "队史首冠后的揭幕战",
        "statement": "主队以冠军身份回到主场。",
        "classification": "source_fact",
        "evidence_ids": ["story-champion"],
    }
    with pytest.raises(ValueError, match="unsupported_franchise_first_championship_claim"):
        validate_judgment(payload, current)

    payload["pregame_story"] = {
        "title": "卫冕冠军回到主场",
        "statement": "冠军班底迎来新赛季主场首战，对手则要重新回答上次季后赛交锋留下的问题。",
        "classification": "data_inference",
        "evidence_ids": ["story-champion"],
    }
    payload["analysis_article"] = ANALYSIS_ARTICLE + "这不是普通比赛，而是证明自己的机会。"
    assert validate_judgment(payload, current)
    payload["analysis_article"] = ANALYSIS_ARTICLE + "冠军归来—新赛季开始。"
    assert validate_judgment(payload, current)
    payload["analysis_article"] = ANALYSIS_ARTICLE
    assert validate_judgment(payload, current)


def test_high_relevance_matchup_is_optional_but_history_time_label_is_required():
    evidence = [
        *EVIDENCE,
        {
            "id": "matchup-series",
            "section": "matchup_history",
            "kind": "Fact",
            "value": {"status": "available", "matchup_relevance": "high", "series_summary": {"games": 4}},
        },
        {
            "id": "season-phase",
            "section": "season_phase",
            "kind": "Fact",
            "value": {"phase": "opening_night"},
        },
        {
            "id": "champion-story",
            "section": "web_context",
            "kind": "Fact",
            "value": {
                "category": "game_story",
                "title": "2026 NBA Champions",
                "snippet": "The home team won the championship.",
            },
        },
    ]
    current = build_analyst_context(
        LAYERS,
        evidence,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前最终预测。",
    )
    payload = judgment_payload()
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction", "matchup-series"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    assert validate_judgment(payload, current)

    payload["pregame_story"]["evidence_ids"] = ["matchup-series"]
    payload["analysis_article"] = ANALYSIS_ARTICLE + "近15场主队表现更稳定。"
    with pytest.raises(
        ValueError,
        match="opening_night_rolling_window_missing_previous_season_label",
    ):
        validate_judgment(payload, current)
    payload["analysis_article"] = ANALYSIS_ARTICLE + "上赛季近15场主队表现更稳定。"
    assert validate_judgment(payload, current)


def test_nested_analysis_evidence_is_validated(context):
    payload = judgment_payload()
    payload["roster_analysis"][0]["evidence_ids"] = ["invented"]
    with pytest.raises(ValueError, match="unknown_judgment_evidence"):
        validate_judgment(payload, context)


def test_structured_support_can_replace_legacy_support_id_list(context):
    payload = judgment_payload()
    payload["supporting_evidence_ids"] = []
    validated = validate_judgment(payload, context)
    assert validated.supporting_evidence[0].evidence_ids == ["evidence-prediction"]


def test_missing_availability_cannot_be_turned_into_confirmed_injury(context):
    payload = judgment_payload()
    payload["player_analysis"] = [{
        "player_name": "Known Player",
        "statement": "Known Player 本场确认因伤缺阵。",
        "classification": "subjective_analysis",
        "evidence_ids": ["evidence-prediction"],
    }]
    with pytest.raises(ValueError, match="unsupported_availability_claim"):
        validate_judgment(payload, context)


@pytest.mark.parametrize("statement,error", [
    ("系列赛里客队从未把比赛拖进最后五分钟。", "unsupported_series_game_flow_claim"),
    ("这些数字说明的是四场里持续存在的出手质量差距。", "unsupported_shot_quality_inference"),
])
def test_box_score_cannot_prove_unobserved_game_flow_or_shot_quality(context, statement, error):
    payload = judgment_payload()
    payload["historical_data_relevance"][0]["statement"] = statement
    with pytest.raises(ValueError, match=error):
        validate_judgment(payload, context)


def test_missing_availability_allows_clearly_hypothetical_game_variable(context):
    payload = judgment_payload()
    payload["player_analysis"] = [{
        "player_name": "Known Player",
        "statement": "如果核心球员缺阵，球队的持球组织会承受更大压力。",
        "classification": "subjective_analysis",
        "evidence_ids": ["evidence-prediction"],
    }]
    assert validate_judgment(payload, context)


def test_roster_difference_cannot_be_stated_as_confirmed_transaction(context):
    payload = judgment_payload()
    payload["roster_analysis"][0]["statement"] = "该球员已经确认交易离队。"
    with pytest.raises(ValueError, match="unsupported_transaction_claim"):
        validate_judgment(payload, context)


def test_current_roster_alone_cannot_be_called_an_all_new_core(context):
    payload = judgment_payload()
    payload["roster_analysis"][0]["statement"] = "球队带着全新的核心名单进入新赛季。"
    with pytest.raises(ValueError, match="unsupported_roster_change_claim"):
        validate_judgment(payload, context)


def test_later_unknown_word_cannot_excuse_earlier_unsupported_upgrade_claim(context):
    payload = judgment_payload()
    payload["pregame_story"]["statement"] = (
        "当前名单有几位新名字，纸面火力明显提升，但轮换默契仍是未知数。"
    )
    with pytest.raises(ValueError, match="unsupported_roster_change_claim"):
        validate_judgment(payload, context)


def test_current_roster_alone_cannot_support_new_arrival_wording(context):
    payload = judgment_payload()
    payload["analysis_article"] = ANALYSIS_ARTICLE + "球队新加入的持球点将改变进攻。"
    with pytest.raises(ValueError, match="unsupported_roster_change_claim"):
        validate_judgment(payload, context)


def staged_forecast_client(messages, schema):
    value = {"prediction": {"winner_side": "home", "win_probability": .58,
        "expected_home_margin": 3.5, "expected_total": 228.5},
        "factors": [{"classification": "inference", "evidence_ids": ["evidence-prediction"],
            "effect": "home", "summary": "Basketball evidence supports the home direction."}], "uncertainties": []}
    if schema.__name__ == "MarketReviewForecast":
        value['factors'][0]['evidence_ids'].append('market.market_benchmark')
        value.update(decision="keep", market_assessment="unclear", rationale="Retain the independently assessed game environment.")
    return value, {}


@pytest.mark.parametrize("remains_invalid", [False, True])
@pytest.mark.parametrize("include_forecasting_skill", [False, True])
def test_product_fact_correction_is_bounded_and_preserves_failed_draft(tmp_path, remains_invalid, include_forecasting_skill):
    current = build_analyst_context(
        LAYERS, EVIDENCE, missing_data=[], analysis_request="生成这场比赛的第25秒赛前最终预测。"
    )
    calls = []

    def client(messages, schema):
        calls.append(messages)
        payload = judgment_payload()
        payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
        payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
        payload["analysis_article_evidence_ids"] = ["evidence-prediction"]
        for item in payload["watch_themes"]:
            item["evidence_ids"] = ["evidence-form"]
        if len(calls) == 1 or remains_invalid:
            payload["analysis_article"] += "球队新加入的持球点将改变进攻。"
        return payload, {}

    analyst = EvidenceBoundAnalyst(root=tmp_path, client=client, max_calls=4,
                                  include_forecasting_skill=include_forecasting_skill,
                                  forecast_client=staged_forecast_client)
    if remains_invalid:
        with pytest.raises(ValueError, match="unsupported_roster_change_claim"):
            analyst.judge(current)
    else:
        judgment, trace = analyst.judge(current)
        assert judgment.analysis_article == ANALYSIS_ARTICLE
        assert trace["correction"]["attempts"] == 1
    assert len(calls) == 2
    assert "当前名单没有证明" in calls[1][-1]["content"]
    revision = calls[1][-1]["content"]
    assert "schema" in revision and "evidence ID" in revision and "cutoff" in revision
    assert "一轮修正" in revision
    assert "系列赛分析" not in revision
    assert "免责声明" not in revision
    assert "篇幅" not in revision
    for messages in calls:
        system = messages[0]["content"]
        assert "<editorial_skill " in system
        assert "<forecasting_skill " not in system
        if include_forecasting_skill:
            assert "locked_final_prediction" in system
        assert "<pregame_research_skill " not in system
    assert list(tmp_path.glob("*/failure.json"))
    assert len(list(tmp_path.glob("*/parsed_unvalidated.json"))) == 2
    if include_forecasting_skill:
        assert len(list(tmp_path.glob("*/forecast/*_request.json"))) == 2


def test_writer_does_not_rewrite_or_retry_valid_editorial_choices(tmp_path):
    current = build_analyst_context(
        LAYERS, EVIDENCE, missing_data=[], analysis_request="生成这场比赛的第25秒赛前分析。"
    )
    payload = judgment_payload()
    for claim in [payload["pregame_story"], *payload["watch_themes"], payload["final_prediction"]]:
        claim["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article"] += "模型也有不确定性。这不是结论，而是我的判断。比赛开始—等待结果。"
    calls = []
    def client(messages, schema):
        calls.append(messages)
        return payload, {}
    judgment, trace = EvidenceBoundAnalyst(
        root=tmp_path, client=client, include_forecasting_skill=True,
        forecast_client=staged_forecast_client,
    ).judge(current)
    assert len(calls) == 1
    assert judgment.analysis_article == payload["analysis_article"]
    assert judgment.final_prediction.model_dump() == payload["final_prediction"]
    assert "correction" not in trace
    assert not list(tmp_path.glob("*/failure.json"))


def test_web_fact_must_be_linked_to_the_specific_claim():
    evidence = [
        *EVIDENCE,
        {
            "id": "web-transaction",
            "section": "web_context",
            "kind": "Fact",
            "value": {"category": "roster_news", "title": "Official transaction"},
        },
    ]
    current = build_analyst_context(LAYERS, evidence, missing_data=[])
    payload = judgment_payload()
    payload["supporting_evidence_ids"].append("web-transaction")
    payload["roster_analysis"][0] = {
        "statement": "该球员已经确认交易加盟。",
        "classification": "source_fact",
        "evidence_ids": ["evidence-prediction"],
    }
    with pytest.raises(ValueError, match="unsupported_transaction_claim"):
        validate_judgment(payload, current)
    payload["roster_analysis"][0]["evidence_ids"].append("web-transaction")
    assert validate_judgment(payload, current)


def test_historical_minutes_cannot_be_stated_as_current_starter(context):
    payload = judgment_payload()
    payload["player_analysis"] = [{
        "player_name": "Known Player",
        "statement": "Known Player 将担任本场首发。",
        "classification": "subjective_analysis",
        "evidence_ids": ["evidence-prediction"],
    }]
    with pytest.raises(ValueError, match="unsupported_starter_claim"):
        validate_judgment(payload, context)


def test_explicitly_unknown_starter_language_is_allowed(context):
    payload = judgment_payload()
    payload["uncertainty"] = [{
        "statement": "当前无确认首发证据，名单也不能证明其首发身份。",
        "classification": "source_fact",
        "evidence_ids": [],
    }]
    assert validate_judgment(payload, context)


def test_missing_starter_evidence_is_not_mistaken_for_a_confirmed_lineup(context):
    payload = judgment_payload()
    payload["roster_analysis"][0]["statement"] = (
        "双方均无伤病、出场状态与确认首发证据。"
    )
    assert validate_judgment(payload, context)


def test_roster_confirmation_word_is_not_mistaken_for_injury_claim(context):
    payload = judgment_payload()
    payload["uncertainty"] = [{
        "statement": "揭幕战最终名单未经确认，当前球员可用性状态也缺少来源。",
        "classification": "source_fact",
        "evidence_ids": [],
    }]
    assert validate_judgment(payload, context)


def test_player_analysis_must_use_an_official_roster_name():
    roster_value = {
        "teams": {
            "home": {"official_team_roster": {"players": [{"name": "Known Player"}]}},
            "away": {"official_team_roster": {"players": []}},
        }
    }
    evidence = [
        {"id": "roster", "section": "roster_context", "kind": "Fact", "value": roster_value},
        {"id": "prediction", "section": "prediction", "kind": "ModelForecast", "value": LAYERS},
    ]
    current = build_analyst_context(LAYERS, evidence, missing_data=[])
    payload = judgment_payload()
    payload["supporting_evidence_ids"] = ["prediction"]
    payload["conflicting_evidence_ids"] = []
    payload["roster_analysis"] = []
    payload["historical_data_relevance"] = []
    payload["supporting_evidence"] = []
    payload["conflicting_evidence"] = []
    payload["player_analysis"] = [{
        "player_name": "Invented Player",
        "statement": "该球员可能改变球队空间。",
        "classification": "subjective_analysis",
        "evidence_ids": ["roster"],
    }]
    with pytest.raises(ValueError, match="player_not_in_official_roster"):
        validate_judgment(payload, current)


def test_story_cannot_assign_an_opponent_player_to_current_roster():
    roster_value = {
        "teams": {
            "home": {"official_team_roster": {"players": [{"name": "Andre Drummond"}]}},
            "away": {"official_team_roster": {"players": [{"name": "Joel Embiid"}]}},
        }
    }
    evidence = [
        *EVIDENCE,
        {"id": "roster", "section": "roster_context", "kind": "Fact", "value": roster_value},
    ]
    current = build_analyst_context(
        LAYERS, evidence, missing_data=[], analysis_request="生成76人对尼克斯的常规分析"
    )
    payload = judgment_payload()
    payload["watch_themes"][0]["statement"] = "76人当前名单里有Embiid、Drummond两名内线。"
    with pytest.raises(ValueError, match="player_attributed_to_wrong_team_roster"):
        validate_judgment(payload, current)
    payload["watch_themes"][0]["statement"] = "76人当前名单里有Embiid。"
    assert validate_judgment(payload, current)


def test_unknown_supporting_evidence_is_rejected(context):
    payload = judgment_payload()
    payload["supporting_evidence_ids"] = ["invented"]
    with pytest.raises(ValueError, match="unknown_judgment_evidence"):
        validate_judgment(AnalystJudgment.model_validate(payload), context)


def test_judgment_schema_has_no_exact_numeric_forecast_fields():
    fields = set(AnalystJudgment.model_fields)
    assert not fields & {"win_probability", "expected_margin", "expected_total", "score"}


def test_product_generation_schema_excludes_legacy_rubric_sections():
    fields = set(ProductAnalystOutput.model_fields)
    assert {"final_prediction", "analysis_article", "pregame_story", "watch_themes"} <= fields
    assert not fields & {
        "game_script",
        "model_risk",
        "unresolved_uncertainty",
    }


def test_rolling_window_number_is_allowed_but_new_numeric_forecast_is_rejected(context):
    payload = judgment_payload()
    payload["subjective_rationale"] = "rolling 15 全部来自上赛季，因此只能作为历史基线。"
    assert validate_judgment(payload, context)
    payload["subjective_rationale"] = "我自行给出胜率 72%。"
    with pytest.raises(ValueError, match="judgment_exact_numeric_forecast_not_allowed"):
        validate_judgment(payload, context)
    payload = judgment_payload()
    payload["pregame_story"]["statement"] = "模型给出的胜率是 72%。"
    with pytest.raises(ValueError, match="judgment_exact_numeric_forecast_not_allowed"):
        validate_judgment(payload, context)


def test_team_signal_cannot_be_rewritten_as_model_feature_attribution(context):
    payload = judgment_payload()
    payload["supporting_evidence"][0]["statement"] = (
        "近期净效率差是模型给出主队高胜率的主要来源。"
    )
    with pytest.raises(ValueError, match="unsupported_model_attribution_claim"):
        validate_judgment(payload, context)


def test_domain_perspectives_separate_observation_and_missing_evidence(context):
    signals = {item.name: item for item in context.domain_analysis_perspectives}
    assert signals["pure_market_divergence"].status == "reviewable"
    assert signals["motivation_proxies"].classification == "domain_analysis_perspective"
    assert "standings" in signals["motivation_proxies"].missing_requirements


def test_fake_structured_analyst_is_saved_and_evidence_checked(tmp_path, context):
    def client(messages, schema):
        assert context.context_hash in messages[-1]["content"]
        return judgment_payload(), {"prompt_tokens": 120, "completion_tokens": 60}

    analyst = EvidenceBoundAnalyst(root=tmp_path / "analyst_calls", client=client)
    judgment, trace = analyst.judge(context)
    assert judgment.total_lean == "under"
    assert trace["status"] == "success"
    assert trace["usage"]["completion_tokens"] == 60
    response_paths = list((tmp_path / "analyst_calls").glob("*/response.json"))
    assert response_paths
    attempt = json.loads(response_paths[0].with_name("attempt.json").read_text(encoding="utf-8"))
    assert attempt["skill_hashes"]["editorial"]
    assert "forecasting" not in attempt["skill_hashes"]


def test_real_philadelphia_context_drives_roster_and_old_window_analysis(monkeypatch, tmp_path):
    from .analyst import AnalystContext
    from v2.frontend.analysis_service import SavedGameAnalysisService
    from v2.frontend.export_data import build_app_data

    def blocked(*args, **kwargs):
        raise AssertionError("no network permitted")

    seen = {}
    def client(messages, schema):
        current = AnalystContext.model_validate_json(messages[-1]["content"])
        seen["context"] = current
        roster_evidence = next(item for item in current.objective_evidence if item["section"] == "roster_context")
        prediction_evidence = next(item for item in current.objective_evidence if item["section"] == "prediction")
        matchup_evidence = next(item for item in current.objective_evidence if item["section"] == "matchup_history")
        payload = judgment_payload()
        payload.update({
            "winner_lean": "away",
            "side_lean": "no_lean",
            "total_lean": "no_lean",
            "supporting_evidence_ids": [prediction_evidence["id"], roster_evidence["id"]],
            "conflicting_evidence_ids": [],
            "baseline_agreement": {"pure": "mixed", "market": "not_applicable", "residual": "not_applicable"},
            "subjective_rationale": "正式预测提供方向，但当前阵容和旧赛季窗口之间存在明显时点错位。",
            "pregame_story": {
                "title": "新名单遇上旧基线",
                "statement": "76 人当前名单包含 LeBron James、Tyrese Maxey 和 Joel Embiid，而近期球队窗口仍来自上赛季。",
                "classification": "data_inference",
                "evidence_ids": [roster_evidence["id"], matchup_evidence["id"]],
            },
            "watch_themes": [
                {
                    "title": "76 人的球权如何分配",
                    "statement": "多名高使用率持球人与内线终结点如何共存，是本场值得关注的比赛变量。",
                    "classification": "subjective_analysis",
                    "evidence_ids": [roster_evidence["id"]],
                },
                {
                    "title": "旧赛季基线还能解释多少",
                    "statement": "当前名单与旧赛季窗口存在时点差异，团队均值的代表性需要谨慎判断。",
                    "classification": "data_inference",
                    "evidence_ids": [roster_evidence["id"]],
                },
            ],
            "prediction_perspective": {
                "direction_view": "agree",
                "magnitude_view": "uncertain",
                "market_relation": "unavailable",
                "statement": "第25秒认同正式预测方向，但当前名单和旧赛季基线之间的错位使优势幅度仍有疑问。",
                "classification": "subjective_analysis",
                "evidence_ids": [prediction_evidence["id"], roster_evidence["id"], matchup_evidence["id"]],
            },
            "final_prediction": {
                "winner_side": "away",
                "win_probability": 0.61,
                "expected_margin": 2.5,
                "expected_total": 229.5,
                "evidence_ids": [prediction_evidence["id"], roster_evidence["id"]],
            },
            "analysis_article": ANALYSIS_ARTICLE.replace("主队", "客队"),
            "analysis_article_evidence_ids": [prediction_evidence["id"], roster_evidence["id"], matchup_evidence["id"]],
            "roster_analysis": [{
                "statement": "76 人当前官方名单包括 LeBron James、Tyrese Maxey 和 Joel Embiid；该名单仅代表抓取时点。",
                "classification": "source_fact",
                "evidence_ids": [roster_evidence["id"]],
            }],
            "player_analysis": [{
                "player_name": "LeBron James",
                "statement": "LeBron James 的组织与持球角色可能改变球队进攻结构；这是篮球层面的主观分析，没有转化为数值加减分。",
                "classification": "subjective_analysis",
                "evidence_ids": [roster_evidence["id"]],
            }],
            "cold_start_analysis": [{
                "statement": "76 人 rolling 5 的五场和 rolling 15 的十五场全部来自上赛季。",
                "classification": "source_fact",
                "evidence_ids": [roster_evidence["id"]],
            }],
            "historical_data_relevance": [{
                "statement": "当前官方名单已更新，旧赛季团队统计只能作为历史基线，无法完整代表当前阵容。",
                "classification": "data_inference",
                "evidence_ids": [roster_evidence["id"]],
            }],
            "key_matchup_variables": [{
                "statement": "76 人核心持球与内线终结角色如何共存，是本场值得观察的变量。",
                "classification": "subjective_analysis",
                "evidence_ids": [roster_evidence["id"]],
            }],
            "supporting_evidence": [{
                "statement": "正式预测与当前名单证据共同支持谨慎的客队倾向。",
                "classification": "subjective_analysis",
                "evidence_ids": [prediction_evidence["id"], roster_evidence["id"]],
            }],
            "conflicting_evidence": [],
            "uncertainty": [{
                "statement": "当前缺少符合截止时间的伤病与确认首发证据。",
                "classification": "source_fact",
                "evidence_ids": [],
            }],
            "unresolved_uncertainty": ["当前伤病和确认首发证据缺失"],
        })
        return payload, {"prompt_tokens": 10, "completion_tokens": 10}

    monkeypatch.setattr("requests.Session.request", blocked)
    analyst = EvidenceBoundAnalyst(root=tmp_path / "calls", client=client)
    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    expected_official = copy.deepcopy(game["relationship"]["final"])
    report = SavedGameAnalysisService(
        data,
        analyst=analyst,
        searcher=None,
    ).analyze("espn:401909089", allow_lookup=False)
    context = seen["context"].roster_context
    assert len(seen["context"].model_dump_json()) < 80000
    away = context["teams"]["away"]
    names = {player["name"] for player in away["official_team_roster"]["players"]}
    assert {"LeBron James", "Tyrese Maxey", "Joel Embiid"} <= names
    assert away["cold_start"]["cold_start"] is True
    assert away["cold_start"]["rolling_5_current_season_games"] == 0
    assert away["cold_start"]["rolling_5_previous_season_games"] == 5
    assert away["cold_start"]["rolling_15_current_season_games"] == 0
    assert away["cold_start"]["rolling_15_previous_season_games"] == 15
    judgment = report["judgment"]
    combined = json.dumps(judgment, ensure_ascii=False)
    assert all(name in combined for name in ("LeBron James", "Tyrese Maxey", "Joel Embiid"))
    assert "历史基线" in combined and "无法完整代表当前阵容" in combined
    assert report["official_numeric_prediction"] == expected_official


def test_betting_execution_language_is_rejected(context):
    payload = judgment_payload()
    payload["subjective_rationale"] = "建议下注并投入 bankroll。"
    with pytest.raises(ValueError, match="judgment_betting_execution_language"):
        validate_judgment(AnalystJudgment.model_validate(payload), context)


def test_pregame_agent_persists_context_judgment_and_trace(tmp_path):
    from .agent import PregameAgent
    from .storage import BASE
    from .analyst import AnalystContext

    demo=json.loads((BASE/'runtime_demo/context.json').read_text(encoding='utf-8'))

    def client(messages, schema):
        current=AnalystContext.model_validate_json(messages[-1]['content'])
        evidence_id=current.objective_evidence[0]['id']
        payload={
            'winner_lean':'no_lean',
            'side_lean':'no_lean',
            'total_lean':'no_lean',
            'confidence':'low',
            'supporting_evidence_ids':[evidence_id],
            'conflicting_evidence_ids':[],
            'domain_analysis_signals':['pure_market_divergence'],
            'unresolved_uncertainty':['合法市场数据当前缺失'],
            'baseline_agreement':{'pure':'agree','market':'not_applicable','residual':'not_applicable'},
            'subjective_rationale':'现有证据只支持保留中性倾向。',
            'classification':'subjective_analyst_judgment',
        }
        return payload,{'prompt_tokens':10,'completion_tokens':5}

    analyst=EvidenceBoundAnalyst(root=tmp_path/'calls',client=client)
    agent=PregameAgent(analyst=analyst)
    report,_=agent.ask(
        'structured analyst fixture',
        cutoff=demo['cutoff'],
        plan={'intent':'report','game_id':demo['game']['game_id']},
        generate_judgment=True,
    )
    assert report['analyst_context']['context_hash']
    assert report['analyst_judgment']['classification']=='subjective_analyst_judgment'
    assert report['prediction']==report['official_numeric_prediction']
    assert any(row.get('node')=='analyst_judgment' and row.get('status')=='success' for row in report['trace'])


def test_real_pregame_agent_context_receives_all_four_legal_market_layers(tmp_path):
    from .agent import PregameAgent
    from .market import save_live_market_snapshot
    from .storage import BASE

    demo=json.loads((BASE/'runtime_demo/context.json').read_text(encoding='utf-8'))
    game=demo['game']
    quote={
        'game_id':game['game_id'],
        'event_id':game['event_id'],
        'home_team_id':game['home_team_id'],
        'away_team_id':game['away_team_id'],
        'scheduled_tipoff':game['scheduled_tipoff'],
        'provider_id':'100',
        'provider':'Draft Kings',
        'expected_home_margin':-2.5,
        'total':226.5,
        'home_probability_no_vig':0.48,
        'captured_at':'2026-09-13T19:59:00+00:00',
        'source_published_at':None,
        'eligible_prediction_input':True,
        'source':{'kind':'legal_market_fixture'},
    }
    save_live_market_snapshot(quote,root=tmp_path/'market')
    agent=PregameAgent(schedule=[game],live_market_root=tmp_path/'market')
    report,_=agent.ask('four layer fixture',cutoff=demo['cutoff'],
        plan={'intent':'report','game_id':game['game_id']})
    assert report['prediction_method']=='market_residual_v1'
    assert report['pure_prediction']
    assert report['market_benchmark']
    assert report['residual_prediction']
    assert report['official_numeric_prediction']==report['prediction']
    context=report['analyst_context']
    assert context['pure_prediction']==report['pure_prediction']
    assert context['market_benchmark']==report['market_benchmark']
    assert context['residual_prediction']==report['residual_prediction']
    assert context['official_numeric_prediction']==report['official_numeric_prediction']


def test_public_market_fallback_stays_out_of_numeric_prediction(monkeypatch):
    from .agent import PregameAgent
    from .storage import BASE

    demo=json.loads((BASE/'runtime_demo/context.json').read_text(encoding='utf-8'))

    class Searcher:
        def search(self, game, cutoff, **kwargs):
            assert 'market' in kwargs['categories']
            return {
                'status':'available',
                'records':[{
                    'id':'public-market',
                    'category':'market',
                    'title':'Pistons vs Celtics odds',
                    'url':'https://sports.betmgm.com/example',
                    'published_at':None,
                    'captured_at':'2026-09-13T19:59:00+00:00',
                    'score':0.8,
                    'eligible_for_fact':True,
                    'market_reference':{
                        'status':'available',
                        'usage':'display_only_public_market_reference',
                        'eligible_prediction_input':False,
                        'perspective':'home',
                        'home_spread':-1.5,
                        'away_spread':1.5,
                        'expected_home_margin':1.5,
                        'total':221.5,
                        'source_title':'Pistons vs Celtics odds',
                        'source_url':'https://sports.betmgm.com/example',
                        'source_domain':'sports.betmgm.com',
                        'source_tier':1,
                        'captured_at':'2026-09-13T19:59:00+00:00',
                    },
                }],
            }

    agent=PregameAgent(web_searcher=Searcher())
    monkeypatch.setattr(agent,'market',lambda game,cutoff,replay=False:{
        'as_of_cutoff':None,
        'historical_reference':None,
        'reason':'no legal future market data available',
        'model_market_disagreement':None,
    })
    report,_=agent.ask(
        'public market fallback fixture',
        cutoff=demo['cutoff'],
        plan={'intent':'report','game_id':demo['game']['game_id']},
        allow_lookup=True,
    )
    assert report['public_market_reference']['home_spread']==-1.5
    assert report['public_market_reference']['eligible_prediction_input'] is False
    assert report['market_benchmark'] is None
    assert report['residual_prediction'] is None
    assert report['official_numeric_prediction']==report['pure_prediction']


def test_public_cli_exposes_explicit_analyst_opt_in_and_ledger():
    from .storage import BASE

    text=(BASE/'__main__.py').read_text(encoding='utf-8')
    assert "--analyst-judgment" in text
    assert "--analyst-ledger" in text
