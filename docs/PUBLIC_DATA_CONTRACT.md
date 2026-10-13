# Public Data Contract V1

The static site consumes `public_data/index.json` and named records in
`public_data/games/<namespace>_<id>.json`. This is a read-only positive projection,
not a copy of the private database or a reconstructed prediction.

Each record contains `game`, server-selected `prediction`, optional `postgame`,
qualified saved `market`, and `publication`. Namespaced game identities, actual
scores, times, source URLs, selected version ID, article, prediction fields and
lineup basis remain bound to their selected saved version. Missing reports are
explicitly unavailable; facts-only games have no synthetic prediction.

`payload_sha256` identifies the **original immutable private prediction**. It is
not the checksum of the reduced public object. `public_sha256` is SHA-256 over the
complete public record **excluding that checksum field**, canonical UTF-8 JSON
(sorted keys, no whitespace, Unicode preserved). Index repeats that public hash.
This allows independent integrity checks without exposing the full private payload.

Raw requests, provider response bodies, raw article quotes, local file paths,
database provenance and credentials are excluded. Minimal evidence identity
references retain IDs and names for existing lineup display checks; names-only
`quote` fields are publication references, **not verbatim news quotations**.
They must not be consumed as new Research evidence.

`publication.mode` is `saved-read-only-projection`. Portfolio manual editorial
sidecars, when present in the selected display projection, are curated display
content, not an assertion of untouched LLM quality. Historical versions remain in
the private append-only store. Updating this public projection does not mutate them.

The public site shows a published archive, not live schedule coverage. All displayed
time conversions use Asia/Shanghai. It does not call any backend, models or tools.
