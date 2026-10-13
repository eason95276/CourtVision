import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from asset_scanner import gid,canonical_hash
from build_eval_baseline import market_for,verify_research

class EvaluationPreparationTests(unittest.TestCase):
    def test_canonical_order_and_non_game_id(self):
        self.assertEqual(canonical_hash({'a':1,'b':2}),canonical_hash({'b':2,'a':1}))
        self.assertEqual(gid('0022500001'),'nba:0022500001')
        self.assertEqual(gid('bogus'),'unresolved:bogus')
    def payload(self):
        return {'game_id':'espn:123','input_cutoff_at':'2026-10-09T10:00:00Z',
            'game_identity':{'scheduled_tipoff':'2026-10-09T11:00:00Z','home_team_id':1,'away_team_id':2},
            'public_market_reference':{'source_url':'https://example.org/quote','captured_at':'2026-10-09T09:59:00Z',
                'game_identity':{'game_id':'espn:123','home_team_id':1,'away_team_id':2},'home_spread':-4.5,'expected_home_margin':4.5,'total':220.5}}
    def test_market_identity(self):
        p=self.payload();self.assertIsNotNone(market_for(p));p['public_market_reference']['game_identity']['game_id']='espn:other';self.assertIsNone(market_for(p))
    def test_market_cutoff(self):
        p=self.payload();p['public_market_reference']['captured_at']='2026-10-09T10:01:00Z';self.assertIsNone(market_for(p))
    def test_market_sign(self):
        p=self.payload();p['public_market_reference']['expected_home_margin']=-4.5;self.assertIsNone(market_for(p))
    def test_research_real_saved_request_contract(self):
        from asset_scanner import sha
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);messages=[{'role':'system','content':'protocol'},{'role':'user','content':json.dumps({'game':{'game_id':'espn:123'},'cutoff':'2026-10-09T09:00:00Z','local_context':{}})}]
            attempt=folder/'attempt.json';attempt.write_text(json.dumps({'input_hash':canonical_hash(messages)}))
            request=folder/'search_model_request.json';request.write_text(json.dumps({'messages':messages}))
            p=self.payload();p['research']={'records':[{'id':'x'}]};p['provenance']={'research_artifact':{'files':[{'path':str(attempt),'sha256':sha(attempt)}]}}
            self.assertTrue(verify_research(p)[1])
            request.write_text(json.dumps({'messages':[{'role':'user','content':'tampered'}]}));self.assertFalse(verify_research(p)[1])
    def test_smoke_samples_have_no_labels(self):
        root=Path(__file__).resolve().parents[1]
        samples=[json.loads(s) for s in (root/'datasets/smoke_samples.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(samples),3)
        for sample in samples:self.assertIsNone(sample['metadata']['quality_label'])

if __name__=='__main__':unittest.main()
