PRAGMA foreign_keys = ON;

CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    game_date TEXT NOT NULL,
    season TEXT NOT NULL,
    season_type TEXT NOT NULL,
    away_team_id INTEGER NOT NULL,
    away_team_tricode TEXT NOT NULL,
    away_points INTEGER NOT NULL CHECK (away_points >= 0),
    home_team_id INTEGER NOT NULL,
    home_team_tricode TEXT NOT NULL,
    home_points INTEGER NOT NULL CHECK (home_points >= 0),
    CHECK (away_team_id <> home_team_id),
    CHECK (length(game_id) > 0),
    CHECK (length(game_date) = 10)
);

CREATE TABLE team_boxscores (
    game_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('away', 'home')),
    team_id INTEGER NOT NULL,
    team_city TEXT NOT NULL,
    team_name TEXT NOT NULL,
    team_tricode TEXT NOT NULL,
    minutes TEXT NOT NULL,
    field_goals_made INTEGER NOT NULL CHECK (field_goals_made >= 0),
    field_goals_attempted INTEGER NOT NULL CHECK (field_goals_attempted >= field_goals_made),
    field_goals_percentage REAL NOT NULL CHECK (field_goals_percentage BETWEEN 0.0 AND 1.0),
    three_pointers_made INTEGER NOT NULL CHECK (three_pointers_made >= 0),
    three_pointers_attempted INTEGER NOT NULL CHECK (three_pointers_attempted >= three_pointers_made),
    three_pointers_percentage REAL NOT NULL CHECK (three_pointers_percentage BETWEEN 0.0 AND 1.0),
    free_throws_made INTEGER NOT NULL CHECK (free_throws_made >= 0),
    free_throws_attempted INTEGER NOT NULL CHECK (free_throws_attempted >= free_throws_made),
    free_throws_percentage REAL NOT NULL CHECK (free_throws_percentage BETWEEN 0.0 AND 1.0),
    rebounds_offensive INTEGER NOT NULL CHECK (rebounds_offensive >= 0),
    rebounds_defensive INTEGER NOT NULL CHECK (rebounds_defensive >= 0),
    rebounds_total INTEGER NOT NULL CHECK (rebounds_total >= 0),
    assists INTEGER NOT NULL CHECK (assists >= 0),
    steals INTEGER NOT NULL CHECK (steals >= 0),
    blocks INTEGER NOT NULL CHECK (blocks >= 0),
    turnovers INTEGER NOT NULL CHECK (turnovers >= 0),
    fouls_personal INTEGER NOT NULL CHECK (fouls_personal >= 0),
    points INTEGER NOT NULL CHECK (points >= 0),
    plus_minus_points REAL NOT NULL,
    PRIMARY KEY (game_id, team_id),
    UNIQUE (game_id, side),
    FOREIGN KEY (game_id) REFERENCES games (game_id) ON DELETE CASCADE
);

CREATE TABLE player_boxscores (
    game_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team_tricode TEXT NOT NULL,
    person_id INTEGER NOT NULL,
    first_name TEXT NOT NULL,
    family_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    name_i TEXT NOT NULL,
    position TEXT,
    starter INTEGER NOT NULL CHECK (starter IN (0, 1)),
    comment TEXT,
    jersey_num TEXT,
    minutes TEXT,
    field_goals_made INTEGER NOT NULL CHECK (field_goals_made >= 0),
    field_goals_attempted INTEGER NOT NULL CHECK (field_goals_attempted >= field_goals_made),
    field_goals_percentage REAL NOT NULL CHECK (field_goals_percentage BETWEEN 0.0 AND 1.0),
    three_pointers_made INTEGER NOT NULL CHECK (three_pointers_made >= 0),
    three_pointers_attempted INTEGER NOT NULL CHECK (three_pointers_attempted >= three_pointers_made),
    three_pointers_percentage REAL NOT NULL CHECK (three_pointers_percentage BETWEEN 0.0 AND 1.0),
    free_throws_made INTEGER NOT NULL CHECK (free_throws_made >= 0),
    free_throws_attempted INTEGER NOT NULL CHECK (free_throws_attempted >= free_throws_made),
    free_throws_percentage REAL NOT NULL CHECK (free_throws_percentage BETWEEN 0.0 AND 1.0),
    rebounds_offensive INTEGER NOT NULL CHECK (rebounds_offensive >= 0),
    rebounds_defensive INTEGER NOT NULL CHECK (rebounds_defensive >= 0),
    rebounds_total INTEGER NOT NULL CHECK (rebounds_total >= 0),
    assists INTEGER NOT NULL CHECK (assists >= 0),
    steals INTEGER NOT NULL CHECK (steals >= 0),
    blocks INTEGER NOT NULL CHECK (blocks >= 0),
    turnovers INTEGER NOT NULL CHECK (turnovers >= 0),
    fouls_personal INTEGER NOT NULL CHECK (fouls_personal >= 0),
    points INTEGER NOT NULL CHECK (points >= 0),
    plus_minus_points REAL NOT NULL,
    PRIMARY KEY (game_id, person_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id) ON DELETE CASCADE,
    FOREIGN KEY (game_id, team_id)
        REFERENCES team_boxscores (game_id, team_id) ON DELETE CASCADE
);

CREATE TABLE play_by_play (
    game_id TEXT NOT NULL,
    action_id INTEGER NOT NULL,
    action_number INTEGER NOT NULL,
    clock TEXT NOT NULL,
    period INTEGER NOT NULL CHECK (period > 0),
    team_id INTEGER,
    team_tricode TEXT,
    person_id INTEGER,
    player_name TEXT,
    player_name_i TEXT,
    x_legacy INTEGER NOT NULL,
    y_legacy INTEGER NOT NULL,
    shot_distance INTEGER NOT NULL CHECK (shot_distance >= 0),
    shot_result TEXT,
    is_field_goal INTEGER NOT NULL CHECK (is_field_goal IN (0, 1)),
    score_home INTEGER,
    score_away INTEGER,
    points_total INTEGER NOT NULL CHECK (points_total >= 0),
    location TEXT,
    description TEXT NOT NULL,
    action_type TEXT,
    sub_type TEXT,
    video_available INTEGER NOT NULL CHECK (video_available IN (0, 1)),
    shot_value INTEGER NOT NULL CHECK (shot_value >= 0),
    PRIMARY KEY (game_id, action_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id) ON DELETE CASCADE,
    FOREIGN KEY (game_id, team_id)
        REFERENCES team_boxscores (game_id, team_id) ON DELETE CASCADE
);

CREATE TABLE shots (
    grid_type TEXT NOT NULL,
    game_id TEXT NOT NULL,
    game_event_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    period INTEGER NOT NULL CHECK (period > 0),
    minutes_remaining INTEGER NOT NULL CHECK (minutes_remaining >= 0),
    seconds_remaining INTEGER NOT NULL CHECK (seconds_remaining BETWEEN 0 AND 59),
    event_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    shot_type TEXT NOT NULL,
    shot_zone_basic TEXT NOT NULL,
    shot_zone_area TEXT NOT NULL,
    shot_zone_range TEXT NOT NULL,
    shot_distance INTEGER NOT NULL CHECK (shot_distance >= 0),
    loc_x INTEGER NOT NULL,
    loc_y INTEGER NOT NULL,
    shot_attempted_flag INTEGER NOT NULL CHECK (shot_attempted_flag IN (0, 1)),
    shot_made_flag INTEGER NOT NULL CHECK (shot_made_flag IN (0, 1)),
    game_date TEXT NOT NULL,
    htm TEXT NOT NULL,
    vtm TEXT NOT NULL,
    PRIMARY KEY (game_id, game_event_id, player_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id) ON DELETE CASCADE,
    FOREIGN KEY (game_id, team_id)
        REFERENCES team_boxscores (game_id, team_id) ON DELETE CASCADE,
    FOREIGN KEY (game_id, player_id)
        REFERENCES player_boxscores (game_id, person_id) ON DELETE CASCADE
);

CREATE INDEX idx_team_boxscores_team
    ON team_boxscores (team_id, game_id);

CREATE INDEX idx_player_boxscores_team
    ON player_boxscores (team_id, game_id);

CREATE INDEX idx_player_boxscores_person
    ON player_boxscores (person_id, game_id);

CREATE INDEX idx_play_by_play_game_period
    ON play_by_play (game_id, period, action_id);

CREATE INDEX idx_play_by_play_team
    ON play_by_play (game_id, team_id);

CREATE INDEX idx_play_by_play_person
    ON play_by_play (person_id, game_id);

CREATE INDEX idx_shots_game_period
    ON shots (game_id, period, game_event_id);

CREATE INDEX idx_shots_team
    ON shots (game_id, team_id);

CREATE INDEX idx_shots_player
    ON shots (game_id, player_id);
