# Public release verification · 2026-10-13

## Scope and bounded reproducibility

Existing `eason95276/CourtVision` repository reused without force-push or deleting
history. A separate public checkout receives reviewed source-only copies; private
dirty/untracked development files and frozen outputs remain local. Source portability
edits remove machine-specific paths **in copies only**. Full Agent/evaluation reruns
still need private assets; static display has no such dependency.

## Verified before push

- 11 public release unittest cases passed; 0 failed/skipped.
- 18 existing Decimal contract tests passed from the **public checkout**.
- 22 synthetic historical contract tests passed from the public checkout; one private
  baseline-asset protection test explicitly deselected because that corpus is not
  redistributed. This is not 23 passed.
- All public Python files AST-parsed; generated app/editorial/adapter JavaScript syntax
  checks passed; static build succeeded; `git diff --check` passed.
- Tree and initial Git-history content scanner: 0 findings. Checks include token/key
  signatures, assigned credentials, private user paths, forbidden DB/model/key assets,
  oversized files. This is a bounded scan, not a formal security/third-party-license
  certification. New commit history is scanned again before push.

## Browser verification (local static server, Edge)

- Desktop 1366×768: real home, Lakers/Warriors detail, Pregame and Postgame opened.
- Mobile 390×844: detail and Pregame readable; document width390, no horizontal overflow.
- Reload restored version `ffc2947f-ae38-424c-894f-c2f05d71c3d6`:
  Warriors55%, expected winner margin3, total223.5; article belongs to that selection.
- Facts-only Boston/Detroit: no prediction before or after reload, explicit missing
  report rather than an old demo forecast. It is a catalog-backed registered game.
- Tested flows produced no JS page exceptions and no non-GET requests. Static adapter
  reads JSON only; there were no requests to local generation/backend endpoints.
- External ESPN logos timed out on this test machine; existing abbreviation fallback
  remained functional. **Do not claim zero network errors or guaranteed logo delivery.**
  Local CSS/JS/data loaded. Logo packs are not redistributed without established rights.

Screenshots: `docs/images/` (actual browser captures). Public HTTPS deployment requires
its own final check, recorded separately in the final release receipt.

## Source protections

Original published5 Prediction and7 Postgame payloads are not rewritten. Public
projection hashes are separate from source payload hashes. No model, Judge, Research,
Scheduler or Postgame generation was invoked for this release. Local publish helpers
are additive; production numerical, evidence and publication logic is unchanged.
