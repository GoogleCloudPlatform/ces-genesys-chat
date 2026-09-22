# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextvars
import json
import logging
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# Standard LogRecord attrs. Anything not listed here is treated as a caller-supplied
# field and promoted to the top level of the payload.
#
# NOTE: 'taskName' was added to LogRecord in Python 3.12. It must be listed here
# or every single log line emitted from within a coroutine carries a useless
# "taskName": "Task-N" field.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Request headers that are safe to emit verbatim. Everything else is redacted.
#
# This is an ALLOWLIST on purpose. A denylist ('api-key', 'authorization', ...)
# fails open: the day Genesys introduces a new credential header, a denylist
# leaks it. Genesys authenticates with a static 'api-key' header, so any header
# not explicitly named here is assumed to be sensitive.
_SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "content-length",
        "content-type",
        "forwarded",
        "host",
        "inin-correlation-id",
        "traceparent",
        "user-agent",
        "x-cloud-trace-context",
        "x-forwarded-proto",
    }
)

_REDACTED = "<REDACTED>"

# Per-request fields merged into every log line; a ContextVar so nested helpers
# get correlation without threading params.
_request_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "request_context", default=None
)

_project_id: str | None = None


def set_project_id(project_id: str | None) -> None:
    """Records the GCP project, needed to build Cloud Logging trace resource names."""
    global _project_id
    _project_id = project_id


def bind_request_context(
    *,
    trace_header: str | None = None,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """Binds fields onto the request context. trace_header is the raw
    X-Cloud-Trace-Context ('TRACE/SPAN;o=1'); it is rewritten to the resource name
    Cloud Logging uses to group a request's lines."""
    ctx: dict[str, Any] = dict(_request_context.get() or {})
    ctx.update({k: v for k, v in fields.items() if v is not None})

    if correlation_id:
        ctx["genesysCorrelationId"] = correlation_id

    if trace_header and _project_id:
        trace_id = trace_header.split("/", 1)[0].strip()
        if trace_id:
            ctx["logging.googleapis.com/trace"] = f"projects/{_project_id}/traces/{trace_id}"

    _request_context.set(ctx)


def clear_request_context() -> None:
    """Drops any fields bound by `bind_request_context`."""
    _request_context.set({})


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """
    Returns a copy of `headers` with every non-allowlisted value replaced by
    `<REDACTED>`.

    Genesys sends the shared secret as a plain `api-key` request header, so
    dumping the raw header map writes a live credential into Cloud Logging.
    """
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        redacted[lowered] = value if lowered in _SAFE_HEADERS else _REDACTED
    return redacted


class JSONFormatter(logging.Formatter):
    """Formats log records as JSON strings for Cloud Logging."""

    def format(self, record):
        log_entry = {
            "message": record.getMessage(),
            "severity": record.levelname,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }

        # Cloud Logging's stack_trace field drives Error Reporting grouping, so
        # populate it explicitly rather than via super().format().
        if record.exc_info:
            log_entry["stack_trace"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            log_entry["stack_trace"] = record.exc_text

        if record.stack_info:
            log_entry["stack_info"] = self.formatStack(record.stack_info)

        # Fields bound for the lifetime of the request (trace, correlation id).
        for key, value in (_request_context.get() or {}).items():
            log_entry[key] = value

        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS and not key.startswith("_"):
                log_entry[key] = value

        # `default=str` keeps the logger from ever raising on a non-serializable
        # value. A logging call must never be the thing that fails a request.
        return json.dumps(log_entry, default=str)


def setup_logger(name: str, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = JSONFormatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def configure_stdlib_loggers(level=logging.INFO) -> None:
    """
    Routes uvicorn's own loggers through `JSONFormatter`.

    Without this, uvicorn emits plain text to stdout and Cloud Logging ingests
    each *line* of a traceback as a separate, severity-less entry -- a single
    unhandled exception fans out into ~40 disconnected log entries that have to
    be manually reassembled during an incident.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter("%(message)s"))

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = False
