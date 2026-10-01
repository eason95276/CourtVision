# CourtVision Data Dictionary — Schema Only

This public copy documents table roles, keys, and time semantics. It contains no source rows, row counts, match results, odds, player records, or model outputs. Source-data access and redistribution rights must be reviewed separately.

## Table groups

| Table | One row represents | Primary key | Time boundary |
|---|---|---|---|
| `dim_team` | A team/franchise reference | `team_id` | Reference data; name/history rules depend on the source. |
| `dim_season` | A season reference | `season_start` | Season dates come from schedule data. |
| `dim_game` | A scheduled or completed game | `game_id` | Schedule fields may be pregame; scores and outcome labels are postgame. |
| `fact_team_game` | One team's record for one game | `game_id, team_id` | `pregame_*` features must be shifted and cutoff-checked; results belong to postgame evaluation. |
| `fact_prediction` | A model forecast for one game | `game_id` | A stored value is not proof of a genuine pregame forecast unless creation time and input cutoff are persisted. |
| `fact_prediction_error` | A forecast compared with a later result | `game_id` | Postgame only. |
| `fact_market_benchmark` | A market reference for one game | `game_id` | Treat as a dated market reference only when quote identity and capture time are evidenced. |
| `fact_data_quality_event` | A recorded validation outcome | `quality_event_id` | Describes a check; it does not prove checks that were not implemented. |
| `fact_model_stability` | A model evaluation slice | `stability_id` | Historical evaluation; not a substitute for forward snapshots. |

## Temporal fields

- `pregame_*` fields must be derived only from information available before the prediction cutoff.
- Scores, win/loss labels, margins, totals, and prediction errors are postgame fields.
- Historical market lines without verified capture timestamps must not be presented as opening, closing, or a specific pregame-time quote.
- Replay claims require preserved source records, capture timestamps, frozen inputs, and an input hash.

## Distribution boundary

The public draft includes schema descriptions only. It does not package NBA-derived game records, market rows, player evidence, databases, or model-call records.
