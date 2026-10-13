"""Synthetic one-sample adapter checks; no server, provider, or label submission."""
import copy
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import first_sample_workspace as workspace
from asset_scanner import canonical_hash, read_json, sha
from control import Control


@pytest.fixture
def chain():
    context = {
        "matchup": {"target_game_id": "nba:SYNTHETIC", "game_date": "2025-10-29",
                    "home_team": "Minnesota Timberwolves", "away_team": "Los Angeles Lakers"},
        "team_a": {"team_name": "Minnesota Timberwolves", "record": {"wins_before_game": 2, "losses_before_game": 1},
                   "recent_form": {"recent_5": {"sample_games": 3}}, "previous_season": {"sample_games": None, "status": "unavailable"}},
        "team_b": {"team_name": "Los Angeles Lakers", "record": {"wins_before_game": 2, "losses_before_game": 2},
                   "recent_form": {"recent_5": {"sample_games": 4}}, "previous_season": {"sample_games": None, "status": "unavailable"}},
    }
    editorial = {"title": "SYNTHETIC TEST ONLY", "lead": "Synthetic lead", "article": "Synthetic final Writer article.",
                 "watch_themes": [], "confidence": "low", "player_analysis": []}
    return {"context": context, "editorial": editorial,
            "forecast": {"never_visible": "INTERMEDIATE_FORECAST_SENTINEL"},
            "manifest": {"sample_id": workspace.SAMPLE_ID, "game_id": "nba:SYNTHETIC",
                         "dataset_revision": workspace.DATASET_ID, "golden_set_revision": workspace.GOLDEN_ID,
                         "generation_revision": "synthetic-unit-revision", "context_hash": canonical_hash(context),
                         "manifest_hash": "a" * 64, "editorial_artifact_hash": "b" * 64,
                         "components": {"editorial": {"response_hash": "c" * 64},
                                        "forecast": {"private_data": "INTERMEDIATE_FORECAST_SENTINEL"}}}}


@pytest.fixture
def environment(tmp_path, monkeypatch, chain):
    control = Control(tmp_path / "control.sqlite")
    evaluation = tmp_path / "evaluation"
    monkeypatch.setattr(workspace, "EVAL", evaluation)
    monkeypatch.setattr(workspace, "HERE", evaluation / "execution")
    monkeypatch.setattr(workspace, "RUNTIME", evaluation / "label_studio/runtime")
    monkeypatch.setattr(workspace, "FOLDER", evaluation / "label_studio/runtime/pregame_first")
    monkeypatch.setattr(workspace, "load_verified_chain", lambda: copy.deepcopy(chain))
    server = {"projects": [], "tasks": [], "imports": [], "starts": 0, "stops": [], "requests": []}
    xml = (workspace.GOLD / "pregame_config.xml").read_text(encoding="utf-8")

    def start():
        server["starts"] += 1
        return copy.deepcopy(server["projects"])

    def api(route, body=None, method=None):
        server["requests"].append((route, copy.deepcopy(body), method))
        if route == "/api/projects/":
            assert body["label_config"] == xml
            project = {"id": 7, "title": body["title"], "label_config": xml, "task_number": 0}
            server["projects"].append(project)
            return copy.deepcopy(project)
        if route == "/api/projects/7/":
            project = copy.deepcopy(server["projects"][0])
            project["task_number"] = len(server["tasks"])
            return project
        if route == "/api/projects/7/import":
            assert method is None
            server["imports"].append(copy.deepcopy(body))
            server["tasks"] = [{**copy.deepcopy(task), "id": 77, "annotations": [], "predictions": []} for task in body]
            return {"task_count": len(body)}
        if route.startswith("/api/projects/7/export?"):
            return copy.deepcopy(server["tasks"])
        raise AssertionError("unscoped adapter API route: " + route)

    monkeypatch.setattr(workspace, "_start_server", start)
    monkeypatch.setattr(workspace, "api", api)
    monkeypatch.setattr(workspace, "stop_owned", lambda port, folder: server["stops"].append((port, folder)))
    return control, server


def annotation(user=39, annotation_id=103):
    rubric = read_json(workspace.GOLD / "rubric.json")["pregame"]
    result = [{"from_name": name, "value": {"choices": ["PASS"]}} for name in rubric["hard_gates"]]
    result += [{"from_name": name, "value": {"choices": ["3"]}} for name in rubric["soft_metrics"]]
    return {"id": annotation_id, "completed_by": {"id": user}, "created_at": "2026-10-11T00:00:00Z",
            "result": result, "was_cancelled": False}


def test_positive_projection_only_final_writer(chain):
    task = workspace.build_task(chain)
    assert task["data"]["model_output"] == chain["editorial"]
    assert task["data"]["context"] == chain["context"]
    assert task["meta"]["editorial_output_hash"] == canonical_hash(chain["editorial"])
    encoded = json.dumps(task)
    assert "INTERMEDIATE_FORECAST_SENTINEL" not in encoded
    assert "manifest_hash" not in task["meta"]
    assert "forecast" not in task["data"]
    assert "hidden_outcome_bucket" not in encoded
    assert "selection_bucket" not in encoded


@pytest.mark.parametrize("mutation", ["sample", "dataset", "context", "game", "extra_editorial", "response_hash"])
def test_chain_binding_failure_prevents_start(chain, environment, mutation):
    control, server = environment
    if mutation == "sample":
        chain["manifest"]["sample_id"] = "second-sample"
    elif mutation == "dataset":
        chain["manifest"]["dataset_revision"] = "other-revision"
    elif mutation == "context":
        chain["context"]["team_a"]["record"]["wins_before_game"] = 8
    elif mutation == "game":
        chain["manifest"]["game_id"] = "different-game"
    elif mutation == "extra_editorial":
        chain["editorial"]["forecast"] = {"private": True}
    else:
        chain["manifest"]["components"]["editorial"]["response_hash"] = "bad-hash"
    with pytest.raises(ValueError):
        workspace.open_annotations(control, "test")
    assert server["starts"] == 0
    assert control.workspace(workspace.WORKSPACE) is None


def test_hash_loader_failure_has_no_service_effect(environment, monkeypatch):
    control, server = environment
    def broken():
        raise ValueError("synthetic artifact hash failure")
    monkeypatch.setattr(workspace, "load_verified_chain", broken)
    with pytest.raises(ValueError, match="artifact hash"):
        workspace.open_annotations(control, "test")
    assert server["starts"] == 0
    assert not server["stops"]


def test_open_exactly_one_task_preserves_global_gate(environment):
    control, server = environment
    identity = workspace.open_annotations(control, "test")
    assert identity["port"] == 8684 and identity["task_id"] == 77
    assert len(server["projects"]) == len(server["tasks"]) == 1
    assert len(server["imports"]) == 1 and len(server["imports"][0]) == 1
    assert control.workspace(workspace.WORKSPACE) == identity
    assert control.state()["phase"] == "PREPARED"
    assert workspace.status(control)["status"] == "ANNOTATING"
    with pytest.raises(ValueError, match="PREGAME_FROZEN"):
        control.postgame_gate("test")
    with control.tx() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='annotation_started'").fetchone()[0] == 1
    workspace.open_annotations(control, "test")
    assert len(server["imports"]) == 1


def test_blank_label_cannot_freeze(environment):
    control, server = environment
    workspace.open_annotations(control, "test")
    with pytest.raises(ValueError, match="real completed human"):
        workspace.freeze_annotations(control, "test")
    assert workspace.status(control)["status"] == "ANNOTATING"
    assert control.state()["phase"] == "PREPARED"
    assert not list(workspace.HERE.rglob("*pregame*.json"))
    assert not server["stops"]


def test_freeze_real_records_immutable_first_only(environment):
    control, server = environment
    workspace.open_annotations(control, "test")
    server["tasks"][0]["annotations"] = [annotation()]
    receipt = workspace.freeze_annotations(control, "test")
    assert receipt["task_count"] == receipt["completed_count"] == 1
    assert receipt["annotator_identities"] == ["39"]
    assert receipt["annotation_export_hash"] == canonical_hash(server["tasks"])
    assert receipt["next_sample_unlocked"] is False and receipt["postgame_unlocked"] is False
    path = Path(receipt["export_path"])
    assert sha(path) == receipt["export_file_sha256"]
    export = read_json(path)
    assert export["labels"][0]["annotator_a"]["annotation_id"] == 103
    assert export["labels"][0]["annotator_a"]["annotator_id"] == "39"
    assert "sampling_strata" not in export["labels"][0]
    assert "annotator_b" not in export["labels"][0]
    assert workspace.status(control)["status"] == "FROZEN"
    assert server["stops"] == [(8684, workspace.FOLDER)]
    assert control.state()["phase"] == "PREPARED"
    starts = server["starts"]
    with pytest.raises(ValueError, match="cannot reopen"):
        workspace.open_annotations(control, "test")
    assert server["starts"] == starts
    with pytest.raises(ValueError, match="not open"):
        workspace.freeze_annotations(control, "test")
    with control.tx() as db:
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            db.execute("UPDATE single_sample_annotations SET status='ANNOTATING'")
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            db.execute("DELETE FROM single_sample_annotations")


@pytest.mark.parametrize("mutation", ["second_task", "editorial", "context", "meta", "prediction", "task_id", "rubric"])
def test_freeze_rechecks_live_binding(environment, mutation):
    control, server = environment
    workspace.open_annotations(control, "test")
    task = server["tasks"][0]
    task["annotations"] = [annotation()]
    if mutation == "second_task":
        server["tasks"].append(copy.deepcopy(task))
    elif mutation == "editorial":
        task["data"]["model_output"]["article"] += " altered"
    elif mutation == "context":
        task["data"]["context"]["team_a"]["record"]["wins_before_game"] = 4
    elif mutation == "meta":
        task["meta"]["editorial_response_hash"] = "0" * 64
    elif mutation == "prediction":
        task["predictions"] = [{"result": []}]
    elif mutation == "task_id":
        task["id"] = 88
    else:
        server["projects"][0]["label_config"] += " altered"
    with pytest.raises(ValueError):
        workspace.freeze_annotations(control, "test")
    assert workspace.status(control)["status"] == "ANNOTATING"


@pytest.mark.parametrize("mutation", ["cancelled", "missing_gate", "missing_identity", "duplicate_field", "duplicate_annotator", "unknown_tag"])
def test_no_fabricated_or_ambiguous_labels(environment, mutation):
    control, server = environment
    workspace.open_annotations(control, "test")
    human = annotation()
    humans = [human]
    if mutation == "cancelled":
        human["was_cancelled"] = True
    elif mutation == "missing_gate":
        human["result"].pop(0)
    elif mutation == "missing_identity":
        human["completed_by"] = None
    elif mutation == "duplicate_field":
        human["result"].append(copy.deepcopy(human["result"][0]))
    elif mutation == "duplicate_annotator":
        humans.append(annotation(annotation_id=104))
    else:
        human["result"].append({"from_name": "error_tags", "value": {"choices": ["not-in-rubric"]}})
    server["tasks"][0]["annotations"] = humans
    with pytest.raises(ValueError):
        workspace.freeze_annotations(control, "test")
    assert workspace.status(control)["status"] == "ANNOTATING"


def test_genuine_second_annotator_is_preserved(environment):
    control, server = environment
    workspace.open_annotations(control, "test")
    second = annotation(user=40, annotation_id=104)
    second["result"][0]["value"]["choices"] = ["FAIL"]
    server["tasks"][0]["annotations"] = [second, annotation()]
    receipt = workspace.freeze_annotations(control, "test")
    assert receipt["annotator_identities"] == ["39", "40"]
    export = read_json(receipt["export_path"])
    assert export["labels"][0]["annotator_b"]["annotation_id"] == 104
    assert len(export["disagreements"]) == 1


def test_unknown_project_is_closed_without_import(environment):
    control, server = environment
    server["projects"] = [{"id": 7, "title": "unexpected"}]
    with pytest.raises(ValueError, match="unexpected project"):
        workspace.open_annotations(control, "test")
    assert not server["imports"]
    assert server["stops"] == [(8684, workspace.FOLDER)]


def test_api_uses_private_auth_and_single_port(tmp_path, monkeypatch):
    folder = tmp_path / "private"
    folder.mkdir()
    (folder / "credentials.json").write_text(json.dumps({"token": "SYNTHETIC_TEST_TOKEN"}), encoding="utf-8")
    monkeypatch.setattr(workspace, "FOLDER", folder)
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self):
            return b"[]"
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 30
        return Response()
    monkeypatch.setattr(workspace.urllib.request, "urlopen", open_request)
    assert workspace.api("/api/projects/") == []
    assert requests[0].full_url == "http://127.0.0.1:8684/api/projects/"
    assert requests[0].get_header("Authorization") == "Token SYNTHETIC_TEST_TOKEN"
