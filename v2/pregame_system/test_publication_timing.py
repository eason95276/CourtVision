from datetime import datetime, timezone
import pytest

from .publication_timing import next_evening, preview_evening


@pytest.mark.parametrize('at,expected',[
    ('2026-10-21T02:00:00Z','2026-10-21T12:00:00+00:00'),
    ('2026-10-21T04:00:00Z','2026-10-21T12:00:00+00:00'),
    ('2026-10-21T12:00:00Z','2026-10-21T12:00:00+00:00'),
    ('2026-10-21T14:30:00Z','2026-10-21T14:30:00+00:00'),
    ('2026-10-21T16:01:00Z','2026-10-22T12:00:00+00:00'),
])
def test_evening_release_uses_beijing_calendar(at,expected):
    assert next_evening(at).isoformat()==expected


def test_next_day_preview_and_today_postgame_share_evening_window():
    assert preview_evening('2026-10-21T23:00:00Z','2026-10-21T04:00:00Z')==next_evening('2026-10-21T04:00:00Z')


def test_special_preheat_can_keep_its_explicit_earlier_day():
    assert preview_evening('2026-10-24T23:00:00Z','2026-10-21T04:00:00Z',preheat=True).isoformat()=='2026-10-21T12:00:00+00:00'


def test_existing_policy_decoding_does_not_invent_evening_identity():
    from .scheduler_policy import normalized_policy
    policy={'lifecycle':'early-final-v1','early_requested_at':'2026-10-21T04:00:00Z'}
    assert 'early_release_hour_beijing' not in normalized_policy(policy)
    assert normalized_policy({**policy,'early_release_hour_beijing':20})['policy_identity']!=normalized_policy(policy)['policy_identity']


def test_planner_keeps_final_at_t_minus_twenty_and_holds_only_initial_preview():
    from .two_stage_lifecycle import plan
    from types import SimpleNamespace
    class DB:
        def execute(self,*args):return self
        def fetchone(self):return None
    tasks=SimpleNamespace(_insert=lambda db,gid,stage,trigger,due,deadline,at,**kw:
        {'stage':stage,'scheduled_for':due,'deadline_at':deadline})
    policy={'early_requested_at':'2026-10-21T04:00:00Z','policy_identity':'test-evening',
        'policy_version':'scheduler-v2','early_release_hour_beijing':20,'prefer_off_peak':True}
    rows=plan(SimpleNamespace(tasks=tasks),DB(),{'id':'espn:test','tipoff':'2026-10-21T23:00:00Z'},
        {'schedule_revision':1,'current_version_id':None},policy,datetime(2026,10,21,4,tzinfo=timezone.utc))
    assert {r['stage']:r['scheduled_for']for r in rows}=={
        'EARLY_PREVIEW':'2026-10-21T12:00:00+00:00','FINAL_PREGAME':'2026-10-21T22:40:00+00:00'}
