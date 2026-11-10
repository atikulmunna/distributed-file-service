"""Structured JSON logging shared across the app: request events and audit events."""

import json
import logging
import os
import socket
import uuid

from fastapi import Request
from opentelemetry import trace

# Identifies this process in logs, in the X-DFS-Instance response header and in /ready and /v1/stats.
# INSTANCE_NAME (a pod or service name, say) replaces the hostname, which is the container id under
# Docker; the random suffix tells apart processes on one host.
INSTANCE_ID = f"{os.getenv('INSTANCE_NAME') or socket.gethostname()}-{uuid.uuid4().hex[:6]}"

request_logger = logging.getLogger("dfs.request")
if not request_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(handler)
request_logger.setLevel(logging.INFO)
audit_logger = logging.getLogger("dfs.audit")
if not audit_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)
audit_logger.setLevel(logging.INFO)


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context or not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def log_event(payload: dict) -> None:
    payload.setdefault("trace_id", current_trace_id())
    payload.setdefault("instance_id", INSTANCE_ID)
    request_logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def audit_event(payload: dict) -> None:
    payload.setdefault("trace_id", current_trace_id())
    payload.setdefault("instance_id", INSTANCE_ID)
    audit_logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
