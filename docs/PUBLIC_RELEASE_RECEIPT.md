# Public release receipt · 2026-10-13

## Verified deployment

- Repository: https://github.com/eason95276/CourtVision (public, MIT for original code).
- Website: https://eason95276.github.io/CourtVision/ (HTTP200, HTTPS enforced).
- Successful build/deploy: https://github.com/eason95276/CourtVision/actions/runs/38368297289
- Deployed source commit: `c6942c0bae0af9125fb1f40e8371efdc41280bfa`.
- Subsequent receipt/screenshots commit changes documentation only, not website
  renderer or published game data. Its automatic deployment follows the same workflow.
- Reused initial source repository/history; no force push or private-workspace commit.

## Actual release contents

109 Pregame Python source files,28 Postgame files and150 Evaluation source files,
plus original analysis, native frontend, schemas, SQL, runtime Skills, rubric,
representative RCA, verified metrics, tracing adapters and publication tooling.
474 tracked files before adding this receipt and live screenshots. Counts describe
published source, not whole-production runtime coverage. Private corpora are excluded.

Four catalog-backed games: Lakers@Warriors, Heat@Raptors, Knicks@76ers with saved
Pregame/Postgame; Celtics@Pistons facts-only, no published prediction. These are
saved displays, not regenerated articles or a claim of live daily coverage.

## Cloud verification

Cloud CI:11 public release tests pass;40 contract regression tests pass;1 private
baseline test deselected (not skipped-as-success). No failures in the successful run.
Tree/history scanner:474 files,512 historical blobs,0 findings or Python syntax errors.
JavaScript syntax/build/deploy pass. The first failed CI run remains visible; its
missing Pydantic dependency was corrected without changing production or assertions.

## Actual HTTPS browser check

Edge opened the real Pages URL, navigated from home to Lakers/Warriors and reloaded
the game hash route. Before/after selection matches:

- Version `ffc2947f-ae38-424c-894f-c2f05d71c3d6`.
- Warriors55%, expected winner margin3, total223.5.
- Pregame article and Postgame reader open successfully.
- Tested path:0 page exceptions,0 non-GET requests,0 backend/API/localhost requests.
- Desktop1366×768 and mobile390×844 inspected; mobile no horizontal overflow.
- Captures: `live-home-desktop.png`, `live-product-desktop.png`,
  `live-pregame-desktop.png`, `live-postgame-mobile.png` under `docs/images`.

Initial browser navigation timed out once, but the real page subsequently loaded;
subsequent public reload/navigation checks succeeded. Initial local ESPN logo requests
timed out; live screenshots show the logos loaded. External CDN availability is not
guaranteed, and the existing team abbreviation fallback remains.

HTTPS public JSON was independently retrieved:4 game records and the same Lakers
version/public projection checksum as local. No local Python process is needed to
browse the site. GitHub Actions hosts only static build artifacts.

## Protection and remaining manual step

Private store still has5 Prediction and7 Postgame versions; every payload's SHA-256
matches its stored hash. The five prediction hashes match the pre-release check.
No ML bundles, private database, original requests, keys, account cookies, raw
third-party news or full evaluation dataset were uploaded. No LLM/Judge/generation
call was made. Original dirty/untracked engineering work remains untouched apart
from additive release helpers and plan documentation.

`weitao.si` was supplied by the owner. It is **not yet bound**. No registrar access
or DNS mutation was performed. Default Pages remains live; owner must follow
`PUBLIC_DEPLOYMENT.md` for DNS and domain/HTTPS verification. The first DNS check
showed parking rather than Pages; the later local DNS lookup timed out, so no claim
of current authoritative DNS completion is made.

This release is public-display readiness, not automatic FINAL scheduling readiness,
human-calibrated evaluation, expert fact certification or predictive accuracy proof.
