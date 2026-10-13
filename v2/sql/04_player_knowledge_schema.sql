PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS player_knowledge_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_knowledge_games (
    game_id TEXT PRIMARY KEY,
    season TEXT NOT NULL,
    season_type TEXT NOT NULL CHECK (season_type = 'Regular Season'),
    game_date TEXT NOT NULL,
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL,
    collection_status TEXT NOT NULL DEFAULT 'not_collected'
        CHECK (collection_status IN ('not_collected', 'success', 'failed')),
    latest_success_receipt_id INTEGER,
    CHECK (home_team_id <> away_team_id)
);

CREATE TABLE IF NOT EXISTS player_knowledge_source_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    source_url TEXT NOT NULL,
    client_version TEXT,
    data_version TEXT NOT NULL,
    collection_time_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    request_parameters_json TEXT NOT NULL,
    response_sha256 TEXT,
    source_path TEXT,
    response_bytes INTEGER,
    error_type TEXT,
    error_message TEXT,
    FOREIGN KEY (game_id) REFERENCES player_knowledge_games (game_id),
    UNIQUE (game_id, response_sha256)
);

CREATE INDEX IF NOT EXISTS idx_player_knowledge_receipts_game_time
    ON player_knowledge_source_receipts (game_id, collection_time_utc, receipt_id);

CREATE TABLE IF NOT EXISTS player_game_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    season TEXT NOT NULL,
    game_date TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team_tricode TEXT,
    opponent_team_id INTEGER NOT NULL,
    opponent_tricode TEXT,
    home_away TEXT NOT NULL CHECK (home_away IN ('home', 'away')),
    player_id TEXT,
    player_name TEXT,
    position TEXT,
    jersey_num TEXT,
    participation_status TEXT NOT NULL
        CHECK (participation_status IN ('played', 'did_not_play_confirmed', 'unknown', 'data_missing')),
    starter INTEGER CHECK (starter IS NULL OR starter IN (0, 1)),
    starter_source TEXT,
    minutes_raw TEXT,
    minutes_seconds INTEGER CHECK (minutes_seconds IS NULL OR minutes_seconds >= 0),
    points INTEGER CHECK (points IS NULL OR points >= 0),
    fgm INTEGER CHECK (fgm IS NULL OR fgm >= 0),
    fga INTEGER CHECK (fga IS NULL OR fga >= 0),
    fg_pct REAL,
    three_pm INTEGER CHECK (three_pm IS NULL OR three_pm >= 0),
    three_pa INTEGER CHECK (three_pa IS NULL OR three_pa >= 0),
    three_pct REAL,
    ftm INTEGER CHECK (ftm IS NULL OR ftm >= 0),
    fta INTEGER CHECK (fta IS NULL OR fta >= 0),
    ft_pct REAL,
    offensive_rebounds INTEGER CHECK (offensive_rebounds IS NULL OR offensive_rebounds >= 0),
    defensive_rebounds INTEGER CHECK (defensive_rebounds IS NULL OR defensive_rebounds >= 0),
    rebounds INTEGER CHECK (rebounds IS NULL OR rebounds >= 0),
    assists INTEGER CHECK (assists IS NULL OR assists >= 0),
    turnovers INTEGER CHECK (turnovers IS NULL OR turnovers >= 0),
    steals INTEGER CHECK (steals IS NULL OR steals >= 0),
    blocks INTEGER CHECK (blocks IS NULL OR blocks >= 0),
    fouls INTEGER CHECK (fouls IS NULL OR fouls >= 0),
    plus_minus REAL,
    source_comment TEXT,
    missing_status TEXT NOT NULL
        CHECK (missing_status IN ('none', 'partial_fields', 'not_applicable', 'data_missing', 'unknown')),
    missing_fields_json TEXT NOT NULL,
    field_status_json TEXT NOT NULL,
    normalization_warnings_json TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collection_time_utc TEXT,
    data_version TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES player_knowledge_source_receipts (receipt_id),
    FOREIGN KEY (game_id) REFERENCES player_knowledge_games (game_id),
    UNIQUE (receipt_id, game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_player_game_facts_player_season
    ON player_game_facts (player_id, season, game_date, team_id);

CREATE INDEX IF NOT EXISTS idx_player_game_facts_team_game
    ON player_game_facts (team_id, season, game_date, game_id);

CREATE INDEX IF NOT EXISTS idx_player_game_facts_status
    ON player_game_facts (game_id, participation_status, team_id);
