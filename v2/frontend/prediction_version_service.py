"""Manual generation and authoritative selection for product predictions."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Callable

from v2.pregame_system.analyst import EvidenceBoundAnalyst
from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError, game_identity
from v2.pregame_system.generation_tasks import GenerationTaskStore
from v2.pregame_system.storage import BASE
from v2.pregame_system.web_research_agent import AutonomousWebResearcher

from .analysis_service import SavedGameAnalysisService


class PredictionVersionService:
    def __init__(
        self,
        *,
        store: PredictionVersionStore | None = None,
        clock: Callable[[], datetime] | None = None,
        service_factory: Callable[..., SavedGameAnalysisService] = SavedGameAnalysisService,
        analyst_factory: Callable[[dict[str, Any]], EvidenceBoundAnalyst] | None = None,
        task_store: GenerationTaskStore | None = None,
        input_preparer: Callable[..., dict[str, Any]] | None = None,
        pre_generation_preparer: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
    ):
        self.store = store or PredictionVersionStore(clock=clock)
        self.clock = clock or self.store.clock
        self.service_factory = service_factory
        self.analyst_factory = analyst_factory
        self.tasks = task_store or GenerationTaskStore(self.store)
        self.input_preparer = input_preparer
        self.pre_generation_preparer = pre_generation_preparer

    def selected(self, game: dict[str, Any]) -> dict[str, Any]:
        selection = self.store.select_current(game)
        if selection.get("version"):
            from v2.pregame_system.fact_context import build_version_fact_view
            fact_view = build_version_fact_view(selection["version"])
            if fact_view is not None:
                selection["fact_view"] = fact_view
        return {**selection, "generation_task": self.tasks.current_task(game["id"])}

    def generate(self, data: dict[str, Any], game_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        """Internal/admin execution only. Public HTTP readers never invoke this method."""
        game = next((item for item in data.get("games", []) if item.get("id") == game_id), None)
        if game is None:
            raise KeyError("unknown_game")
        task = self.tasks.request_manual(game, request_id=request_id)
        return self.execute_task(data, game_id, task["task_id"])

    def execute_task(self, data: dict[str, Any], game_id: str, task_id: str) -> dict[str, Any]:
        game = next((item for item in data.get("games", []) if item.get("id") == game_id), None)
        if game is None:
            raise KeyError("unknown_game")
        task = self.tasks.get_task(task_id)
        if task is None or task["game_id"] != game_id:
            raise VersionStoreError("generation_task_wrong_game")
        registered = self.tasks.registered_game(game_id)
        if registered is None or game_identity(game) != game_identity(registered):
            raise VersionStoreError("schedule_changed")
        if game.get("schedule_revision", 1) != registered["schedule_revision"]:
            raise VersionStoreError("schedule_revision_changed")
        if game.get("game_type", registered["game_type"]) != registered["game_type"]:
            raise VersionStoreError("registered_game_type_changed")
        claim = self.tasks.claim(task_id)
        if claim is None:
            task = self.tasks.get_task(task_id)
            reply = self._selection_reply(self.selected(game), update_status=(
                "already_published" if task["status"] == "succeeded" else task["status"]
            ))
            reply["generation_task"] = self.tasks.summary(task)
            if task["status"] == "running":
                reply["message"] = "赛前报告正在生成，请稍后查看。"
            return reply
        try:
            with self.tasks.keep_alive(claim):
                reply = self._generate_version(data, game_id, claim)
                reply["generation_task"] = self.tasks.summary(self.tasks.get_task(task_id))
                return reply
        except Exception as exc:
            reason = str(exc) if isinstance(exc, VersionStoreError) else type(exc).__name__
            self.tasks.fail(claim, reason)
            raise

    def _generate_version(self, data: dict[str, Any], game_id: str, claim: dict[str, Any]) -> dict[str, Any]:
        game = next(item for item in data["games"] if item["id"] == game_id)
        if game.get("game_type") is None:
            registered = self.tasks.registered_game(game_id)
            data = {**data, "games": [
                {**item, "game_type": registered["game_type"]} if item["id"] == game_id else item
                for item in data["games"]
            ]}
            game = next(item for item in data["games"] if item["id"] == game_id)
        request_id = claim["request_id"]
        # Current roster identity is captured before begin_request creates the
        # generation cutoff. The same explicit hook is wired by API and
        # Scheduler production entrypoints; history-only Player Knowledge is
        # never a replacement for this proof.
        self.tasks.assert_claim(claim)
        if self.pre_generation_preparer is not None:
            data = self.pre_generation_preparer(data, game_id)
            game = next(item for item in data["games"] if item["id"] == game_id)
        injuries = None
        if self.service_factory is SavedGameAnalysisService:
            from .official_injury_reports import OfficialInjuryReportStore
            injuries = OfficialInjuryReportStore(self.store.path, clock=self.clock)
            try:
                injuries.refresh(game,max_seconds=10,max_reports=1)
            except Exception:
                pass  # A missing official report never blocks independent forecasting.
        request = self.store.begin_request(game, request_id=request_id)
        if request.get("already_published"):
            return self._selection_reply(self.selected(game), update_status="already_published")
        self.tasks.assert_claim(claim)
        if request["schedule_revision"] != claim["schedule_revision"]:
            raise VersionStoreError("schedule_revision_changed")
        if self.input_preparer is not None:
            data = self.input_preparer(data, game_id, cutoff=request["requested_at"])
        elif self.service_factory is SavedGameAnalysisService:
            from .generation_input import prepare_generation_data
            data = prepare_generation_data(data, game_id, cutoff=request["requested_at"])
        game = next(item for item in data["games"] if item["id"] == game_id)
        game["schedule_revision"] = claim["schedule_revision"]
        if injuries is not None:
            try:
                report=injuries.read_as_of(game,request['requested_at'],captured_before_cutoff=True)
                game['official_injury_report']=report
                if report.get('records'):
                    game.setdefault('background',{})['availability']={**report,'status':'available'}
            except Exception:
                game['official_injury_report']={'status':'unavailable','records':[]}
        game["generation_context"] = {"policy_version": claim["policy_version"],
            "stage": claim["stage"],
            "task_metadata": json.loads(claim["metadata_json"])}
        request_root = (BASE / "outputs" / "prediction_version_runs"
                        / request["requested_at"][:10].replace("-", "")
                        / game_id.replace(":", "_") / request_id
                        / f"attempt-{claim['attempt_count']:02d}")
        if self.analyst_factory:
            analyst = self.analyst_factory(request)
        else:
            analyst = EvidenceBoundAnalyst(root=request_root / "analyst", max_calls=6, include_forecasting_skill=True)
        service_kwargs: dict[str, Any] = {"analyst": analyst}
        if self.service_factory is SavedGameAnalysisService:
            from .player_display_names import PlayerDisplayNameStore
            service_kwargs['display_name_store']=PlayerDisplayNameStore(self.store)
            from .market_reference_store import MarketReferenceStore
            service_kwargs['market_reference_store']=MarketReferenceStore(self.store)
            from v2.pregame_system.web_search import PregameWebSearch

            searcher = PregameWebSearch()
            service_kwargs.update(
                searcher=searcher,
                researcher=AutonomousWebResearcher(
                    searcher=searcher, root=request_root / "research"
                ),
            )
        service = self.service_factory(data, **service_kwargs)
        def before_writer():
            self.store.assert_request_open(game, request)
            self.tasks.assert_claim(claim)
        before_writer()
        try:
            analysis = service.analyze(
                game_id,
                allow_lookup=True,
                cutoff=request["requested_at"],
                before_writer=before_writer,
            )
        except VersionStoreError as exc:
            if exc.args and exc.args[0] == "pregame_generation_closed":
                self.store.select_current(game)
            raise
        payload = {
            "game_id": game_id,
            "request_id": request["request_id"],
            "analysis_contract_version": analysis["analysis_contract_version"],
            "game_identity": request["game_identity"],
            "trigger": {"type": claim["trigger"], "request_id": request["request_id"]},
            "stage": claim["stage"],
            "scheduled_for": claim["scheduled_for"],
            "task_id": claim["task_id"],
            "policy_version": claim["policy_version"],
            "schedule_revision": claim["schedule_revision"],
            "created_reason": claim["created_reason"],
            "task_metadata": json.loads(claim["metadata_json"]),
            "requested_at": request["requested_at"],
            "input_cutoff_at": analysis["cutoff"],
            "input_frozen_at": analysis["input_frozen_at"],
            "generated_at": analysis["generated_at"],
            "prediction": analysis["judgment"]["final_prediction"],
            "judgment": analysis["judgment"],
            "official_numeric_prediction": analysis["official_numeric_prediction"],
            "model_layers": analysis["model_layers"],
            "public_market_reference": analysis.get('public_market_reference'),
            "market_collection": analysis.get('market_collection'),
            "spread_comparison": analysis.get("spread_comparison"),
            "input_snapshot": analysis["input_snapshot"],
            "research": analysis.get("research"),
            "analyst_context": analysis["analyst_context"],
            "evidence_index": analysis["evidence_index"],
            "sources": analysis["sources"],
            "cited_sources": analysis["cited_sources"],
            "context_hash": analysis["context_hash"],
            "base_input_hash": analysis["base_input_hash"],
            "search": analysis["search"],
            "analyst_trace": analysis["analyst_trace"],
            "provenance": {
                **analysis["provenance"],
                "runtime_assets": (analysis["input_snapshot"].get("technical") or {}).get("runtime_asset_identity"),
            },
        }
        analyst_trace = analysis.get("analyst_trace") or {}
        for key in ("publication_receipt", "editorial_projection", "editorial_material", "quality_review",
                    "quality_review_input_sha256"):
            value = analysis.get(key)
            if value is None:
                value = analyst_trace.get(key)
            if value is not None:
                payload[key] = value
        try:
            self.store.publish(game, request, payload, generation_claim=claim)
        except VersionStoreError as exc:
            if not exc.args or exc.args[0] not in {"stale_request_seq", "prediction_stage_regression"}:
                raise
            self.tasks.supersede(claim, str(exc))
            return self._selection_reply(self.selected(game), update_status="superseded")
        return self._selection_reply(self.selected(game), update_status="published")

    @staticmethod
    def _selection_reply(selection: dict[str, Any], *, update_status: str) -> dict[str, Any]:
        return {
            "status": "success" if selection.get("version") else selection["status"],
            "update_status": update_status,
            "selection_status": selection["status"],
            "game_id": selection["game_id"],
            "version_id": selection.get("version_id"),
            "state_revision": selection["state_revision"],
            "locked_at": selection.get("locked_at"),
            "version": selection.get("version"),
            "fact_view": selection.get("fact_view"),
            "generation_task": selection.get("generation_task"),
        }
