"""Strict tool arguments for large production forecasts; local validators remain authoritative."""
from __future__ import annotations

import json
import copy
import hashlib
import urllib.request
from types import SimpleNamespace

ENDPOINT = "https://api.deepseek.com/beta/chat/completions"
TOOL_NAME = "submit_forecast"


def strict_schema(schema, roster_catalog=None):
    definitions = schema.get("$defs", {})

    def convert(value):
        if "$ref" in value:
            return convert(definitions[value["$ref"].rsplit("/", 1)[-1]])
        result = {key: value[key] for key in ("type", "enum", "description", "minimum", "maximum",
            "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern") if key in value}
        hints = [f"{key}={value[key]}" for key in ("minLength", "maxLength", "minItems", "maxItems") if key in value]
        if hints:
            result["description"] = (result.get("description", "") + " Local validation requires: " + ", ".join(hints) + ".").strip()
        if "const" in value:
            result["enum"] = [value["const"]]
        if "anyOf" in value:
            result["anyOf"] = [convert(item) for item in value["anyOf"]]
        if value.get("type") == "object":
            result["properties"] = {key: convert(item) for key, item in value.get("properties", {}).items()}
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        if value.get("type") == "array":
            result["items"] = convert(value["items"])
        return result

    result = convert(schema)
    def encode_candidates(value):
        if isinstance(value, list):
            return [encode_candidates(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: encode_candidates(item) for key, item in value.items()}
        fields = value.get("properties", {})
        candidate = fields.get("candidates")
        if candidate and candidate.get("type") == "array":
            fields["candidates"] = {"type": "object", "additionalProperties": False,
                "description": candidate.get("description", "") + " Five ordered optional slots; null means no candidate in this slot.",
                "properties": {f"slot_{i}": {"anyOf": [copy.deepcopy(candidate["items"]), {"type": "null"}]} for i in range(1, 6)},
                "required": [f"slot_{i}" for i in range(1, 6)]}
        return value

    if not roster_catalog or not all(roster_catalog.get(side) for side in ("home", "away")):
        return encode_candidates(result)

    def bind(value):
        if isinstance(value, list):
            return [bind(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: bind(item) for key, item in value.items()}
        properties = value.get("properties", {})
        if "side" not in properties or not ({"players", "candidates"} <= set(properties) or "player_name" in properties):
            return value
        variants = []
        for side in ("home", "away"):
            variant = copy.deepcopy(value)
            fields = variant["properties"]
            fields["side"]["enum"] = [side]
            for field in ("players", "candidates"):
                if field in fields:
                    fields[field]["items"]["enum"] = roster_catalog[side]
            if "player_name" in fields:
                fields["player_name"]["enum"] = roster_catalog[side]
            variants.append(variant)
        return {"anyOf": variants}

    return encode_candidates(bind(result))


def decode_candidate_slots(value):
    """Lossless transport decoding: only optional reserve slots become an ordered list."""
    changes = []
    result = copy.deepcopy(value)
    plan = result.get("pregame_plan")
    if isinstance(plan, dict):
        for index, lineup in enumerate(plan.get("lineups") or []):
            candidates = lineup.get("candidates")
            if not isinstance(candidates, dict) or set(candidates) != {f"slot_{i}" for i in range(1, 6)}:
                raise ValueError("strict_forecast_candidate_slots_invalid")
            ordered = [candidates[f"slot_{i}"] for i in range(1, 6) if candidates[f"slot_{i}"] is not None]
            if any(not isinstance(name, str) or not name for name in ordered):
                raise ValueError("strict_forecast_candidate_value_invalid")
            lineup["candidates"] = ordered
            changes.append({"path": f"pregame_plan.lineups.{index}.candidates", "from": candidates, "to": ordered})
    return result, changes


def request_strict_forecast(messages, api_key, model, schema, max_tokens, timeout_seconds, *, opener=None):
    from .cost_ledger import metered_deepseek
    context = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
    context = context.get("basketball") or context
    parameters = strict_schema(schema.model_json_schema(), context.get("allowed_current_players_by_side"))
    payload = {
        "model": model, "messages": messages, "temperature": 0.1,
        "max_tokens": max_tokens, "thinking": {"type": "disabled"}, "stream": False,
        "tools": [{"type": "function", "function": {
            "name": TOOL_NAME, "description": "Submit this stage's complete forecast using only the supplied inputs.",
            "strict": True, "parameters": parameters,
        }}],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
    }

    def invoke(observed_opener):
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST")
        with (observed_opener or urllib.request.urlopen)(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("strict_forecast_output_truncated")
        calls = choice.get("message", {}).get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != TOOL_NAME:
            raise ValueError("strict_forecast_tool_response_invalid")
        arguments = calls[0]["function"].get("arguments")
        if not isinstance(arguments, str) or not isinstance(json.loads(arguments), dict):
            raise ValueError("strict_forecast_arguments_invalid")
        parsed = json.loads(arguments)
        normalized, changes = decode_candidate_slots(parsed)
        content = json.dumps(normalized, ensure_ascii=False) if changes else arguments
        return SimpleNamespace(content=content, finish_reason=choice.get("finish_reason"), usage=data.get("usage") or {},
            response_metadata={**{key: data[key] for key in ("id", "model", "created", "system_fingerprint") if key in data},
                "output_protocol": "strict_tool_v2", "tool_name": TOOL_NAME,
                "raw_tool_arguments": arguments, "structural_decoding": changes,
                "tool_schema_sha256": hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})

    return metered_deepseek(invoke, model, opener)
