PRAGMA foreign_keys = ON;

CREATE TABLE schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO schema_metadata (key, value)
VALUES ('schema_version', '1.1');

CREATE TABLE source_fetch_runs (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    game_id TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('injuries', 'lineups', 'market')),
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    captured_at TEXT,
    cutoff_time TEXT,
    http_status INTEGER,
    fetch_status TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    normalized_records INTEGER NOT NULL DEFAULT 0,
    response_sha256 TEXT,
    raw_path TEXT,
    error_type TEXT,
    error_message TEXT,
    suggested_manual_url TEXT,
    suggested_save_path TEXT
);

CREATE TABLE injury_snapshots (
    injury_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_published_at TEXT,
    captured_at TEXT NOT NULL,
    cutoff_time TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    team_id TEXT,
    team_name TEXT NOT NULL,
    player_id TEXT,
    player_name TEXT NOT NULL,
    availability_status TEXT,
    reason TEXT,
    source_url TEXT,
    raw_path TEXT,
    UNIQUE (source, source_record_id)
);

CREATE TABLE lineup_snapshots (
    lineup_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_published_at TEXT,
    captured_at TEXT NOT NULL,
    cutoff_time TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    lineup_kind TEXT NOT NULL CHECK (lineup_kind IN ('expected', 'confirmed')),
    team_id TEXT,
    team_name TEXT NOT NULL,
    player_id TEXT,
    player_name TEXT NOT NULL,
    position TEXT,
    source_url TEXT,
    raw_path TEXT,
    UNIQUE (source, source_record_id)
);

CREATE TABLE market_snapshots (
    market_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_published_at TEXT,
    captured_at TEXT NOT NULL,
    cutoff_time TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    provider_event_id TEXT,
    bookmaker TEXT NOT NULL,
    home_spread REAL,
    total REAL,
    source_url TEXT,
    raw_path TEXT,
    UNIQUE (source, source_record_id)
);

CREATE INDEX idx_injury_agent_cutoff
ON injury_snapshots (game_id, source_published_at, captured_at, cutoff_time);

CREATE INDEX idx_lineup_agent_cutoff
ON lineup_snapshots (game_id, lineup_kind, source_published_at, captured_at, cutoff_time);

CREATE INDEX idx_market_agent_cutoff
ON market_snapshots (game_id, bookmaker, source_published_at, captured_at, cutoff_time);

CREATE VIEW agent_eligible_injury_snapshots AS
SELECT injury.*
FROM injury_snapshots AS injury
WHERE injury.source_published_at IS NOT NULL
  AND injury.source_published_at <= injury.cutoff_time
  AND (
      (
          injury.normalized_status = 'available'
          AND injury.captured_at <= injury.cutoff_time
      )
      OR
      (
          injury.normalized_status = 'available_historical_archive'
          AND injury.source = 'nba_official_injury_report_archive'
          AND injury.source_url LIKE
              'https://ak-static.cms.nba.com/referee/injury/Injury-Report_%.pdf'
          AND injury.raw_path IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM source_fetch_runs AS fetch
              WHERE fetch.game_id = injury.game_id
                AND fetch.source_type = 'injuries'
                AND fetch.source = injury.source
                AND fetch.source_url = injury.source_url
                AND fetch.captured_at = injury.captured_at
                AND fetch.raw_path = injury.raw_path
                AND fetch.fetch_status = 'success'
                AND fetch.normalized_status = 'available_historical_archive'
                AND fetch.response_sha256 IS NOT NULL
          )
      )
  );

CREATE VIEW agent_eligible_lineup_snapshots AS
SELECT *
FROM lineup_snapshots
WHERE normalized_status = 'available'
  AND source_published_at IS NOT NULL
  AND source_published_at <= cutoff_time
  AND captured_at <= cutoff_time;

CREATE VIEW agent_eligible_market_snapshots AS
SELECT *
FROM market_snapshots
WHERE normalized_status = 'available'
  AND source_published_at IS NOT NULL
  AND source_published_at <= cutoff_time
  AND captured_at <= cutoff_time;
