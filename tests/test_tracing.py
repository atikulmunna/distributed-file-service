"""OpenTelemetry setup: with tracing on, requests produce spans and the app can read the trace id."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import tracing
from app.config import settings
from app.logging_utils import current_trace_id


def test_tracing_instruments_requests(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(settings, "tracing_enabled", True)
    monkeypatch.setattr(tracing, "_tracing_initialized", False)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **_: exporter)  # no collector needed
    monkeypatch.setattr(tracing, "BatchSpanProcessor", SimpleSpanProcessor)  # export synchronously
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _: None)  # leave the process-wide provider alone

    probe = FastAPI()

    @probe.get("/ping")
    def ping() -> dict:
        return {"trace_id": current_trace_id()}

    tracing.setup_tracing(probe)
    with TestClient(probe) as client:
        trace_id = client.get("/ping").json()["trace_id"]

    spans = exporter.get_finished_spans()
    assert any(span.name.startswith("GET /ping") for span in spans), [span.name for span in spans]
    assert trace_id is not None
    assert len(trace_id) == 32
    assert any(format(span.context.trace_id, "032x") == trace_id for span in spans)
