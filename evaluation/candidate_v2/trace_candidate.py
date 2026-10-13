"""Optional side-channel OTLP traces. Never modifies requests or evaluation."""
import sys,time
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]/'phoenix/.venv/Lib/site-packages'))
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
provider=TracerProvider(resource=Resource.create({'service.name':'courtvision-candidate-v2','openinference.project.name':'courtvision-candidate-v2'}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint='http://127.0.0.1:8682/v1/traces',timeout=5)))
tracer=provider.get_tracer('candidate-v2-only')

def emit(sample,stage,artifact):
    end=time.time_ns();start=end-int(artifact['latency_seconds']*1e9)
    span=tracer.start_span('candidate.'+stage,start_time=start)
    span.set_attribute('sample_id',sample);span.set_attribute('system_revision','golden-pregame-system-candidate-v2')
    span.set_attribute('provider_status',artifact['provider_status']);span.set_attribute('parse_status',artifact['parse_status']);span.set_attribute('schema_status',artifact['schema_status'])
    span.set_attribute('request_hash',artifact['request_hash']);span.set_attribute('response_hash',artifact.get('response_hash',''))
    span.set_attribute('latency_seconds',artifact['latency_seconds']);span.set_attribute('error',bool(artifact['error']))
    for key,value in (artifact.get('provider_metadata',{}).get('usage') or{}).items():
        if isinstance(value,(int,float)):span.set_attribute('llm.token_count.'+key,value)
    span.end(end_time=end);provider.force_flush()
