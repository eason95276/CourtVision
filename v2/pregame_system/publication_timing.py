"""Beijing evening release window shared by previews and Postgame prose."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .cost_ledger import instant

CHINA = ZoneInfo('Asia/Shanghai')
EVENING_HOUR = 20


def evening_start(day, hour=EVENING_HOUR):
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError('evening_hour_invalid')
    return instant(datetime.combine(day, time(hour=hour), CHINA))


def next_evening(at, hour=EVENING_HOUR):
    at = instant(at)
    return max(at, evening_start(at.astimezone(CHINA).date(), hour))


def preview_evening(tipoff, requested_at, *, hour=EVENING_HOUR, preheat=False):
    day = (instant(requested_at).astimezone(CHINA).date() if preheat else
           instant(tipoff).astimezone(CHINA).date() - timedelta(days=1))
    return evening_start(day, hour)
