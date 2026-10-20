"""Title-only style policy, with real guards and no network calls."""
import copy
import json

import pytest

from .editorial_pipeline import EditorialDraft, run_editorial
from .editorial_titles import TitleRevision, normalize_poem_title
from .test_editorial_pipeline import writer, SHORT_ARTICLE


TITLE = "前场对阵看篮板\n后场交锋争球权"


@pytest.fixture
def title_case():
    from .analyst import build_analyst_context
    from .test_analyst import EVIDENCE, LAYERS
    context = build_analyst_context(copy.deepcopy(LAYERS), copy.deepcopy(EVIDENCE), missing_data=[],
        analysis_request='生成第25秒赛前分析。', game={'game_id':'offline-title-case',
            'home':{'id':1610612761,'name':'Toronto Raptors','city':'Toronto'},
            'away':{'id':1610612748,'name':'Miami Heat'},'game_type':'preseason',
            'venue':{'fullName':'Videotron Centre','address':{'city':'Quebec City'}}})
    ref = next(iter(context.evidence_index))
    forecast = {'final_prediction':{'winner_side':'home','win_probability':.55,
        'expected_home_margin':3.0,'expected_total':220.0},
        'review':{'factors':[{'classification':'inference','effect':'home',
            'evidence_ids':[ref],'summary':'观察半场进攻和前场对位。'}],'pregame_plan':None}}
    return context, forecast


@pytest.mark.parametrize("separator", ["\n", "\r\n", "／", "，", "", " / "])
def test_only_separators_are_normalized(separator):
    assert normalize_poem_title(separator.join(TITLE.splitlines())) == TITLE


@pytest.mark.parametrize("value", ["", "六个汉字而已\n后场交锋争球权", "八个汉字才够用吗\n后场交锋争球权",
    "76人再战纽约\n后场交锋争球权", "<b>前场对阵看篮板</b>\n后场交锋争球权"])
def test_invalid_titles_are_never_truncated_or_padded(value):
    assert normalize_poem_title(value) is None


def title_client(initial=TITLE, revised=TITLE):
    calls = []
    def client(messages, schema):
        calls.append(schema.__name__)
        if schema is TitleRevision:
            assert "只改标题" in messages[0]["content"]
            return {"title": revised}, {}
        assert schema is EditorialDraft
        value, receipt = writer(messages, schema)
        value["title"] = initial
        return value, receipt
    return client, calls


def test_valid_title_uses_no_additional_model_call(title_case, tmp_path):
    context, forecast = title_case
    client, calls = title_client()
    judgment, trace = run_editorial(context, forecast, tmp_path / "valid", writer_client=client)
    assert calls == ["EditorialDraft"]
    assert trace["title_policy"]["status"] == "passed"
    assert judgment["pregame_story"]["title"] == TITLE
    assert judgment["analysis_article"] == SHORT_ARTICLE


def test_one_title_only_revision_preserves_all_other_judgment_fields(title_case, tmp_path):
    context, forecast = title_case
    plain, _ = title_client(initial="旧的普通标题")
    baseline, _ = run_editorial(context, forecast, tmp_path / "legacy", writer_client=plain, require_poem_title=False)
    client, calls = title_client(initial="旧的普通标题")
    judgment, trace = run_editorial(context, forecast, tmp_path / "revised", writer_client=client)
    assert calls == ["EditorialDraft", "TitleRevision"]
    assert trace["editorial_calls"] == 2
    assert trace["title_policy"]["revision_calls"] == 1
    expected = copy.deepcopy(baseline)
    expected["pregame_story"]["title"] = TITLE
    assert judgment == expected
    assert json.loads((tmp_path / "revised/title_revision_request.json").read_text(encoding="utf-8"))["max_tokens"] == 300


def test_title_budget_exhaustion_does_not_start_a_revision(title_case, tmp_path):
    context, forecast = title_case
    client, calls = title_client(initial="旧的普通标题")
    with pytest.raises(ValueError, match="title_correction_budget_unavailable"):
        run_editorial(context, forecast, tmp_path / "budget", writer_client=client, max_calls=1)
    assert calls == ["EditorialDraft"]


def test_second_bad_title_is_rejected_without_looping(title_case, tmp_path):
    context, forecast = title_case
    client, calls = title_client(initial="旧的普通标题", revised="还是没写对")
    with pytest.raises(ValueError, match="title_format_invalid_after_one_revision"):
        run_editorial(context, forecast, tmp_path / "bad", writer_client=client)
    assert calls == ["EditorialDraft", "TitleRevision"]


def test_new_title_cannot_bypass_factual_guards(title_case, tmp_path):
    context, forecast = title_case
    context = context.model_copy(deep=True)
    context.game['venue'] = {'fullName': 'Videotron Centre', 'address': {'city': 'Quebec City'}}
    context.game['home']['city'] = 'Toronto'
    client, calls = title_client(initial="旧的普通标题", revised="在多伦多打揭幕\n双方今夜争高下")
    with pytest.raises(ValueError, match="title_factual_blocker"):
        run_editorial(context, forecast, tmp_path / "facts", writer_client=client)
    assert calls == ["EditorialDraft", "TitleRevision"]
