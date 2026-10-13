"""One local OTLP trace, not a production instrumentation change."""
import json,sqlite3,uuid,time
from pathlib import Path
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
ROOT=Path(__file__).resolve().parents[1]
run=str(uuid.uuid4());provider=TracerProvider(resource=Resource.create({'service.name':'courtvision-isolated-smoke','openinference.project.name':'courtvision-evaluation-smoke'}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint='http://127.0.0.1:8682/v1/traces',timeout=20)))
tracer=provider.get_tracer('evaluation-only')
with tracer.start_as_current_span('evaluation.smoke') as span:
    span.set_attribute('evaluation.run_id',run);span.set_attribute('evaluation.isolated',True)
    span.set_attribute('evaluation.model_calls',0)
    trace_id=format(span.get_span_context().trace_id,'032x')
provider.force_flush();provider.shutdown()
# Independently prove ingestion from Phoenix's isolated SQLite, not exporter success alone.
db=ROOT/'phoenix/runtime/phoenix.db'
found=False
for attempt in range(15):
    if db.is_file():
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
            found=bool(c.execute('SELECT COUNT(*) FROM spans s JOIN traces t ON s.trace_rowid=t.id WHERE s.name=? AND t.trace_id=?',('evaluation.smoke',trace_id)).fetchone()[0])
    if found:break
    time.sleep(1)
assert found,'Phoenix has not persisted the isolated span'
receipt={'trace_id':trace_id,'run_id':run,'persisted':found,'local_endpoint':'http://127.0.0.1:8682/v1/traces','llm_calls':0}
(ROOT/'reports/phoenix-smoke.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt))
