import hashlib
import json
from datetime import datetime, timedelta, timezone

from .schedule_catalog import ScheduleCatalog
from .schedule_sync import ScheduleSyncRuntime


class Clock:
    def __init__(self): self.value=datetime(2026,10,5,8,tzinfo=timezone.utc)
    def __call__(self): return self.value


def test_schedule_refresh_invokes_postgame_sync_once_and_never_ai_generation(tmp_path):
    clock=Clock()
    catalog=ScheduleCatalog(tmp_path/"schedule.sqlite",clock=clock)
    postgame_calls=[]
    def fetcher(url):
        raw=json.dumps({"events":[]},sort_keys=True).encode()
        return {"url":url,"captured_at":clock().isoformat(),"status":"success","http_status":200,
            "raw_sha256":hashlib.sha256(raw).hexdigest(),"payload":{"events":[]}}
    def sync_postgame(schedule_catalog):
        postgame_calls.append(schedule_catalog)
        return [{"game_id":"espn:401999901","status":"published"}]

    runtime=ScheduleSyncRuntime(catalog,fetcher=fetcher,clock=clock,postgame_sync=sync_postgame)
    result=runtime.cycle()
    assert result[-1]["postgame_sync"][0]["status"]=="published"
    assert postgame_calls==[catalog]
    clock.value+=timedelta(seconds=30)
    assert runtime.cycle()==[]
    assert len(postgame_calls)==1
    clock.value+=timedelta(minutes=5)
    retry=runtime.cycle()
    assert retry[-1]["postgame_sync"][0]["status"]=="published"
    assert len(postgame_calls)==2
    assert catalog.count_generation_tasks()==0
