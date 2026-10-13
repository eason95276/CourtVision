"""Explicit injected protocol fixtures. Never used by the production generator.

These receipts exercise storage/hash/selection, not semantic correctness.
"""
import copy
import json
from .editorial_contract import build_editorial_projection, make_publication_receipt, validate_publication_hard_facts
from .storage import content_hash


def seal_fixture_payload(payload):
    """Construct an offline candidate receipt; never imply semantic certification."""
    j = payload["judgment"]
    if not j.get("analysis_article"):
        j["analysis_article"] = "Explicit offline storage fixture, not an NBA factual analysis."
    c = payload.setdefault("analyst_context", {})
    if not c.get("game"):
        c["game"] = copy.deepcopy(payload.get("game_identity") or {})
    c["game"].setdefault("game_id", payload.get("game_id"))
    if "cutoff" in c and "cutoff" not in c["game"]:
        c["game"]["cutoff"] = c.pop("cutoff")
    j.pop("evidence_index", None)
    objective_evidence = copy.deepcopy(payload.get("objective_evidence") or c.get("objective_evidence") or [])
    known_ids = {item.get("id") for item in objective_evidence if item.get("id")}
    evidence_index = payload.get("evidence_index") or c.get("evidence_index") or {}
    required = set()
    def collect_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("evidence_ids") and isinstance(child, list):
                    required.update(ref for ref in child if isinstance(ref, str))
                else:
                    collect_refs(child)
        elif isinstance(value, list):
            for child in value:
                collect_refs(child)
    collect_refs(j)
    for ident in sorted(required - known_ids):
        meta = evidence_index.get(ident)
        if not meta:
            raise ValueError("offline_fixture_missing_cited_evidence")
        objective_evidence.append({"id": ident, "section": meta.get("section", "fixture"),
            "kind": meta.get("source_type", "fixture"), "value": {}})
    c["objective_evidence"] = objective_evidence
    c.setdefault("analysis_request", None)
    c.setdefault("pure_prediction", {})
    c.setdefault("roster_context", None)
    c.setdefault("matchup_history", None)
    c.setdefault("season_form", None)
    c.setdefault("season_phase", None)
    c.setdefault("market_benchmark", None)
    c.setdefault("divergence", None)
    c.setdefault("residual_prediction", None)
    c.setdefault("official_numeric_prediction", {})
    c.setdefault("missing_data", [])
    c.setdefault("evidence_quality", {})
    c.setdefault("domain_analysis_perspectives", [])
    c.setdefault("classification_legend", {})
    c.setdefault("evidence_index", {item["id"]: {"section": item.get("section", ""), "source_type": "fixture"}
                                    for item in c.get("objective_evidence", []) if item.get("id")})
    allowed = {item.get("id") for item in c.get("objective_evidence") or [] if item.get("id")}
    references = set()
    def collect_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("evidence_ids") and isinstance(child, list):
                    references.update(ref for ref in child if isinstance(ref, str))
                else:
                    collect_refs(child)
        elif isinstance(value, list):
            for child in value:
                collect_refs(child)
    collect_refs(payload.get("judgment") or {})
    for ident in sorted(references - allowed):
        meta = c["evidence_index"].get(ident)
        if not meta:
            raise ValueError("offline_fixture_missing_cited_evidence")
        c["objective_evidence"].append({"id": ident, "section": meta.get("section", "fixture"),
            "kind": meta.get("source_type", "fixture"), "value": {}})
    c.setdefault("context_hash", content_hash(c))
    allowed = {item.get("id") for item in c.get("objective_evidence") or [] if item.get("id")}
    prediction = copy.deepcopy(payload.get("prediction") or j.get("final_prediction") or {})
    prediction.setdefault("evidence_ids", sorted(allowed)[:1])
    j.setdefault("winner_lean", prediction.get("winner_side", "home"))
    j["winner_lean"] = prediction.get("winner_side", j["winner_lean"])
    j.setdefault("side_lean", "no_lean")
    j.setdefault("total_lean", "no_lean")
    j.setdefault("confidence", "low")
    j.setdefault("supporting_evidence_ids", [])
    j.setdefault("conflicting_evidence_ids", [])
    j.setdefault("baseline_agreement", {"pure": "not_applicable", "market": "not_applicable",
                                        "residual": "not_applicable"})
    j.setdefault("subjective_rationale", "Offline test writer output.")
    j.setdefault("pregame_story", {"title": "Offline fixture", "statement": "Offline test fixture.",
                                   "classification": "subjective_analysis", "evidence_ids": []})
    j.setdefault("watch_themes", [
        {"title": "Offline theme one", "statement": "Offline fixture theme.", "classification": "subjective_analysis", "evidence_ids": []},
        {"title": "Offline theme two", "statement": "Another offline fixture theme.", "classification": "subjective_analysis", "evidence_ids": []},
    ])
    j["final_prediction"] = prediction
    payload["prediction"] = prediction
    payload["objective_evidence"] = copy.deepcopy(c["objective_evidence"])
    j.setdefault("analysis_article_evidence_ids", [])
    material = payload.get("editorial_material") or []
    payload.setdefault("quality_review_input_sha256", None)
    projection = build_editorial_projection(c, prediction, material)
    findings = validate_publication_hard_facts(j, projection)
    if findings:
        raise ValueError("offline_fixture_hard_fact_blocker: " + json.dumps(findings, ensure_ascii=False))
    payload["editorial_projection"] = projection
    payload["editorial_material"] = material
    quality_review = payload.get("quality_review") or {
        "status": "disabled", "warnings": [], "possible_hard_fact_conflicts": []}
    payload["publication_receipt"] = make_publication_receipt(j, c, projection, material, [],
        quality_review, quality_review_input_sha256=payload.get("quality_review_input_sha256"))
    return payload


def protocol_reviewer(messages, schema):
    """A neutral protocol double, not a claim-to-source judge."""
    return {"status": "warning", "warnings": ["Injected offline quality feedback."],
            "possible_hard_fact_conflicts": []}, {"fixture": True}


def protocol_trace(judgment, context):
    """Hash-bind a service test double to its actual frozen input context."""
    c = context.model_dump() if hasattr(context, "model_dump") else context
    payload = {"judgment": judgment, "prediction": judgment["final_prediction"],
               "analyst_context": copy.deepcopy(c)}
    seal_fixture_payload(payload)
    return {key: payload[key] for key in ("editorial_projection", "editorial_material", "publication_receipt")}
