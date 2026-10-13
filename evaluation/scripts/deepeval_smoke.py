"""Deterministic adapter probe; not an article-quality metric or quality threshold."""
import json,os
from pathlib import Path
os.environ['DEEPEVAL_TELEMETRY_OPT_OUT']='YES'
from deepeval import evaluate
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from deepeval.evaluate.configs import AsyncConfig,DisplayConfig,CacheConfig
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1]

class ReadableFrozenOutput(BaseMetric):
    def __init__(self):
        self.threshold=1;self.async_mode=False;self.strict_mode=False
    def measure(self,test_case,*args,**kwargs):
        body=json.loads(test_case.actual_output)
        self.score=int(isinstance(body.get('article'),str) and bool(body.get('prediction')))
        self.reason='Local JSON/readability adapter probe only';self.success=bool(self.score);return self.score
    async def a_measure(self,test_case,*args,**kwargs):return self.measure(test_case)
    @property
    def __name__(self):return 'Environment adapter smoke only'

schema=json.loads((ROOT/'datasets/evaluation-sample.schema.json').read_text())
samples=[json.loads(line) for line in (ROOT/'datasets/smoke_samples.jsonl').read_text(encoding='utf-8').splitlines()]
for s in samples:Draft202012Validator(schema).validate(s)
cases=[LLMTestCase(input=json.dumps(s['input_context'],ensure_ascii=False),actual_output=json.dumps(s['actual_output'],ensure_ascii=False)) for s in samples]
result=evaluate(cases,[ReadableFrozenOutput()],async_config=AsyncConfig(run_async=False),
    display_config=DisplayConfig(show_indicator=False,print_results=False,inspect_after_run=False),cache_config=CacheConfig(write_cache=False,use_cache=False))
receipt={'sample_count':len(samples),'schema_valid':len(samples),'passed':sum(r.success for r in result.test_results),
    'failed':sum(not r.success for r in result.test_results),'llm_calls':0,'purpose':'adapter connectivity, not quality evaluation'}
(ROOT/'reports/deepeval-smoke.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt))
