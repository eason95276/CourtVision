# Saved report publication

Generation and website delivery are separate jobs. Only a legally published Prediction Version can enter the delivery queue. A failed upload never starts Research, forecasting, or another Writer call.

The scheduler opts in with `COURTVISION_PUBLIC_PUBLISH_ROOT`, pointing at a checkout with the verified CourtVision GitHub remote and Git author configuration. The local read API defaults to `http://127.0.0.1:8625`; `COURTVISION_PUBLIC_API_BASE` can select another local reader.

The queue lives beside the version database in `public-release/queue.sqlite`, leaving Prediction and Postgame tables unchanged. Scheduler status includes delivery states and retry errors. On restart, discovery reconciles the authorized games' current or locked versions, including a version committed just before a worker lost its acknowledgment.

Each delivery attempt clones main into its own directory. It exports only saved display data, runs the public checks, and commits the game record, index, and selected-game publication status. It does not edit or clean the development checkout. A non-fast-forward push is retried from a new checkout. An already-delivered version is checked by source and public hashes; a newer remote report is never replaced by an older queued one.

Failures retry with exponential backoff, starting at 30 seconds and capped at 15 minutes. Twelve failed attempts produce a visible terminal failure. Attempt directories are retained for inspection. Queue leases are renewed during validation and Git operations.

This is a local runtime: the computer, local API and credentials must remain available. Network interruptions leave jobs pending for recovery; they are not a cloud deployment or a guarantee of a fixed publication time. GitHub Pages deployment remains a separate CI step after the push.

## Editorial titles

New Writer output must contain two seven-character Chinese lines. Separator normalization never changes words. An invalid title gets at most one title-only correction inside the existing model-call budget. The article, cards and numeric forecast stay fixed, and the resulting title passes the same factual guard before publication. Legacy saved versions remain readable.

The public CI includes title-policy tests and real local-Git delivery tests. Those fixtures prove delivery and formatting contracts; they do not establish prediction quality or certify every sentence. Real-game generation and operator editorial review are recorded separately in runtime acceptance artifacts.
