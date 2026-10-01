"""Manual generation and authoritative selection for product predictions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from v2.pregame_system.analyst import EvidenceBoundAnalyst
from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError
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
    ):
        self.store = store or PredictionVersionStore(clock=clock)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.service_factory = service_factory
        self.analyst_factory = analyst_factory

    def selected(self, game: dict[str, Any]) -> dict[str, Any]:
        return self.store.select_current(game)

    def generate(self, data: dict[str, Any], game_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        game = next((item for item in data.get("games", []) if item.get("id") == game_id), None)
        if game is None:
            raise KeyError("unknown_game")
        request_id = request_id or str(uuid.uuid4())
        request = self.store.begin_request(game, request_id=request_id)
        if request.get("already_published"):
            return self._selection_reply(self.store.select_current(game), update_status="already_published")
        if self.analyst_factory:
            analyst = self.analyst_factory(request)
        else:
            request_root = (BASE / "outputs" / "prediction_version_runs"
                            / request["requested_at"][:10].replace("-", "")
                            / game_id.replace(":", "_") / request_id)
            analyst = EvidenceBoundAnalyst(root=request_root / "analyst", max_calls=4, include_forecasting_skill=True)
        service_kwargs: dict[str, Any] = {"analyst": analyst}
        if self.service_factory is SavedGameAnalysisService:
            from v2.pregame_system.web_search import PregameWebSearch

            request_root = (BASE / "outputs" / "prediction_version_runs"
                            / request["requested_at"][:10].replace("-", "")
                            / game_id.replace(":", "_") / request_id)
            searcher = PregameWebSearch()
            service_kwargs.update(
                searcher=searcher,
                researcher=AutonomousWebResearcher(
                    searcher=searcher, root=request_root / "research"
                ),
            )
        service = self.service_factory(data, **service_kwargs)
        try:
            analysis = service.analyze(
                game_id,
                allow_lookup=True,
                cutoff=request["requested_at"],
                before_writer=lambda: self.store.assert_request_open(game, request),
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
            "trigger": {"type": "manual_refresh", "request_id": request["request_id"]},
            "requested_at": request["requested_at"],
            "input_cutoff_at": analysis["cutoff"],
            "input_frozen_at": analysis["input_frozen_at"],
            "generated_at": analysis["generated_at"],
            "prediction": analysis["judgment"]["final_prediction"],
            "judgment": analysis["judgment"],
            "official_numeric_prediction": analysis["official_numeric_prediction"],
            "model_layers": analysis["model_layers"],
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
            "provenance": analysis["provenance"],
        }
        try:
            self.store.publish(game, request, payload)
        except VersionStoreError as exc:
            if not exc.args or exc.args[0] != "stale_request_seq":
                raise
            return self._selection_reply(self.store.select_current(game), update_status="superseded")
        return self._selection_reply(self.store.select_current(game), update_status="published")

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
        }

