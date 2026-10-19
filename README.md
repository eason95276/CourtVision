# CourtVision · 第25秒

**NBA Analysis Agent + LLM Evaluation / AI Quality engineering.**

[Live product](https://nba.weitao.si/) ·
[Architecture](docs/ARCHITECTURE.md) · [Evaluation evidence](evaluation/reports/FINAL_AI_HANDOFF.md) ·
[Publish/deploy](docs/PUBLIC_DEPLOYMENT.md)

CourtVision combines verified basketball inputs, independent forecasting, market
review and editorial analysis, preserving immutable predictions for postgame review.
The website reuses the real product frontend. Generation stays local, not on visitors'
clicks. 中文 NBA 分析产品，配套可追溯评测工程；胜负、让分和大小分分开解释，
缺失信息保持未知，失败和修复尝试不被成功案例掩盖。

![CourtVision — opening-night 76ers at Knicks pregame analysis](docs/images/opening-night-76ers-knicks-desktop.png)

Opening-night product view: **76ers at Knicks**, showing the saved pregame story,
prediction and market reference. Magazine-style basketball analysis with a
shot-clock identity; the page reads published content without generating a report.

<details>
<summary>Mobile product view</summary>

<img src="docs/images/opening-night-76ers-knicks-mobile.png" width="390" alt="CourtVision mobile: opening-night 76ers at Knicks pregame analysis" />

</details>

## Architecture

```text
Roster/history/Player Knowledge + time-bounded Research
 → Independent Forecast → qualified Market Review → locked FinalPrediction
 → Editorial / hard-fact boundaries → immutable Prediction Version
 → deterministic Postgame → optional Postgame Writer

Frozen context → component attempts → schema/acceptance → RCA/regression
 → DeepEval / Judge / independent Human workspace → Phoenix tracing

Saved display projection → GitHub → Pages → read-only native frontend
```

## Verified Final V1 evaluation (2026-10-13)

| Evidence | Result | Boundary |
|---|---:|---|
| Historical contexts | 1,231 + paired anonymous views | Not 2,462 independent games |
| Static benchmark | 300, 30 teams | **Not 300 LLM executions** |
| Golden | 20, 30 teams | 12 representative + 8 challenge |
| Final raw articles / qualified chains | 15/20 / 14/20 | Execution reliability, **not prediction accuracy** |
| Raw provider / parse / schema | 20/20 / 18/20 / 7/20 | Recovery does not hide raw compliance |
| Bounded schema repair | 2/5 recovered | Failed attempts retained |
| Final DeepEval | 160 checks, 128 pass / 32 fail | 20 × 8 properties; all properties pass 6/20 |
| Judge | 14 valid reviews | Same-family, not human-calibrated |
| Human labels | 0 | Workspace ready, evaluation unfinished |
| Archived regression | 47/47 expected | 27 accept + 20 reject, 20 samples |
| Final Phoenix | 85 spans | Not public production monitoring |

Rubric: **5 hard gates + 6 soft metrics**. Execution, raw schema, recovery and content
quality have separate denominators. Final uses a historical adapter with
`production_equivalence=False` and Pure unavailable. No accuracy uplift, expert
certification or betting-profit claim. Automatic final scheduling remains a prototype.
[Representative RCA](evaluation/finalization/RCA_STORIES.md) includes identity scope,
Decimal validation and the distinction between 15 articles and 14 qualified chains.

## Offline prediction modeling experiments

[LightGBM / XGBoost / ARIMA experiments](experiments/predictive_benchmark/README.md)
include chronological model comparisons, training-only early stopping, and
team-points walk-forward forecasting. [Executed results](experiments/predictive_benchmark/RESULTS.md)
cover three tabular tasks and 492 team-game time-series targets. Code, fixed metadata
and aggregate results are public; raw data and per-game outputs are not distributed.
These are offline experiments, not production model replacements or accuracy-uplift claims.

## Run the public product: no keys or database

Python 3.12+ and a browser:

```sh
python -m unittest discover -s tests/public -v
python scripts/check_public_release.py --history
python scripts/build_public_site.py
python -m http.server 8000 --directory _site
```

Open `http://localhost:8000`. No Node install is needed for the website. Hash routes
support reload and project paths. Optional local Agent/evaluation needs additional
dependencies/assets. `requirements.txt` is the original dependency snapshot, not a
turnkey public-data training recipe.

Optional offline contract regression: install `requirements-evaluation.txt`, then
run `python -m pytest evaluation/candidate_v4/test_contracts_v4.py evaluation/candidate_v2/test_contracts.py -k "not baseline"`.
The excluded baseline check needs the non-public frozen corpus; it is not a pass.

## Repository

| Path | Responsibility |
|---|---|
| `src/`, `analysis_v1/` | Original analysis/modeling foundation |
| `v2/pregame_system/` | Evidence, numeric contracts, forecast, publication, versions |
| `v2/postgame_system/` | Deterministic review and optional Writer |
| `v2/frontend/` | Native product renderer and local server |
| `agent/skills/` | Runtime Research, Forecasting, Editorial and Postgame principles |
| `evaluation/` | Dataset/benchmark builders, closed-book execution, rubric, Judge, regression, tracing adapters |
| `experiments/predictive_benchmark/` | Offline LightGBM/XGBoost comparison and ARIMA walk-forward study |
| `public_data/` | Small sanitized real examples and schema |
| `web/`, `scripts/` | Static adapter, build, public safety checks |
| `.github/workflows/pages.yml` | Automatic checked Pages deployment |

Private data, databases, model bundles, sessions and keys are absent. Original
engineering evidence remains local. [Public contract](docs/PUBLIC_DATA_CONTRACT.md)
separates original private payload hashes from public projection hashes.

## Publish a saved local game

```sh
python publish_game.py espn:YOUR_GAME_ID --api-base http://127.0.0.1:8625
python scripts/check_public_release.py
python scripts/build_public_site.py
git add public_data
git commit -m "Publish saved game analysis"
git push origin main
```

The local backend is needed only for export. No Research/AI is rerun. See
[deployment](docs/PUBLIC_DEPLOYMENT.md) and [checklist](docs/PUBLIC_RELEASE_CHECKLIST.md).
Original code: MIT; third-party rights excluded. [Notices](THIRD_PARTY_NOTICES.md).
