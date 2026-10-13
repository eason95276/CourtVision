"""Deterministic, version-bound spread comparisons and ATS settlement."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .storage import content_hash


ATS_EPSILON = 1e-9
SPREAD_COMPARISON_SCHEMA = "courtvision-spread-comparison-v1"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), 10)
    return 0.0 if abs(rounded) <= ATS_EPSILON else rounded


def model_home_margin(prediction: Mapping[str, Any] | None) -> float | None:
    """Return home score minus away score without guessing ambiguous margins."""
    if not isinstance(prediction, Mapping):
        return None
    for key in ("model_home_margin", "expected_home_margin", "predicted_home_margin"):
        value = _finite_number(prediction.get(key))
        if value is not None:
            return value
    value = _finite_number(prediction.get("expected_margin"))
    if value is None:
        return None
    if prediction.get("perspective") == "home":
        return value
    side = prediction.get("winner_side")
    if side not in {"home", "away"} or value < 0:
        return None
    return value if side == "home" else -value


def _winner_side(prediction: Mapping[str, Any] | None, margin: float | None) -> str | None:
    if isinstance(prediction, Mapping) and prediction.get("winner_side") in {"home", "away"}:
        return str(prediction["winner_side"])
    if margin is None or abs(margin) <= ATS_EPSILON:
        return None
    return "home" if margin > 0 else "away"


def _market_reference(collection: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    collection = collection if isinstance(collection, Mapping) else {}
    reference = collection.get("public_market_reference")
    evidence_reference = None
    for evidence in collection.get("evidence") or []:
        value = evidence.get("value") if isinstance(evidence, Mapping) else None
        candidate = value.get("market_reference") if isinstance(value, Mapping) else None
        if isinstance(candidate, Mapping):
            evidence_reference = candidate
            break
    if isinstance(evidence_reference, Mapping):
        merged = dict(evidence_reference)
        if isinstance(reference, Mapping):
            merged.update({key: value for key, value in reference.items() if value is not None})
        reference = merged
    if not isinstance(reference, Mapping) and any(key in collection for key in (
        "home_spread", "expected_home_margin", "market_expected_home_margin"
    )):
        reference = collection
    if not isinstance(reference, Mapping):
        return None, None
    evidence_id = next((str(row.get("id")) for row in collection.get("evidence") or []
                        if isinstance(row, Mapping) and row.get("id")), None)
    return dict(reference), evidence_id


def _team(game: Mapping[str, Any] | None, reference: Mapping[str, Any], side: str) -> dict[str, Any]:
    game = game if isinstance(game, Mapping) else {}
    candidates = [game.get(side), (reference.get("game_identity") or {}).get(side)]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        team_id = candidate.get("id", candidate.get("team_id"))
        name = candidate.get("name_zh") or candidate.get("display_name") or candidate.get("name")
        if team_id is not None or name:
            return {"team_id": team_id, "team_name": name}
    identity = reference.get("game_identity") or {}
    return {"team_id": identity.get(f"{side}_team_id"),
            "team_name": identity.get(f"{side}_team")}


def _quote_identity(reference: Mapping[str, Any], evidence_id: str | None,
                    home_spread: float | None, away_spread: float | None,
                    market_margin: float | None) -> dict[str, Any]:
    identity = reference.get("game_identity")
    if not isinstance(identity, Mapping):
        identity = {}
    source = reference.get("source_url") or reference.get("url") or reference.get("source")
    if isinstance(source, Mapping):
        source = source.get("url")
    quote_material = {
        "game_identity": dict(identity), "home_spread": home_spread,
        "away_spread": away_spread, "market_implied_home_margin": market_margin,
        "sportsbook": reference.get("sportsbook") or reference.get("provider"),
        "source": source, "published_at": reference.get("published_at"),
        "observed_at": reference.get("observed_at"),
        "captured_at": reference.get("captured_at"),
        "raw_response_sha256": reference.get("raw_response_sha256"),
    }
    quote_available = reference.get("status") not in {"unavailable", "failed", "rejected"}
    spread_available = home_spread is not None
    return {
        "status": "available" if quote_available else "unavailable",
        "spread_status": "available" if spread_available and quote_available else "unavailable",
        "spread_reason": None if spread_available and quote_available else "market_spread_unavailable",
        "market_quote_id": evidence_id or content_hash(quote_material),
        "game_identity": dict(identity),
        "home_spread": home_spread,
        "away_spread": away_spread,
        "market_implied_home_margin": market_margin,
        "total": _finite_number(reference.get("total")),
        "sportsbook": reference.get("sportsbook") or reference.get("provider"),
        "source": source,
        "published_at": reference.get("published_at"),
        "observed_at": reference.get("observed_at"),
        "captured_at": reference.get("captured_at") or reference.get("observed_at"),
        "raw_response_sha256": reference.get("raw_response_sha256"),
        "quote_sha256": content_hash(quote_material),
    }


def _spread_market(collection: Mapping[str, Any] | None,
                   game: Mapping[str, Any] | None) -> dict[str, Any]:
    reference, evidence_id = _market_reference(collection)
    if reference is None:
        return {"status": "unavailable", "reason": "market_quote_missing"}
    if reference.get("status") in {"unavailable", "failed", "rejected"}:
        return {"status": "unavailable", "spread_status": "unavailable",
                "reason": str(reference.get("reason") or "market_quote_unavailable"),
                "spread_reason": str(reference.get("reason") or "market_quote_unavailable")}

    identity = reference.get("game_identity")
    if isinstance(identity, Mapping) and isinstance(game, Mapping):
        expected_id = game.get("game_id") or game.get("id")
        observed_id = identity.get("game_id")
        if expected_id and observed_id and str(expected_id) != str(observed_id):
            return {"status": "unavailable", "spread_status": "unavailable",
                    "reason": "spread_market_game_identity_mismatch",
                    "spread_reason": "spread_market_game_identity_mismatch"}
        for key in ("home_team_id", "away_team_id"):
            expected = game.get(key)
            if expected is None and isinstance(game.get(key.removesuffix("_team_id")), Mapping):
                expected = game[key.removesuffix("_team_id")].get("id")
            observed = identity.get(key)
            if expected is not None and observed is not None:
                try:
                    if int(expected) != int(observed):
                        return {"status": "unavailable", "spread_status": "unavailable",
                                "reason": "spread_market_team_identity_mismatch",
                                "spread_reason": "spread_market_team_identity_mismatch"}
                except (TypeError, ValueError):
                    return {"status": "unavailable", "spread_status": "unavailable",
                            "reason": "spread_market_team_identity_mismatch",
                            "spread_reason": "spread_market_team_identity_mismatch"}

    home_spread = _finite_number(reference.get("home_spread"))
    supplied_market_margin = _finite_number(
        reference.get("expected_home_margin", reference.get("market_expected_home_margin"))
    )
    if home_spread is None and supplied_market_margin is not None:
        home_spread = -supplied_market_margin
    market_margin = -home_spread if home_spread is not None else None
    if supplied_market_margin is not None and market_margin is not None and abs(
        supplied_market_margin - market_margin
    ) > ATS_EPSILON:
        return {"status": "unavailable", "spread_status": "unavailable",
                "reason": "spread_market_sign_conflict", "spread_reason": "spread_market_sign_conflict"}
    away_spread = _finite_number(reference.get("away_spread"))
    if away_spread is None and home_spread is not None:
        away_spread = -home_spread
    if away_spread is not None and home_spread is not None and abs(home_spread + away_spread) > ATS_EPSILON:
        return {"status": "unavailable", "spread_status": "unavailable",
                "reason": "spread_market_paired_lines_conflict", "spread_reason": "spread_market_paired_lines_conflict"}
    return _quote_identity(reference, evidence_id, _round(home_spread),
                           _round(away_spread), _round(market_margin))


def _compare_one(prediction: Mapping[str, Any] | None, market: Mapping[str, Any],
                 game: Mapping[str, Any] | None) -> dict[str, Any]:
    margin = model_home_margin(prediction)
    winner = _winner_side(prediction, margin)
    quote_margin = _finite_number(market.get("market_implied_home_margin"))
    if market.get("spread_status") != "available" or margin is None or quote_margin is None:
        return {
            "status": "unavailable",
            "reason": market.get("reason") or market.get("spread_reason") or ("market_spread_unavailable" if quote_margin is None
                                                else "forecast_margin_unavailable"),
            "winner_side": winner,
            "model_home_margin": _round(margin),
            "market_implied_home_margin": _round(quote_margin),
            "spread_edge": None,
            "margin_relationship": "unavailable",
            "ats_lean": "unavailable",
            "ats_lean_reference": None,
            "winner_relationship": "unavailable",
        }

    edge = margin - quote_margin
    if abs(edge) <= ATS_EPSILON:
        edge = 0.0
        lean = "no_edge"
        margin_relationship = "model_margin_equal_to_market"
    else:
        lean = "home" if edge > 0 else "away"
        margin_relationship = "model_margin_above_market" if edge > 0 else "model_margin_below_market"

    market_favorite = "home" if quote_margin > ATS_EPSILON else (
        "away" if quote_margin < -ATS_EPSILON else None
    )
    relationship = ("unavailable" if winner is None or market_favorite is None else
                    "winner_aligned" if winner == market_favorite else "winner_opposed")

    lean_reference = None
    if lean in {"home", "away"}:
        team = _team(game, market, lean)
        line = _finite_number(market.get(f"{lean}_spread"))
        if line is None:
            line = -quote_margin if lean == "home" else quote_margin
        name = team.get("team_name") or lean
        line_text = f"{line:+g}"
        lean_reference = {"side": lean, **team, "line": _round(line),
                          "display": f"{name} {line_text}"}

    return {
        "status": "available",
        "winner_side": winner,
        "model_home_margin": _round(margin),
        "market_implied_home_margin": _round(quote_margin),
        "spread_edge": _round(edge),
        "margin_relationship": margin_relationship,
        "ats_lean": lean,
        "ats_lean_reference": lean_reference,
        "winner_relationship": relationship,
        "winner_relationship_basis": "spread_implied_favorite",
    }


def build_spread_comparison(game: Mapping[str, Any] | None,
                            market_collection: Mapping[str, Any] | None,
                            independent_prediction: Mapping[str, Any] | None,
                            final_prediction: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build Independent and Final comparisons against one frozen market quote."""
    market = _spread_market(market_collection, game)
    return {
        "schema_version": SPREAD_COMPARISON_SCHEMA,
        "status": "available" if market.get("spread_status") == "available" else "unavailable",
        "market": market,
        "independent": _compare_one(independent_prediction, market, game),
        "final": _compare_one(final_prediction, market, game),
    }


def settle_ats(comparison: Mapping[str, Any] | None, actual_home_margin: Any,
               market: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Settle a frozen ATS lean against the quote used to create it."""
    comparison = comparison if isinstance(comparison, Mapping) else {}
    lean = comparison.get("ats_lean")
    actual = _finite_number(actual_home_margin)
    if lean == "no_edge":
        return {"status": "no_edge", "ats_lean": "no_edge", "line": None, "spread_edge": comparison.get("spread_edge"), "result": "no_edge"}
    if lean not in {"home", "away"} or actual is None:
        return {"status": "unavailable", "ats_lean": "unavailable", "line": None,
                "spread_edge": comparison.get("spread_edge"), "result": "unavailable"}
    market = market or comparison.get("market") or {}
    if market.get("spread_status") != "available":
        return {"status": "unavailable", "ats_lean": "unavailable", "line": None,
                "spread_edge": comparison.get("spread_edge"), "result": "unavailable"}
    pick_line = _finite_number(market.get(f"{lean}_spread"))
    home_line = _finite_number(market.get("home_spread"))
    if pick_line is None or home_line is None:
        return {"status": "unavailable", "ats_lean": "unavailable", "line": None,
                "spread_edge": comparison.get("spread_edge"), "result": "unavailable"}
    # The paired quote stores each team's own handicap. Settlement is easiest
    # in the canonical home-score coordinate: actual_home_margin + home line.
    settlement_delta = actual + home_line
    if abs(settlement_delta) <= ATS_EPSILON:
        result = "push"
    else:
        home_covered = settlement_delta > 0
        pick_won = home_covered if lean == "home" else not home_covered
        result = "win" if pick_won else "loss"
    return {"status": "settled", "ats_lean": lean,
            "ats_lean_reference": comparison.get("ats_lean_reference"), "line": _round(pick_line),
            "spread_edge": comparison.get("spread_edge"),
            "actual_home_margin": _round(actual), "result": result}


def _read_independent_artifact(version: Mapping[str, Any]) -> Mapping[str, Any] | None:
    provenance = version.get("provenance") or {}
    receipt = provenance.get("forecast_artifact") or {}
    files = receipt.get("files") or []
    root = Path(__file__).resolve().parents[2]
    for item in files:
        if not isinstance(item, Mapping) or Path(str(item.get("path") or "")).name != "independent.json":
            continue
        path = Path(str(item["path"]))
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve())
            raw = resolved.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                return None
            saved = json.loads(raw.decode("utf-8"))
            body = saved.get("prediction_and_basis")
            if not isinstance(body, Mapping) or content_hash(body) != saved.get("sha256"):
                return None
            prediction = body.get("prediction")
            return prediction if isinstance(prediction, Mapping) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    return None


def _collection_for_version(version: Mapping[str, Any]) -> Mapping[str, Any] | None:
    collection = version.get("market_collection")
    if isinstance(collection, Mapping):
        return collection
    reference = version.get("public_market_reference")
    if not isinstance(reference, Mapping):
        reference = (version.get("model_layers") or {}).get("market_benchmark")
    return {"public_market_reference": reference} if isinstance(reference, Mapping) else None


def spread_comparison_for_new_version(version: Mapping[str, Any]) -> dict[str, Any]:
    """Create the new-version contract and reject conflicts with its frozen inputs."""
    version = version if isinstance(version, Mapping) else {}
    stored = version.get("spread_comparison")
    artifact_prediction = _read_independent_artifact(version)
    stored_independent = (stored.get("independent") or {}) if isinstance(stored, Mapping) else {}
    if artifact_prediction is not None:
        stored_margin = _finite_number(stored_independent.get("model_home_margin"))
        if stored_margin is not None:
            artifact_margin = model_home_margin(artifact_prediction)
            if (artifact_margin is None or abs(artifact_margin - stored_margin) > ATS_EPSILON
                    or stored_independent.get("winner_side") != _winner_side(artifact_prediction, artifact_margin)):
                raise ValueError("spread_comparison_independent_artifact_mismatch")
        independent = artifact_prediction
    elif _finite_number(stored_independent.get("model_home_margin")) is not None:
        independent = {"expected_home_margin": stored_independent.get("model_home_margin"),
                       "winner_side": stored_independent.get("winner_side")}
    else:
        independent = None

    judgment = version.get("judgment") or {}
    final = judgment.get("final_prediction") or version.get("prediction")
    game = version.get("input_snapshot") or version.get("game_identity") or {}
    collection = _collection_for_version(version)
    comparison = build_spread_comparison(game, collection, independent, final)
    if isinstance(stored, Mapping):
        old_market = stored.get("market") or {}
        new_market = comparison.get("market") or {}
        if old_market.get("quote_sha256") and old_market.get("quote_sha256") != new_market.get("quote_sha256"):
            raise ValueError("spread_comparison_market_snapshot_mismatch")
        if _finite_number(stored_independent.get("model_home_margin")) is not None:
            for key in ("model_home_margin", "spread_edge", "ats_lean", "winner_side"):
                if stored_independent.get(key) != comparison["independent"].get(key):
                    raise ValueError("spread_comparison_independent_mismatch")
        stored_final = stored.get("final") or {}
        if _finite_number(stored_final.get("model_home_margin")) is not None:
            for key in ("model_home_margin", "spread_edge", "ats_lean", "winner_side"):
                if stored_final.get(key) != comparison["final"].get(key):
                    raise ValueError("spread_comparison_final_mismatch")
    if comparison["market"].get("spread_status") == "available":
        if comparison["final"].get("ats_lean") == "no_edge":
            raise ValueError("final_spread_edge_zero")
        if comparison["final"].get("ats_lean") not in {"home", "away"}:
            raise ValueError("final_spread_lean_unavailable")
    return comparison


def spread_comparison_for_version(version: Mapping[str, Any]) -> dict[str, Any]:
    """Read or derive a comparison without mutating a frozen version payload."""
    version = version if isinstance(version, Mapping) else {}
    stored = version.get("spread_comparison")
    collection = _collection_for_version(version)

    judgment = version.get("judgment") or {}
    final_prediction = judgment.get("final_prediction") or version.get("prediction")
    independent_prediction = _read_independent_artifact(version)
    if independent_prediction is None and isinstance(stored, Mapping):
        independent = stored.get("independent") or {}
        margin = independent.get("model_home_margin")
        winner = independent.get("winner_side")
        if _finite_number(margin) is not None:
            independent_prediction = {"expected_home_margin": margin, "winner_side": winner}

    game = version.get("input_snapshot") or version.get("game_identity") or {}
    derived = build_spread_comparison(game, collection, independent_prediction, final_prediction)
    if version.get("version_id"):
        derived["prediction_version_id"] = version["version_id"]
    if isinstance(stored, Mapping) and independent_prediction is None:
        # A legacy payload without its external forecast artifact remains readable;
        # retain its frozen independent comparison after validating the frozen quote.
        frozen_independent = stored.get("independent") or {}
        if frozen_independent.get("status") == "unavailable":
            derived["independent"] = dict(frozen_independent)
    return derived


def validate_spread_comparison(comparison: Mapping[str, Any], game: Mapping[str, Any] | None,
                               market_collection: Mapping[str, Any] | None,
                               independent_prediction: Mapping[str, Any] | None,
                               final_prediction: Mapping[str, Any] | None) -> bool:
    expected = build_spread_comparison(game, market_collection, independent_prediction, final_prediction)
    return dict(comparison) == expected
