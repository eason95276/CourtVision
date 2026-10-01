# Historical Replay and Benchmark

This tool evaluates a saved forecast or calls an offline predictor adapter with a frozen pregame snapshot. It never downloads data, calls a search provider, or reads a game result before prediction finishes.

## Snapshot contract

A snapshot JSON file must contain:

- `schema_version`: `pregame-replay-snapshot-v1`
- `capture_kind`: `point_in_time_pregame`
- `snapshot_id`
- `game`: game ID, scheduled tipoff, home and away team IDs, and team names
- `cutoff`: timezone aware ISO timestamp strictly before tipoff
- `captured_at`: timezone aware snapshot time at or before the cutoff
- `source_records`: at least one source ID and a timezone aware capture time at or before the cutoff; publication time is optional, and when present must also be timezone aware and at or before the cutoff
- `pregame_inputs`: the exact frozen inputs available to a predictor
- `pregame_inputs_sha256`: SHA 256 of the canonical JSON representation of `pregame_inputs`

For saved forecast evaluation, also include `prediction_created_at`, `prediction_version`, `prediction_source`, and `predictions.official_numeric`. Optional `pure`, `market`, and `residual` layers remain separately evaluated. AI narrative output is not a numeric forecast layer.

Compute the input hash when assembling the snapshot:

```python
from v2.pregame_system.historical_replay import pregame_inputs_sha256

snapshot["pregame_inputs_sha256"] = pregame_inputs_sha256(snapshot["pregame_inputs"])
```

The hash protects the stored input values from accidental edits. It does not prove that the declared source capture times are authentic. A trusted collector still needs to preserve source receipts and capture timestamps.

## Result file and command

Keep outcomes in a separate JSON file with a top level `games` array. Each row needs `game_id`, `home_team_id`, `away_team_id`, `home_score`, and `away_score`.

```powershell
python -m v2.pregame_system.historical_replay `
  --game-id fixture:game-a `
  --cutoff 2026-01-01T12:30:00Z `
  --snapshot path\to\pregame-snapshot.json `
  --actual-results path\to\final-results.json
```

The actual result file is opened only after the saved forecast has passed validation, or after the predictor adapter returns. `--output` writes a new JSON report and refuses to overwrite an existing file. Without it, the report is printed to the terminal.

The Python API accepts a `predictor(ReplayInput)` callback. The callback receives immutable game identity, cutoff, snapshot ID, and pregame inputs. It does not receive the outcome provider. Return `prediction_version`, `prediction_source`, and separate numeric forecast layers. A callback executed now is a deterministic replay from archived inputs, not an originally published forecast.

## Evaluation fields

For each saved layer, the report records predicted and actual winner, winner correctness, predicted and actual home margin, margin absolute error, predicted and actual total, total absolute error, and optional home win probability with Brier score and log loss. Official, Pure, Market, and Residual values never get blended. Missing probability stays unavailable.

The tests use synthetic inputs and a fake predictor only to verify isolation and scoring contracts. Those values are not prediction quality results.

## Current repository boundary

The current checkout does not contain a complete, verified historical pregame snapshot set for this contract. Existing historical predictions built from retrospectively acquired box scores do not establish what data had arrived before the original cutoff. The runner therefore reports a missing or invalid snapshot instead of falling back to current app data, current rosters, current web search, or current market pages.

