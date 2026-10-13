from __future__ import annotations

from datetime import datetime
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from .storage import local_wall_time_to_utc


@pytest.mark.parametrize(
    ("wall_time", "expected_utc"),
    [
        (datetime(2026, 3, 8, 1, 30), "2026-03-08T06:30:00+00:00"),
        (datetime(2026, 3, 8, 3, 30), "2026-03-08T07:30:00+00:00"),
        (datetime(2026, 11, 1, 0, 30), "2026-11-01T04:30:00+00:00"),
        (datetime(2026, 11, 1, 2, 30), "2026-11-01T07:30:00+00:00"),
    ],
)
def test_eastern_wall_times_convert_across_daylight_saving(wall_time, expected_utc):
    assert local_wall_time_to_utc(wall_time, "America/New_York").isoformat() == expected_utc


def test_nonexistent_spring_forward_wall_time_is_rejected():
    with pytest.raises(ValueError, match="nonexistent_local_datetime"):
        local_wall_time_to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")


def test_ambiguous_fall_back_wall_time_requires_an_explicit_offset():
    with pytest.raises(ValueError, match="ambiguous_local_datetime_requires_offset"):
        local_wall_time_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York")


def test_aware_datetime_is_not_reinterpreted_as_local_wall_time():
    with pytest.raises(ValueError, match="naive_local_datetime_required"):
        local_wall_time_to_utc(
            datetime.fromisoformat("2026-11-01T01:30:00-05:00"),
            "America/New_York",
        )


def test_historical_agent_rejects_ambiguous_and_nonexistent_cutoffs():
    from v2.agent.generate_historical_pregame_analysis import (
        PregameAgentError,
        _cutoff_source_local_to_utc,
    )

    assert _cutoff_source_local_to_utc("2026-03-08T03:30:00") == "2026-03-08T07:30:00Z"
    with pytest.raises(PregameAgentError, match="nonexistent_local_datetime"):
        _cutoff_source_local_to_utc("2026-03-08T02:30:00")
    with pytest.raises(PregameAgentError, match="ambiguous_local_datetime_requires_offset"):
        _cutoff_source_local_to_utc("2026-11-01T01:30:00")


def test_historical_injury_replay_never_searches_or_fetches_after_cutoff(tmp_path, monkeypatch):
    from v2.pregame_system.storage import local_wall_time_to_utc

    official = ModuleType("v2.scripts.import_official_injury_report")
    official.SOURCE = "nba_official_injury_report_archive"
    official.DEFAULT_ANALYSIS_DATABASE = tmp_path / "analysis.sqlite"
    official.DEFAULT_SNAPSHOT_DATABASE = tmp_path / "snapshots.sqlite"

    def cutoff_to_utc(value):
        instant = local_wall_time_to_utc(datetime.fromisoformat(value), "America/New_York")
        return instant, instant.isoformat(timespec="seconds").replace("+00:00", "Z")

    official._cutoff_to_utc = cutoff_to_utc
    monkeypatch.setitem(sys.modules, "v2.scripts.import_official_injury_report", official)
    source = Path(__file__).resolve().parents[1] / "agent" / "historical_injury_lookup.py"
    spec = importlib.util.spec_from_file_location("historical_injury_lookup_offline_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*_args, **_kwargs):
        pytest.fail("replay attempted an online injury source")

    resolver = module.InjuryResolver(
        tmp_path / "cache",
        fetch=forbidden,
        search=forbidden,
        local=lambda *_: None,
        clock=lambda: datetime.fromisoformat("2026-01-14T00:00:00+00:00"),
    )
    cutoff = "2026-01-13T19:00:00"
    cutoff_utc = "2026-01-14T00:00:00Z"
    query = {
        "game_id": "fixture:game",
        "cutoff_time": cutoff_utc,
        "search_policy": "local source captured before cutoff only; no network",
    }
    result = resolver._resolve(
        query,
        tmp_path / "cache" / "query",
        datetime.fromisoformat("2026-01-14T00:30:00+00:00"),
        "fixture:game",
        cutoff,
        replay=True,
    )

    assert result["status"] == "unavailable"
    assert result["route"] == "historical_no_network"
    assert result["request_count"] == 0

    for captured_at, expected_status in (
        ("2026-01-14T00:01:00Z", "unavailable"),
        ("2026-01-13T23:59:00Z", "success"),
    ):
        local_only = module.InjuryResolver(
            tmp_path / f"cache-{expected_status}",
            fetch=forbidden,
            search=forbidden,
            local=lambda *_: {"captured_at": captured_at, "records": []},
            clock=lambda: datetime.fromisoformat("2026-01-14T00:30:00+00:00"),
        )
        local_result = local_only._resolve(
            query,
            tmp_path / f"cache-{expected_status}" / "query",
            datetime.fromisoformat("2026-01-14T00:30:00+00:00"),
            "fixture:game",
            cutoff,
            replay=True,
        )
        assert local_result["status"] == expected_status
        assert local_result["request_count"] == 0
