from __future__ import annotations


RANDOM_SEED = 42

HISTORY_START_SEASON = 2010
MODELING_START_SEASON = 2014
VALIDATION_SEASON = 2024
TEST_SEASON = 2025
TRAINING_WINDOWS = (5, 8, 10)
ROLLING_WINDOWS = (5, 15)

RAW_SOURCES = {
    "Games.csv": {
        "dataset": "eoinamoore/historical-nba-data-and-player-box-scores",
        "version": 515,
        "remote_name": "Games.csv",
        "bytes": 11_208_824,
        "sha256": "8b21e7e0ebfe25f1de48c18ec701e08a47974d32be006be044b85b2661dfff29",
    },
    "TeamStatistics.csv": {
        "dataset": "eoinamoore/historical-nba-data-and-player-box-scores",
        "version": 515,
        "remote_name": "TeamStatistics.csv",
        "bytes": 36_020_027,
        "sha256": "37df1d8096252a9a38a1162872fdf85a2e413ea978c8b1d70edd3bd7f4399be9",
    },
    "nba_2008-2026.csv": {
        "dataset": "cviaxmiwnptr/nba-betting-data-october-2007-to-june-2024",
        "version": 4,
        "remote_name": "nba_2008-2026.csv",
        "bytes": 2_493_308,
        "sha256": "729eb2ead85c14affbb81ae9a4c611fee9790b8dd3d1d7824e1ddba14410a8c4",
    },
}

