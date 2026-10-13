"""Evaluation workload only; no raw text/key/user-data in OTLP attributes."""
import sys,time
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]/'phoenix/.venv/Lib/site-packages'))
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
provider=TracerProvider(resource=Resource.create({'service.name':'courtvision-evaluation-quality','openinference.project.name':'courtvision-evaluation-quality'}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint='http://127.0.0.1:8682/v1/traces',timeout=5)))
tracer=provider.get_tracer('versioned-evaluation')

def emit(revision,sample,stage,artifact):
    end=time.time_ns();duration=artifact['latency_seconds'];span=tracer.start_span('evaluation.'+stage,start_time=end-int(duration*1e9))
    for k,v in {'sample_id':sample,'revision':revision,'stage':stage,'model':artifact.get('config',{}).get('model','deepseek-v4-flash'),
        'latency_seconds':duration,'provider_status':artifact.get('provider_status','NOT_RUN'),'parse_status':artifact.get('parse_status','NOT_RUN'),
        'raw_schema_status':artifact.get('raw_schema_status','NOT_RUN'),'chain_status':artifact.get('chain_status','NOT_YET_VALIDATED'),
        'request_hash':artifact['request_hash'],'failure_type':artifact.get('failure_type') or (artifact.get('error') or{}).get('type',''),
        'component_success':artifact.get('error') is None}.items():span.set_attribute(k,v)
    for k,v in (artifact.get('provider_metadata',{}).get('usage') or{}).items():
        if isinstance(v,int):span.set_attribute('llm.token_count.'+k,v)
    tid=format(span.get_span_context().trace_id,'032x');sid=format(span.get_span_context().span_id,'016x');span.end(end_time=end);provider.force_flush()
    return {'trace_id':tid,'span_id':sid,'revision':revision,'stage':stage,'sample_id':sample}
