"""Analyst schema, evidence IDs, numeric boundaries and frozen-context wiring.

The retired Chinese phrase validators and old full-product repair route do
not certify prose. Independent review/repair protocol belongs to the editorial
pipeline tests; fake clients below prove compatibility and persistence only,
not real reviewer accuracy or the factuality of their supplied analysis.
"""
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


def test_product_request_still_requires_story_and_prediction():
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
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["prediction_perspective"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction", "evidence-form"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    assert validate_judgment(payload, current)


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_product_watch_themes_accept_zero_through_four_without_changing_other_requirements(count):
    current = build_analyst_context(
        LAYERS,
        EVIDENCE,
        missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前内容。",
    )
    payload = judgment_payload()
    source_themes = payload["watch_themes"]
    payload["watch_themes"] = [copy.deepcopy(source_themes[index % len(source_themes)]) for index in range(count)]
    payload["pregame_story"]["evidence_ids"] = ["evidence-prediction"]
    payload["final_prediction"]["evidence_ids"] = ["evidence-prediction"]
    payload["analysis_article_evidence_ids"] = ["evidence-prediction", "evidence-form"]
    for item in payload["watch_themes"]:
        item["evidence_ids"] = ["evidence-form"]
    assert ProductAnalystOutput.model_validate(payload).model_dump()["watch_themes"] == payload["watch_themes"]
    assert len(validate_judgment(payload, current).watch_themes) == count


def test_product_watch_themes_still_reject_more_than_four():
    payload = judgment_payload()
    payload["watch_themes"] = payload["watch_themes"] * 3
    with pytest.raises(ValueError):
        ProductAnalystOutput.model_validate(payload)


def test_product_and_persisted_judgment_schemas_allow_candidate_bench_for_three_players_per_team():
    payload = judgment_payload()
    payload["player_analysis"] = [{
        "player_name": f"Fixture Player {index}",
        "statement": "角色与本场对手的互动是这组测试中的说明文本。",
        "classification": "subjective_analysis",
        "evidence_ids": [],
    } for index in range(8)]
    assert len(ProductAnalystOutput.model_validate(payload).player_analysis) == 8
    assert len(AnalystJudgment.model_validate(payload).player_analysis) == 8


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


def test_legacy_schema_preserves_article_text_and_official_forecast():
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


def test_legacy_call_budget_exhaustion_stops_before_a_second_client_call(tmp_path, context):
    calls = []

    def client(messages, schema):
        calls.append(schema.__name__)
        return judgment_payload(), {}

    analyst = EvidenceBoundAnalyst(root=tmp_path / "calls", client=client, max_calls=1)
    analyst.judge(context)
    with pytest.raises(ValueError, match="analyst_call_budget_exhausted"):
        analyst.judge(context)
    assert calls == ["AnalystJudgment"]
    assert len(list((tmp_path / "calls").glob("*/attempt.json"))) == 1


def test_two_stage_budget_guard_runs_before_forecast_writer_or_reviewer(tmp_path, context):
    def forbidden_client(*args, **kwargs):
        raise AssertionError("Insufficient budget must stop before any client call")

    analyst = EvidenceBoundAnalyst(
        root=tmp_path / "calls", client=forbidden_client,
        forecast_client=forbidden_client, reviewer_client=forbidden_client,
        max_calls=3, include_forecasting_skill=True,
    )
    with pytest.raises(ValueError, match="analyst_budget_cannot_cover_forecast_and_writer"):
        analyst.judge(context)
    assert not list((tmp_path / "calls").glob("*/forecast/*_request.json"))
    assert not list((tmp_path / "calls").glob("*/editorial/*_request.json"))


def test_saved_philadelphia_context_preserves_roster_old_windows_and_official(monkeypatch, tmp_path):
    from .analyst import AnalystContext
    from v2.frontend.analysis_service import SavedGameAnalysisService
    from v2.frontend.export_data import build_app_data

    def blocked(*args, **kwargs):
        raise AssertionError("no network permitted")

    seen = {}
    def client(messages, schema):
        # Supplied prose is a compatibility fixture, not a semantic-review result.
        current = AnalystContext.model_validate_json(messages[-1]["content"])
        seen["context"] = current
        roster_evidence = next(item for item in current.objective_evidence if item["section"] == "roster_context")
        assert roster_evidence["value"]["context_reference"] == "roster_context"
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
    from .prediction import Predictor
    agent=PregameAgent(analyst=analyst,predictor=Predictor(runtime_root=BASE/'runtime_demo'))
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
    from .prediction import Predictor
    agent=PregameAgent(schedule=[game],live_market_root=tmp_path/'market',predictor=Predictor(runtime_root=BASE/'runtime_demo'))
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

    from .prediction import Predictor
    agent=PregameAgent(web_searcher=Searcher(),predictor=Predictor(runtime_root=BASE/'runtime_demo'))
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
