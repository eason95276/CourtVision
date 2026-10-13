"""Expose one verified Editorial output to the unchanged Pregame rubric.

This adapter neither generates outputs nor opens the twenty-game workflow.
It uses its own Label Studio database and per-sample annotation ledger.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import Control, exclusive, now
from asset_scanner import EVAL, canonical_hash, read_json, sha
from evaluation_integrity import DATASET_ID, GOLDEN_ID
from workspace import clean_pregame_task, listening, stop_owned

HERE = Path(__file__).resolve().parent
GOLD = EVAL / "golden_set/v1"
RUNTIME = EVAL / "label_studio/runtime"
FOLDER = RUNTIME / "pregame_first"
PORT = 8684
SAMPLE_ID = "cb-10c3baf5823776e51d36"
WORKSPACE = "pregame_single"
TITLE = "CourtVision Pregame First Sample Golden V1"
EDITORIAL_FIELDS = {"title", "lead", "article", "watch_themes", "confidence", "player_analysis"}


def load_verified_chain():
    # Generation owns the full hash check. Do not load a partial component,
    # selection manifest, old import, other sample, or production output here.
    from two_stage_first import load_chain
    return load_chain()


def _editorial_hashes(manifest):
    component = manifest.get("components", {}).get("editorial", {})
    result = {}
    for key in ("artifact_hash", "response_hash"):
        value = manifest.get("editorial_" + key, component.get(key))
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("verified Editorial " + key + " missing")
        result["editorial_" + key] = value
    return result


def context_summary(context):
    """Same summary contract as the original UI, using only this Context."""
    matchup = context["matchup"]
    lines = [matchup["away_team"] + " @ " + matchup["home_team"] + " · " + matchup["game_date"],
             "下面是历史事件重建，不是当前确认阵容。盘口、伤病、首发与目标availability均未提供。"]
    for side in ("team_a", "team_b"):
        team = context[side]
        record = team["record"]
        recent = team["recent_form"]["recent_5"]
        previous = team["previous_season"]
        lines.append(team["team_name"] + "：赛前已观察战绩 " + str(record.get("wins_before_game"))
                     + "胜/" + str(record.get("losses_before_game")) + "负；近5窗口实际 "
                     + str(recent["sample_games"]) + " 场；上一赛季 "
                     + str(previous.get("sample_games")) + " 场（" + previous["status"] + "）。")
    lines.append("完整原始Context在下方折叠区；摘要不是另一套模型输入。")
    return "\n".join(lines)


def build_task(chain):
    """Positive projection: only this Context and the final Writer JSON."""
    manifest, context, editorial = chain["manifest"], chain["context"], chain["editorial"]
    if manifest.get("sample_id") != SAMPLE_ID:
        raise ValueError("only the authorized first sample may be exposed")
    if manifest.get("dataset_revision") != DATASET_ID or manifest.get("golden_set_revision") != GOLDEN_ID:
        raise ValueError("first-sample revision mismatch")
    if canonical_hash(context) != manifest.get("context_hash"):
        raise ValueError("first-sample Context binding mismatch")
    matchup = context["matchup"]
    if (matchup.get("game_date") != "2025-10-29" or matchup.get("away_team") != "Los Angeles Lakers"
            or matchup.get("home_team") != "Minnesota Timberwolves"
            or matchup.get("target_game_id") != manifest.get("game_id")):
        raise ValueError("first-sample game identity mismatch")
    if not isinstance(editorial, dict) or set(editorial) != EDITORIAL_FIELDS:
        raise ValueError("final EditorialDraft fields required")
    if not isinstance(editorial.get("article"), str) or not editorial["article"].strip():
        raise ValueError("final Editorial article missing")
    revision = manifest.get("generation_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", revision):
        raise ValueError("invalid generation revision")
    rubric = read_json(GOLD / "rubric.json")
    metadata = {"sample_id": SAMPLE_ID, "dataset_revision": DATASET_ID,
                "golden_set_revision": GOLDEN_ID, "rubric_revision": "human-rubric-v1-" + canonical_hash(rubric)[:20],
                "stage": "pregame", "generation_revision": revision, "context_hash": manifest["context_hash"],
                "editorial_output_hash": canonical_hash(editorial), **_editorial_hashes(manifest)}
    task = {"data": {"sample_id": SAMPLE_ID, "stage": "pregame", "context": copy.deepcopy(context),
                     "context_json": json.dumps(context, ensure_ascii=False, indent=2),
                     "context_summary": context_summary(context), "model_output": copy.deepcopy(editorial),
                     "model_output_status": "accepted", "model_output_display": json.dumps(editorial, ensure_ascii=False, indent=2),
                     "stage_notice": "请评价当前最终赛前输出；所有控件无默认评分。"}, "meta": metadata}
    clean_pregame_task(task)
    return task


def api(route, body=None, method=None):
    credentials = read_json(FOLDER / "credentials.json")
    request = urllib.request.Request("http://127.0.0.1:" + str(PORT) + route,
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None,
        headers={"Authorization": "Token " + credentials["token"], "Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read()) if response.status != 204 else None


def _ensure_table(control):
    with control.tx() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS single_sample_annotations (
                sample_id TEXT PRIMARY KEY, generation_revision TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ANNOTATING','FROZEN')),
                identity_hash TEXT NOT NULL, task_hash TEXT NOT NULL,
                task_id INTEGER NOT NULL, receipt TEXT);
            CREATE TRIGGER IF NOT EXISTS single_sample_frozen_no_update
            BEFORE UPDATE ON single_sample_annotations WHEN OLD.status='FROZEN'
            BEGIN SELECT RAISE(ABORT,'single sample annotation is frozen'); END;
            CREATE TRIGGER IF NOT EXISTS single_sample_no_delete
            BEFORE DELETE ON single_sample_annotations
            BEGIN SELECT RAISE(ABORT,'single sample annotation is retained'); END;
        """)


def _row(control):
    _ensure_table(control)
    with control.tx() as db:
        row = db.execute("SELECT * FROM single_sample_annotations WHERE sample_id=?", (SAMPLE_ID,)).fetchone()
    return dict(row) if row else None


def _start_server():
    # Shut down the known twenty-task views without touching their databases.
    stop_owned(8681, RUNTIME)
    stop_owned(8682, RUNTIME / "pregame")
    # A pre-existing listener must pass the same ownership check before reuse.
    if listening(PORT):
        stop_owned(PORT, FOLDER)
    FOLDER.mkdir(parents=True, exist_ok=True)
    credential_path = FOLDER / "credentials.json"
    if not credential_path.exists():
        original = read_json(RUNTIME / "credentials.json")
        exclusive(credential_path, {"username": original["username"], "password": original["password"],
                                    "token": secrets.token_hex(32)})
    credentials = read_json(credential_path)
    env = os.environ.copy()
    env.update(LABEL_STUDIO_DISABLE_TELEMETRY="true", SENTRY_DSN="")
    command = [str(EVAL / "label_studio/.venv/Scripts/label-studio.exe"), "start", "--no-browser",
               "--internal-host", "127.0.0.1", "--port", str(PORT), "--data-dir", str(FOLDER),
               "--username", credentials["username"], "--password", credentials["password"],
               "--user-token", credentials["token"], "--enable-legacy-api-token"]
    with (FOLDER / "workbench.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=EVAL, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    exclusive(FOLDER / ("process-" + secrets.token_hex(6) + ".json"),
              {"pid": process.pid, "port": PORT, "started_at": now()})
    for _ in range(60):
        if listening(PORT):
            break
        if process.poll() is not None:
            raise ValueError("first-sample Label Studio startup failed; inspect private log")
        time.sleep(1)
    else:
        raise ValueError("first-sample Label Studio startup timeout")
    for _ in range(20):
        try:
            return api("/api/projects/")
        except (OSError, ValueError):
            time.sleep(1)
    raise ValueError("first-sample workspace API unavailable")


def _export(identity):
    return api(f"/api/projects/{identity['project_id']}/export?exportType=JSON&download_all_tasks=true")


def _verify_tasks(tasks, wanted, task_id=None):
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise ValueError("first-sample workspace must contain exactly one task")
    task = tasks[0]
    if task.get("data") != wanted["data"] or task.get("meta") != wanted["meta"]:
        raise ValueError("first-sample live task differs from verified Editorial binding")
    if task.get("predictions"):
        raise ValueError("first-sample workspace cannot contain automatic labels")
    if task_id is not None and task.get("id") != task_id:
        raise ValueError("first-sample Label Studio task identity changed")
    clean_pregame_task({"data": task["data"], "meta": task["meta"]})
    return task


def open_annotations(control, actor="local-operator"):
    chain = load_verified_chain()
    wanted = build_task(chain)
    row = _row(control)
    if row and row["status"] == "FROZEN":
        raise ValueError("frozen first-sample workspace cannot reopen silently")
    if row and (row["generation_revision"] != wanted["meta"]["generation_revision"]
                or row["task_hash"] != canonical_hash(wanted)):
        raise ValueError("first-sample accepted output binding changed")
    try:
        response = _start_server()
        projects = response.get("results", []) if isinstance(response, dict) else response
        if len(projects) > 1 or any(p["title"] != TITLE for p in projects):
            raise ValueError("unexpected project in first-sample instance")
        xml = (GOLD / "pregame_config.xml").read_text(encoding="utf-8")
        project = projects[0] if projects else api("/api/projects/", {"title": TITLE, "label_config": xml,
            "description": "One frozen Context and one final Pregame output. Human labels have no defaults."})
        project = api(f"/api/projects/{project['id']}/")
        if project["label_config"].strip() != xml.strip():
            raise ValueError("Pregame rubric UI drift")
        if project["task_number"] == 0:
            if row:
                raise ValueError("recorded first-sample task disappeared")
            api(f"/api/projects/{project['id']}/import", [wanted])
        identity = {"stage": WORKSPACE, "sample_id": SAMPLE_ID,
                    "instance": str(FOLDER.relative_to(EVAL)), "port": PORT, "project_id": project["id"],
                    "url": f"http://127.0.0.1:{PORT}/projects/{project['id']}/data",
                    "generation_revision": wanted["meta"]["generation_revision"],
                    "manifest_hash": chain["manifest"]["manifest_hash"],
                    "context_hash": wanted["meta"]["context_hash"],
                    "editorial_output_hash": wanted["meta"]["editorial_output_hash"],
                    **_editorial_hashes(chain["manifest"])}
        task = _verify_tasks(_export(identity), wanted, row["task_id"] if row else None)
        if not row and task.get("annotations"):
            raise ValueError("unrecorded first-sample human labels cannot be adopted silently")
        identity["task_id"] = task["id"]
        old = control.workspace(WORKSPACE)
        if old and old != identity:
            raise ValueError("first-sample workspace identity changed")
        if row and row["identity_hash"] != canonical_hash(identity):
            raise ValueError("first-sample workspace ledger identity changed")
        control.workspace(WORKSPACE, identity)
        with control.tx() as db:
            current = db.execute("SELECT * FROM single_sample_annotations WHERE sample_id=?", (SAMPLE_ID,)).fetchone()
            if current and current["status"] == "FROZEN":
                raise ValueError("first-sample was frozen concurrently")
            if current is None:
                db.execute("INSERT INTO single_sample_annotations VALUES(?,?,?,?,?,?,NULL)",
                    (SAMPLE_ID, wanted["meta"]["generation_revision"], "ANNOTATING", canonical_hash(identity), canonical_hash(wanted), task["id"]))
                control.audit("annotation_started", actor,
                              {"stage": "pregame", "scope": "single_sample", "sample_id": SAMPLE_ID, "workspace": identity}, db)
        return identity
    except BaseException:
        stop_owned(PORT, FOLDER)
        raise


def _normalize_task(task, rubric):
    from export_annotations import individual
    annotations = [annotation for annotation in task.get("annotations", []) if not annotation.get("was_cancelled")]
    if not annotations:
        raise ValueError("one real completed human annotation is required")
    humans = []
    for annotation in annotations:
        completed_by = annotation.get("completed_by")
        user_id = completed_by.get("id") if isinstance(completed_by, dict) else completed_by
        if user_id is None or not str(user_id).strip() or not annotation.get("created_at") or annotation.get("id") is None:
            raise ValueError("actual annotator and annotation identity required")
        names = [item["from_name"] for item in annotation["result"]]
        if len(names) != len(set(names)):
            raise ValueError("duplicate human rubric field")
        human = individual(annotation, rubric)
        if set(human["error_tags"]) - set(rubric["error_tags"]):
            raise ValueError("unknown human error tag")
        humans.append(human)
    humans.sort(key=lambda item: item["annotator_id"])
    if len({human["annotator_id"] for human in humans}) != len(humans):
        raise ValueError("same-person re-annotation requires explicit batch selection")
    if len(humans) > 2:
        raise ValueError("more than two people requires explicit adjudication")
    metadata = task["meta"]
    label = {key: metadata[key] for key in ("sample_id", "stage", "dataset_revision", "golden_set_revision", "rubric_revision")}
    label["annotator_a"] = humans[0]
    disagreements = []
    if len(humans) == 2:
        label["annotator_b"] = humans[1]
        for field in ("hard_gates", "soft_metrics", "error_tags"):
            if humans[0][field] != humans[1][field]:
                disagreements.append({"sample_id": SAMPLE_ID, "stage": "pregame", "field": field,
                    "annotator_a": humans[0][field], "annotator_b": humans[1][field], "adjudicated": None})
    return [label], disagreements


def freeze_annotations(control, actor="local-operator"):
    chain = load_verified_chain()
    wanted = build_task(chain)
    row = _row(control)
    if not row or row["status"] != "ANNOTATING":
        raise ValueError("first-sample annotation workspace is not open")
    identity = control.workspace(WORKSPACE)
    if not identity or canonical_hash(identity) != row["identity_hash"] or canonical_hash(wanted) != row["task_hash"]:
        raise ValueError("first-sample freeze binding changed")
    project = api(f"/api/projects/{identity['project_id']}/")
    if project["label_config"].strip() != (GOLD / "pregame_config.xml").read_text(encoding="utf-8").strip():
        raise ValueError("Pregame rubric UI drift")
    tasks = _export(identity)
    task = _verify_tasks(tasks, wanted, row["task_id"])
    labels, disagreements = _normalize_task(task, read_json(GOLD / "rubric.json")["pregame"])
    receipt = {"sample_id": SAMPLE_ID, "stage": "pregame", "scope": "single_sample",
        "golden_set_revision": GOLDEN_ID, "dataset_revision": DATASET_ID,
        "generation_revision": row["generation_revision"], "annotation_revision": 1,
        "context_hash": wanted["meta"]["context_hash"], "chain_manifest_hash": chain["manifest"]["manifest_hash"],
        "editorial_output_hash": wanted["meta"]["editorial_output_hash"], **_editorial_hashes(chain["manifest"]),
        "rubric_revision": wanted["meta"]["rubric_revision"], "annotation_export_hash": canonical_hash(tasks),
        "normalized_labels_hash": canonical_hash(labels),
        "annotator_identities": sorted({label[name]["annotator_id"] for label in labels
                                       for name in ("annotator_a", "annotator_b") if name in label}),
        "task_count": 1, "completed_count": 1, "timestamp": now(), "workspace": identity,
        "next_sample_unlocked": False, "postgame_unlocked": False}
    receipt["receipt_hash"] = canonical_hash(receipt)
    path = HERE / "annotations/first-sample" / (SAMPLE_ID + "-pregame-" + row["generation_revision"] + ".json")
    document = {"receipt": receipt, "raw_export": tasks, "labels": labels, "disagreements": disagreements}
    with control.tx() as db:
        current = db.execute("SELECT status FROM single_sample_annotations WHERE sample_id=?", (SAMPLE_ID,)).fetchone()
        if not current or current["status"] != "ANNOTATING":
            raise ValueError("illegal first-sample freeze phase")
        exclusive(path, document)
        receipt = {**receipt, "export_path": str(path), "export_file_sha256": sha(path),
                   "export_document_hash": canonical_hash(document)}
        db.execute("UPDATE single_sample_annotations SET status='FROZEN',receipt=? WHERE sample_id=?",
                   (json.dumps(receipt, ensure_ascii=False), SAMPLE_ID))
        control.audit("annotation_frozen", actor, receipt, db)
    stop_owned(PORT, FOLDER)
    return receipt


def status(control):
    row = _row(control)
    result = {"sample_id": SAMPLE_ID, "status": row["status"] if row else "NOT_OPEN",
              "workspace": control.workspace(WORKSPACE), "global_phase": control.state()["phase"],
              "next_sample_unlocked": False, "postgame_unlocked": False}
    if row and row["receipt"]:
        receipt = json.loads(row["receipt"])
        if sha(Path(receipt["export_path"])) != receipt["export_file_sha256"]:
            raise ValueError("immutable first-sample annotation export changed")
        result["receipt"] = receipt
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("open", "freeze", "status"))
    parser.add_argument("--actor", default="local-operator")
    args = parser.parse_args()
    control = Control()
    if args.command == "open":
        result = open_annotations(control, args.actor)
    elif args.command == "freeze":
        result = freeze_annotations(control, args.actor)
    else:
        result = status(control)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
