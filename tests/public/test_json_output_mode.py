import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from v2.agent.generate_historical_pregame_analysis import _request_deepseek_impl


class JsonOutputMode(unittest.TestCase):
    def request(self, content='{"value":1}', **options):
        seen = []
        def opener(request, timeout):
            seen.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'id': 'fixture-response', 'model': 'fixture-model',
                'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 3}}).encode())
        result = _request_deepseek_impl([{'role': 'user', 'content': 'Return JSON.'}],
            'fixture-key', 'fixture-model', 321, opener=opener, **options)
        return result, seen[0]

    def test_explicit_json_mode_changes_format_only(self):
        legacy, legacy_body = self.request()
        structured, body = self.request(response_format={'type': 'json_object'})
        self.assertEqual(body.pop('response_format'), {'type': 'json_object'})
        self.assertEqual(body, legacy_body)
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['max_tokens'], 321)
        self.assertEqual(structured.content, legacy.content)

    def test_legacy_requests_remain_opt_out(self):
        _, body = self.request()
        self.assertNotIn('response_format', body)

    def test_incomplete_provider_json_is_preserved_not_repaired(self):
        result, _ = self.request(content='{"value":1', response_format={'type': 'json_object'})
        self.assertEqual(result.content, '{"value":1')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.content)

    def test_unsupported_format_stops_before_network(self):
        with self.assertRaisesRegex(ValueError, 'unsupported_response_format'):
            self.request(response_format={'type': 'unsupported'})


if __name__ == '__main__':
    unittest.main()
