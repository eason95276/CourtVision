from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import editorial_skill, forecasting_skill, research_skill
from .analyst import EvidenceBoundAnalyst, build_analyst_context
from .web_research_agent import AutonomousWebResearcher
from v2.frontend.analysis_service import SavedGameAnalysisService


@pytest.fixture(autouse=True)
def default_examples_policy(monkeypatch):
    monkeypatch.delenv("NBA_EDITORIAL_EXAMPLES", raising=False)


LAYERS = {
    "pure_prediction": {"win_probability": 0.6, "expected_margin": 4.0, "expected_total": 220.0},
    "market_benchmark": None,
    "divergence": None,
    "residual_prediction": None,
    "official_numeric_prediction": {
        "win_probability": 0.6,
        "expected_margin": 4.0,
        "expected_total": 220.0,
    },
}


@pytest.mark.parametrize(
    ("module", "path_attr", "loader"),
    [
        (research_skill, "PREGAME_RESEARCH_SKILL_PATH", research_skill.load_pregame_research_skill),
        (forecasting_skill, "FORECASTING_SKILL_PATH", forecasting_skill.load_forecasting_skill),
    ],
)
def test_single_file_skill_loaders_read_latest_content_each_time(tmp_path, monkeypatch, module, path_attr, loader):
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("version one", encoding="utf-8")
    monkeypatch.setattr(module, path_attr, skill_path)

    assert "version one" in loader()
    skill_path.write_text("version two", encoding="utf-8")
    assert "version two" in loader()


def test_editorial_loader_reads_latest_skill_without_examples_by_default(tmp_path, monkeypatch):
    skill_dir = tmp_path / "editorial"
    skill_dir.mkdir()
    monkeypatch.delenv("NBA_EDITORIAL_EXAMPLES", raising=False)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("editorial one", encoding="utf-8")
    (skill_dir / "good_examples.md").write_text("must stay unloaded", encoding="utf-8")
    monkeypatch.setattr(editorial_skill, "EDITORIAL_SKILL_DIR", skill_dir)

    assert "editorial one" in editorial_skill.load_editorial_skill()
    assert "must stay unloaded" not in editorial_skill.load_editorial_skill()
    skill_path.write_text("editorial two", encoding="utf-8")
    assert "editorial two" in editorial_skill.load_editorial_skill()


def test_editorial_loader_fails_explicitly_for_missing_or_empty_skill(tmp_path, monkeypatch):
    skill_dir = tmp_path / "editorial"
    monkeypatch.setattr(editorial_skill, "EDITORIAL_SKILL_DIR", skill_dir)
    with pytest.raises(FileNotFoundError):
        editorial_skill.load_editorial_skill()

    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(" \n", encoding="utf-8")
    with pytest.raises(ValueError, match="skill is empty"):
        editorial_skill.load_editorial_skill()


@pytest.mark.parametrize(
    ("module", "path_attr", "loader"),
    [
        (research_skill, "PREGAME_RESEARCH_SKILL_PATH", research_skill.load_pregame_research_skill),
        (forecasting_skill, "FORECASTING_SKILL_PATH", forecasting_skill.load_forecasting_skill),
    ],
)
def test_missing_or_empty_skill_fails_explicitly(tmp_path, monkeypatch, module, path_attr, loader):
    skill_path = tmp_path / "missing" / "SKILL.md"
    monkeypatch.setattr(module, path_attr, skill_path)
    with pytest.raises(FileNotFoundError):
        loader()

    skill_path.parent.mkdir()
    skill_path.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="skill is empty"):
        loader()


def test_researcher_loads_only_research_skill_before_search(tmp_path, monkeypatch):
    skill_path = tmp_path / "research.md"
    skill_path.write_text("research skill marker", encoding="utf-8")
    monkeypatch.setattr(research_skill, "PREGAME_RESEARCH_SKILL_PATH", skill_path)
    captured = []

    def client(stage, messages, tools, required):
        captured.append((stage, messages[0]["content"]))
        return [{"name": "search_pregame_web", "args": {"categories": ["game_story"], "reason": "test"}}]

    searcher = SimpleNamespace(search=lambda *args, **kwargs: {"status": "empty", "records": []})
    _, trace = AutonomousWebResearcher(searcher=searcher, root=tmp_path / "runs", client=client).investigate(
        {"game_id": "fixture"},
        "2026-10-20T18:55:00Z",
        categories={"game_story"},
    )

    assert captured and captured[0][0] == "search"
    prompt = captured[0][1]
    assert "research skill marker" in prompt
    assert "forecasting_skill" not in prompt
    assert "editorial_skill" not in prompt
    attempt = json.loads((Path(trace["path"]) / "attempt.json").read_text(encoding="utf-8"))
    assert attempt["skill_hashes"]["pregame_research"] == hashlib.sha256(
        b"research skill marker"
    ).hexdigest()


def test_writer_skill_scope_order_and_web_default_only(tmp_path, monkeypatch):
    editorial_dir = tmp_path / "editorial"
    editorial_dir.mkdir()
    (editorial_dir / "SKILL.md").write_text("editorial marker", encoding="utf-8")
    monkeypatch.setattr(editorial_skill, "EDITORIAL_SKILL_DIR", editorial_dir)
    forecast_path = tmp_path / "forecasting.md"
    forecast_path.write_text("forecasting marker", encoding="utf-8")
    monkeypatch.setattr(forecasting_skill, "FORECASTING_SKILL_PATH", forecast_path)
    context = build_analyst_context(LAYERS, [], missing_data=[])

    generic_prompt = EvidenceBoundAnalyst._messages(context)[0]["content"]
    assert "editorial marker" in generic_prompt
    assert "forecasting marker" not in generic_prompt

    web_prompt = EvidenceBoundAnalyst._messages(context)[0]["content"]
    assert "forecasting marker" not in web_prompt
    assert "forecasting marker" in forecasting_skill.load_forecasting_skill()
    assert "cutoff" in web_prompt
    assert "evidence" in web_prompt and "引用ID" in web_prompt
    assert "Official数值" in web_prompt
    assert "调用方schema" in web_prompt
    assert web_prompt.index("调用方schema") < web_prompt.index("<editorial_skill ")

    forecast_path.write_text("forecasting marker updated", encoding="utf-8")
    (editorial_dir / "SKILL.md").write_text("editorial marker updated", encoding="utf-8")
    updated_prompt = EvidenceBoundAnalyst._messages(context)[0]["content"]
    assert "forecasting marker updated" in forecasting_skill.load_forecasting_skill()
    assert "forecasting marker updated" not in updated_prompt
    assert "forecasting marker\n" not in updated_prompt
    assert "editorial marker updated" in updated_prompt
    base = web_prompt.split("<editorial_skill ", 1)[0]
    for protocol in ("schema", "身份", "cutoff", "evidence", "引用ID", "Official数值", "数字单位/方向", "模型解释", "投注执行"):
        assert protocol in base
    for method in ("故事优先", "市场校验器", "400～600", "主线", "模型只是 baseline"):
        assert method not in base

    service = SavedGameAnalysisService({"games": []})
    assert service.analyst.include_forecasting_skill is True


def test_research_and_fetch_share_research_skill_and_reload_next_run(tmp_path, monkeypatch):
    skill_path = tmp_path / "research.md"
    monkeypatch.setattr(research_skill, "PREGAME_RESEARCH_SKILL_PATH", skill_path)
    calls = []

    def client(stage, messages, tools, required):
        calls.append((stage, messages[0]["content"]))
        if stage == "fetch":
            assert [tool["function"]["name"] for tool in tools] == ["fetch_sources"]
            assert "URL" in messages[0]["content"]
            assert "sources" in json.loads(messages[-1]["content"])
            return []
        return [{"name": "search_pregame_web", "args": {"categories": ["game_story"], "reason": "fixture"}}]

    searcher = SimpleNamespace(search=lambda *args, **kwargs: {
        "status": "available", "records": [{
            "id": "story", "category": "game_story", "url": "https://www.nba.com/example",
            "snippet": "fixture source", "published_at": "2026-09-01T00:00:00Z",
        }],
    })
    researcher = AutonomousWebResearcher(searcher=searcher, root=tmp_path / "runs", client=client)
    for version in ("research-v1", "research-v2"):
        skill_path.write_text(version, encoding="utf-8")
        researcher.investigate({"game_id": "fixture"}, "2026-10-20T18:55:00Z", categories={"game_story"})
        assert [stage for stage, _ in calls[-2:]] == ["search", "fetch"]
        for _, prompt in calls[-2:]:
            assert version in prompt
            assert "forecasting_skill" not in prompt and "editorial_skill" not in prompt
            assert "cutoff" in prompt and "search_pregame_web" in prompt
            for old_method in ("系列赛数据尚缺", "故事优先", "高关注球星", "固定的研究问题"):
                assert old_method not in prompt


@pytest.mark.parametrize("skill_name", ["research", "forecasting", "editorial"])
def test_missing_skill_stops_model_transport(tmp_path, monkeypatch, skill_name):
    def must_not_call(*args, **kwargs):
        pytest.fail("missing skill reached model transport")

    if skill_name == "research":
        monkeypatch.setattr(research_skill, "PREGAME_RESEARCH_SKILL_PATH", tmp_path / "missing.md")
        with pytest.raises(FileNotFoundError):
            AutonomousWebResearcher(searcher=SimpleNamespace(), root=tmp_path / "runs", client=must_not_call).investigate(
                {"game_id": "fixture"}, "2026-10-20T18:55:00Z", categories={"game_story"},
            )
    else:
        if skill_name == "forecasting":
            monkeypatch.setattr(forecasting_skill, "FORECASTING_SKILL_PATH", tmp_path / "missing.md")
        else:
            monkeypatch.setattr(editorial_skill, "EDITORIAL_SKILL_DIR", tmp_path / "missing")
        with pytest.raises(FileNotFoundError):
            EvidenceBoundAnalyst(root=tmp_path / "calls", client=must_not_call, include_forecasting_skill=True).judge(
                build_analyst_context(LAYERS, [], missing_data=[])
            )


@pytest.mark.parametrize("loader", [research_skill.load_pregame_research_skill,
                                    forecasting_skill.load_forecasting_skill,
                                    editorial_skill.load_editorial_skill])
def test_read_error_is_not_silently_replaced(monkeypatch, loader):
    def denied(*args, **kwargs):
        raise PermissionError("fixture skill read denied")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="fixture skill read denied"):
        loader()


def test_generic_agent_keeps_its_default_writer_and_direct_search(tmp_path, monkeypatch):
    from . import analyst as analyst_module
    from .agent import PregameAgent, Session

    monkeypatch.setattr(editorial_skill, "load_editorial_skill", lambda: "<editorial_skill>editorial fixture</editorial_skill>")
    def excluded_skill():
        pytest.fail("generic default unexpectedly loaded Research or Forecasting Skill")
    monkeypatch.setattr(research_skill, "load_pregame_research_skill", excluded_skill)
    monkeypatch.setattr(forecasting_skill, "load_forecasting_skill", excluded_skill)
    seen = []
    def client(messages, schema):
        seen.append(messages)
        assert schema.__name__ == "AnalystJudgment"
        context = json.loads(messages[-1]["content"])
        return {
            "winner_lean": "no_lean", "side_lean": "no_lean", "total_lean": "no_lean",
            "confidence": "low", "supporting_evidence_ids": [context["objective_evidence"][0]["id"]],
            "baseline_agreement": {"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
            "subjective_rationale": "generic fixture",
        }, {}

    class LocalWriter(EvidenceBoundAnalyst):
        def __init__(self):
            super().__init__(root=tmp_path / "calls", client=client)
            assert self.include_forecasting_skill is False

    monkeypatch.setattr(analyst_module, "EvidenceBoundAnalyst", LocalWriter)
    agent = PregameAgent.__new__(PregameAgent)
    agent.analyst = None
    agent.predictor = SimpleNamespace(source_paths=[], demo_context=None)
    layers = {**LAYERS, "market_usage": {"status": "unavailable"},
              "official_market_adjustment": None, "residual_model": {},
              "prediction_method": "pure_basketball_fallback"}
    agent.residual_predictor = SimpleNamespace(predict=lambda *args, **kwargs: layers)
    (tmp_path / "model_reference.json").write_text("{}", encoding="utf-8")
    state = {"game": {"game_id": "fixture"}, "cutoff": "2026-10-20T18:55:00Z", "session": Session(),
             "missing": [], "trace": [], "question": "generic fixture", "generate_judgment": True,
             "data": {"prediction": {"prediction": LAYERS["pure_prediction"], "snapshot": {}, "path": str(tmp_path)}}}
    report = agent._select_evidence(state)["report"]
    assert seen and "editorial fixture" in seen[0][0]["content"]
    assert "forecasting_skill" not in seen[0][0]["content"]
    assert report["prediction"] == report["official_numeric_prediction"] == LAYERS["official_numeric_prediction"]
    assert report["analyst_judgment"] is not None

    search_calls = []
    def direct_search(*args, **kwargs):
        search_calls.append(kwargs)
        return {"status": "unavailable", "records": []}
    agent.web_searcher = SimpleNamespace(search=direct_search)
    agent._lookup({**state, "data": {}, "replay": False})
    assert len(search_calls) == 1
    assert search_calls[0]["categories"] == {"availability", "market"}
