import unittest
from audit_historical_replay import FIELDS,CORE,status,tier,eligible_package,replay_binding

class ReplayEligibilityTests(unittest.TestCase):
    def fields(self):return {k:status('safe','fixture proof') for k in FIELDS}
    def test_all_safe_a(self):self.assertEqual(tier(self.fields()),'TIER_A_STRICT')
    def test_missing_auxiliary_b(self):
        f=self.fields();f['injury']=status('missing','unavailable');self.assertEqual(tier(f),'TIER_B_PARTIAL')
    def test_unknown_core_never_promoted(self):
        f=self.fields();f['recent_form']=status('unknown','shift(1) lacks source time');self.assertEqual(tier(f),'TIER_C_UNSAFE')
    def test_late_capture_core_c(self):
        f=self.fields();f['roster']=status('unsafe','captured after cutoff');self.assertEqual(tier(f),'TIER_C_UNSAFE')
    def test_optional_late_archive_not_available(self):
        f=self.fields();f['injury']=status('unsafe','late archive');self.assertEqual(tier(f),'TIER_B_PARTIAL')
    def test_package_excludes_unknown_and_target_truth(self):
        row={'game_id':'nba:fixture','prediction_cutoff':'2025-10-01T10:00:00Z','safe_fields':['game_id'],
            'missing_fields':['injury'],'unknown_fields':['market_spread'],'unsafe_fields':['player_role_starter'],
            'field_audit':{'game_id':status('safe','fixture')}}
        p=eligible_package(row,{'game_id':'nba:fixture','market_spread':-4.5,'actual_total':240})
        self.assertEqual(p['available'],{'game_id':'nba:fixture'});self.assertNotIn('actual_total',p['available'])
    def test_target_outcome_cannot_be_safe_input(self):
        row={'game_id':'fixture','prediction_cutoff':None,'safe_fields':['actual_total'],'missing_fields':[],
            'unknown_fields':[],'unsafe_fields':[],'field_audit':{}}
        with self.assertRaises(ValueError):eligible_package(row,{'actual_total':240})
    def test_same_attempt_binding_not_directory_proximity(self):
        self.assertTrue(replay_binding({'request_id':'a'},{'request_id':'a'},{'request_id':'a'}))
        self.assertFalse(replay_binding({'request_id':'a'},{'request_id':'b'},{'request_id':'a'}))
        self.assertFalse(replay_binding({}, {}, {}))

if __name__=='__main__':unittest.main()
