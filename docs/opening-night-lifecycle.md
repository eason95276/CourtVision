# Opening Night: EARLY baseline → qualified FINAL delta

This release enables only three 2026–27 Opening Night games: Boston at Detroit,
Philadelphia at New York, and Oklahoma City at San Antonio. Previous portfolio
examples remain available. The website is a read-only publication, not an online
model endpoint.

## Execution boundary

- EARLY is the full production Research / Forecast / Market Review / Editorial
  chain, preferably scheduled in the provider's off-peak window.
- T−20 starts HTTP-only readiness checks, followed by T−15, T−10, T−5 and bounded
  30-second checks near tipoff. Waiting does not call a model or consume a
  generation attempt.
- FINAL requires two source-confirmed five-player lineups, verified current
  identities, explicit important-player availability, complete fresh injuries,
  and a fresh qualified spread and total. Unknown or conflicting states hold
  the EARLY version; time passing is not evidence of readiness.
- FINAL loads the immutable EARLY version and builds a delta. It does not
  recompute ML features, query Player Knowledge again, or repeat unchanged
  Research. Changed basketball information can trigger a compact reassessment;
  material price signals can trigger Market Review. The existing Writer,
  factual publication checks and immutable version store remain authoritative.
- First reliable confirmation of an important player's availability is sent to
  Change Assessment even when the projected five names are unchanged. The
  assessment may retain all four numbers; confirmation is not an instruction
  to increase a probability or spread mechanically.
- Small quote changes do not automatically rewrite predictions. Direction
  changes, crossing the existing baseline's spread/total direction, verified
  distinct-book movements, or basketball-linked changes are review signals.
  A market volatility distribution has not been established; no invented
  absolute movement threshold is presented as statistically calibrated.
- The deadline and publication lock still apply. If too little safe generation
  budget remains, the system retains EARLY rather than inventing a FINAL.

## Publication and costs

Prediction, article, lineup evidence, cutoff and hashes belong to the same saved
version. A scoped, opt-in background exporter can publish a legally saved FINAL
to GitHub Pages without re-generating it. A deployment error does not invalidate
the saved prediction. Deployments are asynchronous, not instantaneous.

The internal production ledger records actual provider token/cache usage,
component/stage, failures, bounded repairs, retries and dated unit prices.
Evaluation/Judge calls are outside its production scope. Search credits and
incremental cash fees are distinct; missing usage or unverified prices are not
silently treated as zero. No claim about actual FINAL savings is made before a
real FINAL execution.

## Operating limitations

- Windows scheduled execution uses the existing runtime, explicit three-game
  activation and a FINAL-only stage allowlist. Wake-to-run is requested, but
  actual waking depends on hardware, AC power and an existing logged-in session.
  No permanent system power policy is changed.
- ESPN starter/status fields and official injury availability can be incomplete;
  unavailable confirmations intentionally prevent FINAL publication.
- Reliable minute restrictions/news can be represented in the incremental input,
  but the lightweight HTTP collector does not guarantee coverage of interviews,
  all late news, or multiple sportsbooks. Missing data is not manufactured.
- New versions are append-only. Historical Prediction/Postgame payloads and
  frozen Evaluation outputs are not migrated or rewritten.
