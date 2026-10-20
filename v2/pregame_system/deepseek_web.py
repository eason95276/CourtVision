"""Bounded server-side web acquisition using DeepSeek's documented tools API."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .storage import now, save, content_hash
from .web_search import _public_discovery_url


class DeepSeekWebTransport:
    provider='deepseek-web'
    def __init__(self, *, root=None, timeout=15, opener=None):
        self.root = Path(root) if root else None
        self.timeout = min(30, max(1, timeout))
        self.opener = opener or urllib.request.urlopen

    def _request(self, operation, body):
        key = os.environ.get('DEEPSEEK_API_KEY', '')
        if not key:
            raise ValueError('deepseek_web_api_key_missing')
        started = time.perf_counter()
        request = urllib.request.Request(
            'https://api.deepseek.com/v1/tools/' + operation,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            method='POST')
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(4_000_001)
        except Exception as exc:
            from .cost_ledger import record_call
            record_call(provider='DeepSeek '+operation.title(),model=None,success=False,
                error_type=type(exc).__name__,detail={'billable_requests':0,'operation':operation})
            if self.root:
                import uuid
                save(self.root/(operation+'-error-'+uuid.uuid4().hex+'.json'),
                    {'request':body,'failed_at':now(),'error_type':type(exc).__name__})
            raise
        if len(raw) > 4_000_000:
            raise ValueError('deepseek_web_response_too_large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('deepseek_web_response_invalid')
        receipt = {'provider': 'deepseek-' + operation, 'captured_at': now(),
                   'latency_seconds': round(time.perf_counter() - started, 3),
                   'response_sha256': content_hash(data)}
        if self.root:
            import uuid
            save(self.root / (operation + '-' + uuid.uuid4().hex + '.json'),
                 {'request': body, 'receipt': receipt, 'response': data})
        from .cost_ledger import record_call
        billable = bool(data.get('results')) if operation == 'search' else bool(data.get('content'))
        record_call(provider='DeepSeek ' + operation.title(), model=None, success=True,
                    detail={'billable_requests': int(billable), 'operation': operation})
        return data, receipt

    def search(self, payload):
        query = ' '.join(str(payload['query']).split())
        if not 1 <= len(query) <= 128:
            raise ValueError('deepseek_search_query_length')
        data, receipt = self._request('search', {'query': query,
            'count': min(8, max(1, int(payload.get('max_results', 6)))), 'need_content': False})
        return {'results': [{'url': r.get('url'), 'title': r.get('title'),
            'content': r.get('snippet') or r.get('summary') or '',
            'published_date': r.get('date'), 'score': r.get('rerank_score'),
            'provider_temporal_metadata': {'published_at': r.get('date')}}
            for r in data.get('results', []) if isinstance(r, dict)], **receipt}

    def fetch_many(self, urls, *, budget_seconds=60):
        deadline = time.perf_counter() + min(90, max(1, budget_seconds))
        def one(url):
            if not _public_discovery_url(url):
                return {'url': url, 'content': '', 'error': 'unsafe_source_url'}
            if time.perf_counter() >= deadline:
                return {'url': url, 'content': '', 'error': 'fetch_budget_exhausted'}
            try:
                data, receipt = self._request('fetch', {'url': url})
                return {'url': url, 'title': data.get('title'), 'content': data.get('content') or '',
                        'method': 'deepseek-server-fetch', **receipt}
            except Exception as exc:
                return {'url': url, 'content': '', 'provider': 'deepseek-fetch',
                        'error': type(exc).__name__}
        with ThreadPoolExecutor(max_workers=3) as pool:
            from contextvars import copy_context
            jobs=[pool.submit(copy_context().run,one,url)for url in list(dict.fromkeys(urls))[:8]]
            return [job.result()for job in jobs]
