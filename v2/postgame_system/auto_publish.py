"""Idempotent final-score to Postgame V1 bridge for already registered games."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from v2.frontend.schedule_catalog import ScheduleCatalog
from v2.pregame_system.prediction_versions import PredictionVersionStore, _instant, payload_sha256
from v2.pregame_system.sources import get

from .version_service import PostgameVersionService, normalize_completed_summary


PREGAME_STAGES = {"EARLY_PREVIEW", "UPDATED_PREVIEW", "FINAL_PREGAME"}
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event="


def fetch_espn_summary(game_id: str):
    if not isinstance(game_id, str) or not game_id.startswith("espn:"):
        raise ValueError("postgame_requires_espn_game_id")
    return get(ESPN_SUMMARY_URL + game_id.removeprefix("espn:"))


class PostgameAutoPublisher:
    """Publish deterministic postgame records after final facts arrive.

    Only games already registered for pregame prediction are considered. The
    immutable last selected pregame version is the review base. An optional
    Writer creates a separately hashed article after the deterministic record
    is published; its failure never rolls back or mutates that record.
    """

    def __init__(
        self,
        catalog: ScheduleCatalog,
        *,
        prediction_service: Any | None = None,
        summary_fetcher: Callable[[str], tuple[dict[str, Any] | None, dict[str, Any]]] = fetch_espn_summary,
        postgame_factory: Callable[[Any], PostgameVersionService] = PostgameVersionService,
        editorial_generator: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_games: int = 20,
        editorial_output_root=None,
        allowed_game_ids=None,
    ):
        self.catalog = catalog
        self.store=prediction_service.store if prediction_service is not None else PredictionVersionStore(catalog.path)
        self.prediction_service=prediction_service
        self.summary_fetcher = summary_fetcher
        self.postgame_factory = postgame_factory
        self.editorial_generator = editorial_generator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_games = max(1, min(int(max_games), 50))
        self.editorial_output_root=editorial_output_root
        self.allowed_game_ids=None if allowed_game_ids is None else set(allowed_game_ids)

    def _candidates(self) -> list[dict[str, Any]]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone_required")
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"schedule_catalog_games", "prediction_versions", "postgame_versions"}
            if not required.issubset(tables):
                return []
            rows = db.execute(
                """SELECT s.game_id,s.scheduled_tipoff,s.home_score,s.away_score
                   FROM schedule_catalog_games s
                   WHERE s.status='final' AND s.home_score IS NOT NULL AND s.away_score IS NOT NULL
                     AND s.scheduled_tipoff<=?
                     AND EXISTS (SELECT 1 FROM prediction_versions p
                         WHERE p.game_id=s.game_id AND p.published_at<s.scheduled_tipoff)
                     AND NOT EXISTS (SELECT 1 FROM postgame_versions pg WHERE pg.game_id=s.game_id)
                   ORDER BY s.scheduled_tipoff,s.game_id LIMIT ?""",
                (now.astimezone(timezone.utc).isoformat(), self.max_games),
            ).fetchall()
        return [dict(row) for row in rows if self.allowed_game_ids is None or row['game_id']in self.allowed_game_ids]

    def _stored_result(self, game_id: str):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT result_id,payload_json,payload_sha256 FROM game_result_snapshots "
                "WHERE game_id=? ORDER BY captured_at DESC,rowid DESC LIMIT 1", (game_id,)
            ).fetchone()
        if not row:
            return None, None
        import json
        bundle = json.loads(row["payload_json"])
        if payload_sha256(bundle) != row["payload_sha256"]:
            raise ValueError("result_payload_hash_mismatch")
        return row["result_id"], bundle

    @staticmethod
    def _scores_match(bundle: dict[str, Any], candidate: dict[str, Any]) -> bool:
        actual = bundle.get("game") or {}
        try:
            return (int(actual["home_points"]) == int(float(candidate["home_score"]))
                    and int(actual["away_points"]) == int(float(candidate["away_score"])))
        except (KeyError, TypeError, ValueError):
            return False

    def sync_pending(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        postgame = self.postgame_factory(self.store)
        candidates = self._candidates()
        if candidates and self.prediction_service is None:
            # Avoid importing the full report-generation stack when the catalog
            # has no completed registered games to process.
            from v2.frontend.prediction_version_service import PredictionVersionService
            self.prediction_service=PredictionVersionService(store=self.store)
        for candidate in candidates:
            game_id = candidate["game_id"]
            try:
                registered = self.prediction_service.tasks.registered_game(game_id)
                if not registered:
                    results.append({"game_id": game_id, "status": "skipped", "reason": "not_registered"})
                    continue
                game = {**registered, "status": "final"}
                selection = self.prediction_service.store.select_current(game)
                base = selection.get("version")
                if not base or base.get("stage") not in PREGAME_STAGES:
                    results.append({"game_id": game_id, "status": "skipped", "reason": "no_locked_pregame_version"})
                    continue
                if _instant(base["published_at"],"published_at") >= _instant(game["tipoff"],"scheduled_tipoff"):
                    results.append({"game_id": game_id, "status": "skipped", "reason": "base_not_pregame"})
                    continue

                result_id, bundle = self._stored_result(game_id)
                if not result_id or not self._scores_match(bundle, candidate):
                    summary, receipt = self.summary_fetcher(game_id)
                    # Validate identity and the scoreboard result before writing
                    # any immutable result snapshot.
                    validated = normalize_completed_summary(game, summary or {}, receipt or {})
                    if not self._scores_match(validated, candidate):
                        raise ValueError("summary_score_does_not_match_final_schedule")
                    result_id, _ = postgame.sync_result(game, summary, receipt)

                published = postgame.publish(game, result_id, required_base_version=base["version_id"])
                result = {"game_id": game_id, "status": "published",
                    "version_id": published["version_id"],
                    "base_prediction_version_id": published["base_prediction_version_id"],
                    "result_id": published["result_id"]}
                results.append(result)
            except Exception as exc:
                # A failed source/validation/build is retained as a retryable
                # status; the schedule sync loop will retry on its next pass.
                results.append({"game_id": game_id, "status": "failed", "error": str(exc)[:240],
                    "error_type": type(exc).__name__})
        if self.editorial_generator is not None:
            from .version_service import read_postgame
            from .writer_retry import attempt_postgame_writer,initialize_writer_attempts
            initialize_writer_attempts(self.store)
            at=self.clock().isoformat()
            with self.store.connect() as db:
                ids=[r['game_id'] for r in db.execute('''SELECT p.game_id FROM postgame_versions p
                    LEFT JOIN postgame_writer_attempts a ON a.postgame_version_id=p.version_id
                    WHERE p.version_id=(SELECT latest.version_id FROM postgame_versions latest
                        WHERE latest.game_id=p.game_id ORDER BY latest.published_at DESC,latest.rowid DESC LIMIT 1)
                      AND (a.postgame_version_id IS NULL OR (a.status!='succeeded' AND a.attempt_count<3+COALESCE(
                        (SELECT g.additional_attempts FROM postgame_writer_retry_grants g
                         WHERE g.postgame_version_id=p.version_id AND g.not_before<=? AND g.expires_at>?),0)
                        AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=?)
                        AND (a.lease_until IS NULL OR a.lease_until<=?)))
                    ORDER BY p.published_at,p.game_id LIMIT ?''',(at,at,at,at,self.max_games))]
            for game_id in ids:
                if self.allowed_game_ids is not None and game_id not in self.allowed_game_ids:continue
                try:
                    post=read_postgame(self.store,game_id)
                    options={'clock':self.clock}
                    if self.editorial_output_root is not None:
                        options['output_root']=self.editorial_output_root
                    status=attempt_postgame_writer(self.store,post,self.editorial_generator,**options)
                    target=next((r for r in results if r['game_id']==game_id),None)
                    if target is not None:
                        target.update(status)
                    elif status['editorial_status'] not in {'already_exists','waiting','exhausted'}:
                        results.append({'game_id':game_id,'status':'record_preserved','version_id':post['version_id'],**status})
                except Exception as exc:
                    results.append({'game_id':game_id,'status':'record_preserved','editorial_status':'failed','error':str(exc)[:240]})
        return results


def sync_pending_postgames(catalog: ScheduleCatalog, **kwargs) -> list[dict[str, Any]]:
    import os
    scope=os.environ.get('COURTVISION_REPORT_GAME_IDS')
    if scope and 'allowed_game_ids'not in kwargs:kwargs['allowed_game_ids']=scope.split(',')
    if "editorial_generator" not in kwargs:
        from .editorial import generate_postgame_editorial
        kwargs["editorial_generator"] = generate_postgame_editorial
    result=PostgameAutoPublisher(catalog, **kwargs).sync_pending()
    root=os.environ.get('COURTVISION_PUBLIC_PUBLISH_ROOT')
    if root:
        from v2.frontend.opening_publish import publication_queue,publish_saved
        queue=publication_queue(catalog.path)
        allowed=kwargs.get('allowed_game_ids')
        discovery=queue.discover_postgames(catalog.path,game_ids=allowed)
        if discovery['errors']:result.append({'publication_errors':discovery['errors']})
        job=queue.claim(game_ids=allowed)
        if job:
            result.append({'public_delivery':queue.run(job,lambda item:publish_saved(item,
                public_root=root,queue_root=queue.root,
                api_base=os.environ.get('COURTVISION_PUBLIC_API_BASE','http://127.0.0.1:8625')))})
    return result
