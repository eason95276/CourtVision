"""Isolated generation worker: preserve the existing pipeline and fence its budget."""
from __future__ import annotations

import copy
import os
import re
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Bound native pools before importing the model stack, including spawned workers.
for _thread_setting in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_thread_setting, "1")

from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError, _instant


def isolate_worker_children():
    """Killing a worker must also close its search-bridge subprocesses."""
    if os.name != "nt":
        os.setsid()
        return
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended(); limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        if handle:
            kernel.CloseHandle(handle)
        raise OSError("worker_process_tree_isolation_failed")
    # Keep the handle for the process lifetime. OS cleanup on termination also
    # kills remaining descendants (for example a blocked uv/MCP bridge).
    global _WORKER_JOB_HANDLE
    _WORKER_JOB_HANDLE = handle


def failure_category(error: BaseException, phase: str | None = None) -> str:
    code = str(error).lower()
    if "tipoff" in code or "pregame_generation_closed" in code:
        return "tipoff_crossed"
    if "schedule_revision" in code or "schedule_changed" in code:
        return "schedule_revision_changed"
    if "policy" in code:
        return "cancelled_by_policy"
    if "lease" in code or "claim_lost" in code:
        return "lease_failure"
    if "deadline" in code or "budget" in code or "stage_expired" in code:
        return "deadline_expired"
    if "validation" in code or "unsupported_" in code or "boundary" in type(error).__name__.lower():
        return "validation_failure"
    return {"claim": "claim_failure", "research": "research_failure",
            "forecasting": "forecasting_failure", "editorial": "editorial_failure",
            "validation": "validation_failure", "publish": "publish_failure"}.get(phase, "pre_execution_failure")


class GuardedVersionStore(PredictionVersionStore):
    """Additional runtime check; the original atomic publication contract remains authoritative."""
    runtime_tasks = None
    runtime_phase_callback = None

    def connect(self):
        connection = super().connect()
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def publish(self, game, request, payload, *, generation_claim=None):
        if generation_claim is not None and self.runtime_tasks is not None:
            if self.clock() >= _instant(game["tipoff"], "scheduled_tipoff"):
                raise VersionStoreError("pregame_generation_closed")
            self.runtime_tasks.assert_claim(generation_claim)
            deadline = _instant(generation_claim["effective_deadline_at"], "effective_deadline_at")
            if self.clock() >= deadline:
                raise VersionStoreError("runtime_deadline_expired")
            if self.runtime_phase_callback is not None:
                self.runtime_phase_callback("publish")
            else:
                self.runtime_tasks.record_phase(generation_claim, "publish")
        return super().publish(game, request, payload, generation_claim=generation_claim)


def production_service(versions, tasks, data, report_phase):
    """Factory used only after the activation gate, never by offline integration tests."""
    from .analysis_service import SavedGameAnalysisService
    from .generation_input import prepare_generation_data
    from .prediction_version_service import PredictionVersionService
    from v2.pregame_system.analyst import EvidenceBoundAnalyst
    from v2.pregame_system.web_research_agent import AutonomousWebResearcher
    from v2.pregame_system.web_search import PregameWebSearch
    from v2.pregame_system import forecasting_workflow, editorial_contract

    # These wrappers live in this dedicated process. They forward all methods,
    # inputs and validators unchanged and only record/check runtime boundaries.
    original_forecast = forecasting_workflow.run_two_stage
    # Instrument an existing hook only. The separate Editorial line owns its
    # contract; no fallback validator or guessed publication receipt is supplied.
    original_validate = getattr(editorial_contract,"validate_editorial_boundaries",None)

    def forecast(*args, **kwargs):
        report_phase("forecasting", minimum_seconds=80)
        result = original_forecast(*args, **kwargs)
        report_phase("editorial", minimum_seconds=40)
        return result

    def validate(*args, **kwargs):
        report_phase("validation")
        result = original_validate(*args, **kwargs)
        report_phase("editorial")
        return result

    forecasting_workflow.run_two_stage = forecast
    if original_validate is not None:
        editorial_contract.validate_editorial_boundaries = validate

    class Analyst(EvidenceBoundAnalyst):
        def judge(self, context, **kwargs):
            if kwargs.get('_locked_forecast'):
                report_phase('editorial',minimum_seconds=40)
            if kwargs.get("_correction"):
                report_phase("editorial", minimum_seconds=40)
            return super().judge(context, **kwargs)

    class Researcher(AutonomousWebResearcher):
        def investigate(self, *args, **kwargs):
            remaining = report_phase("research")
            original = self.acquisition_budget_seconds
            # Reserve room for forecast/editorial without relaxing evidence gates.
            self.acquisition_budget_seconds = min(original, max(1, remaining - 80))
            try:
                return super().investigate(*args, **kwargs)
            finally:
                self.acquisition_budget_seconds = original

    def prepare(inputs, game_id, *, cutoff):
        report_phase("pre_execution")
        return prepare_generation_data(inputs, game_id, cutoff=cutoff)

    def prepare_current_identity(inputs, game_id):
        report_phase("pre_execution", minimum_seconds=180)
        from .current_roster_preflight import prepare_current_roster_preflight
        return prepare_current_roster_preflight(inputs, game_id)

    def factory(inputs, *, analyst):
        from .player_display_names import PlayerDisplayNameStore
        from .market_reference_store import MarketReferenceStore
        searcher = PregameWebSearch()
        return SavedGameAnalysisService(inputs, analyst=analyst, searcher=searcher,
            display_name_store=PlayerDisplayNameStore(versions),
            market_reference_store=MarketReferenceStore(versions),
            researcher=Researcher(searcher=searcher, root=analyst.root / "research"),
            require_research_success=True)

    def analyst_factory(request):
        from v2.pregame_system.storage import BASE
        claim = tasks.active_runtime_claim
        root = BASE / "outputs" / "prediction_version_runs" / request["requested_at"][:10].replace("-", "")
        root = root / request["game_identity"]["game_id"].replace(":", "_") / request["request_id"]
        return Analyst(root=root / f"attempt-{claim['attempt_count']:02d}" / "analyst",
                       # independent + two bounded protocol repairs + market
                       # review + writer + one hard-fact repair; no extra retries.
                       max_calls=6, include_forecasting_skill=True)

    return PredictionVersionService(store=versions, task_store=tasks, service_factory=factory,
        analyst_factory=analyst_factory, input_preparer=prepare,
        pre_generation_preparer=prepare_current_identity)


def generation_worker(database: str, task_id: str, data: dict[str, Any], config,
                      run_id: str, pipe, service_builder=None, clock=None):
    """Spawn target. Each process owns its clients, SQLite connections and inputs."""
    from v2.pregame_system.scheduler_runtime_store import RuntimeTaskStore
    current = {"phase": "pre_execution", "claim": None}

    def send(message):
        try:
            pipe.send(message)
        except (BrokenPipeError, EOFError, OSError):
            pass

    class WorkerTasks(RuntimeTaskStore):
        def leader_healthy(self):
            with closing(self.versions.connect()) as db:
                db.execute("PRAGMA busy_timeout=1000")
                row = db.execute("SELECT * FROM scheduler_runtime_leader WHERE singleton_id=1").fetchone()
            return row is not None and row["run_id"]==run_id and row["status"]=="running" and _instant(row["lease_until"],"lease_until")>datetime.now(timezone.utc)

        def claim(self, identity):
            current["phase"] = "claim"
            if not self.leader_healthy():
                raise VersionStoreError("runtime_scheduler_lease_lost")
            claim = super().claim(identity)
            if claim is not None:
                current["claim"] = claim
                self.active_runtime_claim = claim
                send({"kind": "claimed", "task_id": task_id,
                      "attempt_id": claim["runtime_attempt_id"], "owner_token": claim["owner_token"]})
            return claim

        def assert_claim(self, claim):
            if not self.leader_healthy():
                self._lose_lease(claim,"runtime_scheduler_lease_lost")
                raise VersionStoreError("runtime_scheduler_lease_lost")
            return super().assert_claim(claim)

    tasks = None
    monitor_stop = threading.Event()
    monitor = None
    try:
        isolate_worker_children()
        versions = GuardedVersionStore(Path(database), clock=clock)
        tasks = WorkerTasks(versions, lease_seconds=config.lease_seconds, max_attempts=config.max_attempts,
            heartbeat_seconds=config.heartbeat_seconds, final_min_budget_seconds=config.final_min_budget_seconds,
            publish_margin_seconds=config.publish_margin_seconds, run_id=run_id)
        versions.runtime_tasks = tasks

        def phase(name, *, minimum_seconds=0):
            claim = current["claim"]
            if claim is None:
                raise VersionStoreError("generation_claim_missing")
            tasks.assert_claim(claim)
            remaining = (_instant(claim["effective_deadline_at"], "effective_deadline_at") - versions.clock()).total_seconds()
            if remaining <= minimum_seconds:
                raise VersionStoreError("runtime_deadline_budget_insufficient:" + name)
            current["phase"] = name
            tasks.record_phase(claim, name)
            send({"kind": "phase", "task_id": task_id, "phase": name, "remaining_budget": remaining})
            return remaining

        versions.runtime_phase_callback=phase
        service = (service_builder or production_service)(versions, tasks, data, phase)
        def watch_health():
            while not monitor_stop.wait(min(1, config.heartbeat_seconds)):
                claim = current["claim"]
                if claim is None:
                    continue
                try:
                    row=tasks.get_task(task_id)
                    if row and row["status"]=="succeeded":
                        return
                    lost=tasks._lease_health.get(claim["owner_token"])
                    if tasks.leader_healthy() and not (lost is not None and lost.is_set()):
                        continue
                    tasks._lose_lease(claim,"runtime_scheduler_lease_lost")
                except Exception:
                    tasks._lose_lease(claim,"runtime_health_check_failed")
                send({"kind":"failed","task_id":task_id,"failure_stage":"lease_failure","error_type":"RuntimeHealthFailure"})
                os._exit(13)  # Stop provider/bridge work even when no pipeline hook is reached.
        monitor=threading.Thread(target=watch_health,name="runtime-worker-health",daemon=True)
        monitor.start()
        result = service.execute_task(copy.deepcopy(data), tasks.get_task(task_id)["game_id"], task_id)
        if current["claim"]:
            tasks.finish_attempt(current["claim"], result=result)
        send({"kind": "finished", "task_id": task_id, "update_status": result.get("update_status"),
              "version_id": result.get("version_id")})
    except BaseException as error:
        category = failure_category(error, current["phase"])
        if tasks is not None and current["claim"] is not None:
            try:
                tasks.finish_attempt(current["claim"], error=error, failure_stage=category)
            except Exception:
                pass  # A restarted dispatcher detects any remaining uncertain claim.
        # Exception messages can contain provider request content/secrets: only
        # persist the code/type in runtime status. Full receipts stay in pipeline artifacts.
        send({"kind": "failed", "task_id": task_id, "failure_stage": category,
              "error_type": type(error).__name__,
              **({"import_module":error.name,"missing_symbol":(re.search(r"cannot import name '([A-Za-z_][A-Za-z0-9_]*)'",str(error)).group(1)
                    if re.search(r"cannot import name '([A-Za-z_][A-Za-z0-9_]*)'",str(error)) else None)} if isinstance(error,ImportError) else {})})
    finally:
        monitor_stop.set()
        if monitor is not None:
            monitor.join(timeout=2)
        pipe.close()
