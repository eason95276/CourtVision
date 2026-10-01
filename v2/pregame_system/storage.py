from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import uuid

BASE = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timezone required')
    return d.astimezone(timezone.utc)


def local_wall_time_to_utc(value: datetime, zone_name: str) -> datetime:
    """Convert an unzoned wall time, rejecting DST gaps and repeated hours."""
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError('naive_local_datetime_required')
    zone = ZoneInfo(zone_name)
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    first_utc = first.astimezone(timezone.utc)
    second_utc = second.astimezone(timezone.utc)
    first_round_trip = first_utc.astimezone(zone).replace(tzinfo=None)
    second_round_trip = second_utc.astimezone(zone).replace(tzinfo=None)
    if first_round_trip != value and second_round_trip != value:
        raise ValueError('nonexistent_local_datetime')
    if first.utcoffset() != second.utcoffset():
        raise ValueError('ambiguous_local_datetime_requires_offset')
    return first_utc


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def clean(value):
    if isinstance(value, dict):
        return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value,'item'):
        return clean(value.item())
    if hasattr(value,'isoformat'):
        return value.isoformat()
    return value


def content_hash(value):
    return hashlib.sha256(json.dumps(clean(value),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        json.dump(clean(value),f,ensure_ascii=False,indent=2,allow_nan=False)
    return path


def new_run(folder):
    p=BASE/'outputs'/folder/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:10])
    p.mkdir(parents=True,exist_ok=False)
    return p
