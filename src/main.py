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

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import google.auth
import google.auth.transport.requests
import httpx
import uvicorn
from cachetools import TTLCache
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.cloud import firestore
from pydantic import BaseModel, Field

from src import config, logging_utils

logger = logging_utils.setup_logger(__name__)
logging_utils.configure_stdlib_loggers()

# A single connection pool is shared by every turn. Building an
# httpx.AsyncClient per request throws away the TCP + TLS handshake to
# ces.googleapis.com each time, which adds several round trips to the critical
# path of a webhook that Genesys abandons if it runs long.
_HTTP_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)

# Sized against the Genesys webhook deadline, not against CES's own 60s server
# deadline. See config.CES_TIMEOUT_SECONDS for the full reasoning.
_CES_TIMEOUT = httpx.Timeout(
    config.CES_TIMEOUT_SECONDS,
    connect=config.CES_CONNECT_TIMEOUT_SECONDS,
)

# A retry that cannot plausibly finish is worse than no retry: it burns what is
# left of the budget and then fails anyway, turning a fast error into a Genesys
# timeout. Below this many seconds remaining, skip the retry and fail cleanly.
_MIN_RETRY_BUDGET_SECONDS = 1.0

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Process-wide CES client. Lazily recreated so tests that call handlers directly,
    without ASGI startup, still work."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(limits=_HTTP_LIMITS, timeout=_CES_TIMEOUT)
    return _http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Owns the shared CES client for the lifetime of the ASGI application."""
    global _http_client
    _http_client = httpx.AsyncClient(limits=_HTTP_LIMITS, timeout=_CES_TIMEOUT)
    app.state.ces_client = _http_client
    logger.info("Opened shared CES HTTP connection pool.")
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None
        logger.info("Closed shared CES HTTP connection pool.")


app = FastAPI(title="Genesys Chat Adapter for CXAS", lifespan=lifespan)

# SessionConfig fields this adapter sends opportunistically. If CES rejects the
# request because it does not recognise one of them, it is dropped and the call
# is retried once. See _strip_unsupported_config_fields.
_OPTIONAL_CES_CONFIG_FIELDS = ("excludeDiagnosticInfo", "sessionTtl")
_UNKNOWN_FIELD_PATTERN = re.compile(r'Unknown name "([A-Za-z_][A-Za-z0-9_]*)"')

# A single Cloud Logging entry is capped at 256 KB. Stay well clear of it.
_MAX_LOGGED_FIELD_BYTES = 100_000


def _safe_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Strips caller-supplied values out of Pydantic validation errors.

    `exc.errors()` embeds the offending `input` verbatim, which for this webhook
    is the customer's utterance or button payload. Only the machine-readable
    shape of the failure is retained.
    """
    return [
        {"type": err.get("type"), "loc": err.get("loc"), "msg": err.get("msg")}
        for err in errors
    ]


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    safe_errors = _safe_validation_errors(exc.errors())

    log_fields: dict[str, Any] = {
        "validation_errors": safe_errors,
        "body_bytes": len(body),
        "adapter_error": True,
    }
    if config.DEBUG_MODE:
        log_fields["body"] = body.decode("utf-8", errors="replace")

    logger.error("Validation Error", extra=log_fields)
    # The response is echoed into Genesys-side logs, so it carries only the
    # redacted error shape -- never the request body.
    return JSONResponse(status_code=422, content={"detail": safe_errors})


credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
auth_req = google.auth.transport.requests.Request()
logging_utils.set_project_id(project)

# Serialises token refreshes. See _get_access_token.
_token_lock = asyncio.Lock()


async def _get_access_token() -> str:
    """Returns a valid access token. credentials.refresh blocks the event loop,
    so it runs in a thread; the lock stops every concurrent turn from refreshing
    at once when the token expires for all of them simultaneously."""
    if credentials.valid:
        return credentials.token
    async with _token_lock:
        if not credentials.valid:
            await asyncio.to_thread(credentials.refresh, auth_req)
    return credentials.token


# Per-instance fallback session store (deployment_id + turn_count) used when Firestore is unset or unreachable.
session_cache = TTLCache(maxsize=10000, ttl=3600)

FIRESTORE_SESSIONS_COLLECTION = config.FIRESTORE_SESSIONS_COLLECTION

if FIRESTORE_SESSIONS_COLLECTION:
    firestore_db = firestore.AsyncClient(project=project, credentials=credentials)
    logger.info(f"Using Firestore collection '{FIRESTORE_SESSIONS_COLLECTION}' for session tracking.")
else:
    logger.warning("Session-to-deployment-id mapping is happening in-memory. This is only recommended for testing and should not be used in production.")


class ButtonResponse(BaseModel):
    type: str
    text: str
    payload: str

class ContentItem(BaseModel):
    contentType: str
    content: str | None = None
    buttonResponse: ButtonResponse | None = None

class InputMessage(BaseModel):
    type: str
    text: str | None = None
    content: list[ContentItem] | None = Field(default_factory=list)

class ChatRequest(BaseModel):
    botId: str
    botVersion: str
    inputMessage: InputMessage
    languageCode: str
    botSessionId: str
    botSessionTimeout: int
    genesysConversationId: str
    chatBot: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = Field(default_factory=dict)


@dataclass
class SessionState:
    """Per-conversation state resolved at the start of each turn."""

    deployment_id: str | None
    turn_count: int
    is_new_session: bool
    # Where turn_count came from: "firestore", "memory", or "memory_degraded"
    # when Firestore was configured but unreachable.
    source: str


def _verify_api_key(supplied: str | None) -> bool:
    """
    Compares the supplied key against every configured key in constant time.

    `hmac.compare_digest` avoids leaking a prefix of the expected key through
    response-timing differences. Multiple configured keys allow a rotation to be
    staged without a window in which inbound Genesys requests are rejected.
    """
    expected_keys = config.API_KEYS
    if not expected_keys:
        return False

    candidate = supplied or ""
    # Every key is compared even after a match so that the number of comparisons
    # performed does not depend on which key matched.
    matched = False
    for expected in expected_keys:
        if hmac.compare_digest(candidate, expected):
            matched = True
    return matched


def _failure_response(message: str) -> dict[str, Any]:
    """
    Builds a well-formed Bot Connector response that ends the bot turn and hands
    the conversation to a human.

    Genesys treats any non-200 from this webhook as a hard failure and routes the
    Architect flow down its Failure branch, which terminates the customer's
    conversation. Returning a valid 200 payload with `botState: COMPLETE` and the
    escalation intent instead lets the flow execute its normal handoff path.
    """
    return {
        "replymessages": [{"type": "Text", "text": message}],
        "intent": "live-agent-handoff",
        "confidence": 1.0,
        "botState": "COMPLETE",
        "parameters": {"escalationReason": "adapter_error"},
    }


async def _load_or_create_session(
    conversation_id: str,
    deployment_id_param: str | None,
    log_extra: dict[str, Any],
) -> SessionState:
    """Resolves the session record for this conversation, creating it on first turn.

    New-session detection keys off the stored record, never the _deployment_id
    param: Architect re-enters the Call Bot Connector action and resends it
    mid-conversation, which would reset the turn counter and replay session_start.
    The param still refreshes routing."""
    if FIRESTORE_SESSIONS_COLLECTION:
        try:
            doc_ref = firestore_db.collection(FIRESTORE_SESSIONS_COLLECTION).document(conversation_id)
            snapshot = await doc_ref.get()

            if snapshot.exists:
                data = snapshot.to_dict() or {}
                stored_deployment_id = data.get("deployment_id")
                deployment_id = deployment_id_param or stored_deployment_id
                turn_count = int(data.get("turn_count", 0) or 0) + 1

                # Increment server-side so concurrent turns cannot lose an update.
                updates: dict[str, Any] = {"turn_count": firestore.Increment(1)}
                if deployment_id_param and deployment_id_param != stored_deployment_id:
                    updates["deployment_id"] = deployment_id_param
                    logger.info(
                        "Refreshed stored deployment_id for existing session.",
                        extra=log_extra,
                    )
                await doc_ref.update(updates)

                logger.info(
                    f"Retrieved deployment_id {deployment_id} from Firestore. Turn: {turn_count}",
                    extra=log_extra,
                )
                return SessionState(deployment_id, turn_count, False, "firestore")

            if not deployment_id_param:
                return SessionState(None, 1, False, "firestore")

            expiry_time = datetime.now(UTC) + timedelta(hours=24)
            await doc_ref.set(
                {
                    "deployment_id": deployment_id_param,
                    "expiry_time": expiry_time,
                    "turn_count": 1,
                }
            )
            logger.info(
                f"New session started and deployment_id {deployment_id_param} stored in Firestore.",
                extra=log_extra,
            )
            return SessionState(deployment_id_param, 1, True, "firestore")

        except Exception:
            # A Firestore outage or IAM misconfiguration must not end the
            # conversation. Fall through to the in-process cache: a turn counter
            # that is only accurate per-instance is far cheaper than a dropped chat.
            logger.error(
                "Firestore session lookup failed; falling back to in-memory session cache.",
                extra={**log_extra, "adapter_error": True},
                exc_info=True,
            )
            return _load_or_create_session_in_memory(
                conversation_id, deployment_id_param, log_extra, source="memory_degraded"
            )

    return _load_or_create_session_in_memory(
        conversation_id, deployment_id_param, log_extra, source="memory"
    )


def _load_or_create_session_in_memory(
    conversation_id: str,
    deployment_id_param: str | None,
    log_extra: dict[str, Any],
    source: str,
) -> SessionState:
    """In-process equivalent of _load_or_create_session. Not shared across instances."""
    session_data = session_cache.get(conversation_id)

    if session_data:
        stored_deployment_id = session_data.get("deployment_id")
        deployment_id = deployment_id_param or stored_deployment_id
        turn_count = int(session_data.get("turn_count", 0) or 0) + 1
        session_data["turn_count"] = turn_count
        if deployment_id_param:
            session_data["deployment_id"] = deployment_id_param
        logger.info(
            f"Retrieved deployment_id {deployment_id} from memory. Turn: {turn_count}",
            extra=log_extra,
        )
        return SessionState(deployment_id, turn_count, False, source)

    if not deployment_id_param:
        return SessionState(None, 1, False, source)

    session_cache[conversation_id] = {
        "deployment_id": deployment_id_param,
        "turn_count": 1,
    }
    logger.info(
        f"New session started and deployment_id {deployment_id_param} stored in memory.",
        extra=log_extra,
    )
    return SessionState(deployment_id_param, 1, True, source)


async def _delete_session(conversation_id: str, log_extra: dict[str, Any]) -> None:
    """Removes the session record once CES reports the conversation has ended."""
    session_cache.pop(conversation_id, None)
    if not FIRESTORE_SESSIONS_COLLECTION:
        logger.info("Cleaned up session from memory.", extra=log_extra)
        return
    try:
        await firestore_db.collection(FIRESTORE_SESSIONS_COLLECTION).document(conversation_id).delete()
        logger.info("Cleaned up session from Firestore.", extra=log_extra)
    except Exception:
        # The document carries a TTL, so a failed delete is self-healing.
        logger.error(
            "Failed to clean up session from Firestore; relying on TTL expiry.",
            extra={**log_extra, "adapter_error": True},
            exc_info=True,
        )


def _strip_unsupported_config_fields(
    payload: dict[str, Any], error_body: str, log_extra: dict[str, Any]
) -> list[str]:
    """
    Removes optional SessionConfig fields that CES reported as unknown.

    `sessionTtl` is INTERNAL-visibility in the CES protos and `excludeDiagnosticInfo`
    may not have reached the published v1 surface yet. REST transcoding rejects
    unknown fields outright, so rather than hard-failing the turn the field is
    dropped and the call retried once.
    """
    unknown_names = set(_UNKNOWN_FIELD_PATTERN.findall(error_body or ""))
    if not unknown_names:
        return []

    session_config = payload.get("config", {})
    dropped = [
        field
        for field in _OPTIONAL_CES_CONFIG_FIELDS
        if field in unknown_names and session_config.pop(field, None) is not None
    ]
    if dropped:
        logger.warning(
            f"CES rejected unsupported SessionConfig field(s) {dropped}; retrying without them.",
            extra={**log_extra, "unsupported_ces_fields": dropped},
        )
    return dropped


def to_genesys_param(value: Any) -> str:
    """Coerces a CES parameter value into the flat string Genesys Architect expects.

    Bot Connector params are string-only. Plain str() breaks three cases:
    bools (Architect ToBool() wants lowercase JSON spelling), None ("None"
    leaks into the flow), and dict/list (Python repr is not parseable JSON)."""
    # `bool` must be tested before `int`: bool is a subclass of int, so the
    # numeric branch would otherwise swallow it and yield "True"/"False".
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    # dict / list / anything else -> valid JSON the flow can round-trip.
    return json.dumps(value, default=str)


def _redact_output_for_log(output: dict[str, Any]) -> dict[str, Any]:
    """Prepares a single SessionOutput for structured logging."""
    safe: dict[str, Any] = {}
    for key, value in output.items():
        if key == "diagnosticInfo":
            # Logged separately so it can never crowd out the useful fields.
            continue
        if key == "audio" and isinstance(value, str):
            safe[key] = f"<base64 audio: {len(value)} chars>"
        else:
            safe[key] = value
    return safe


def _log_ces_response(data: dict[str, Any], log_extra: dict[str, Any]) -> None:
    """Emits each SessionOutput under `extra` so it lands in jsonPayload and stays queryable
    (jsonPayload.ces_output.turnIndex), instead of one oversized escaped blob."""
    if not config.DEBUG_MODE:
        return

    outputs = data.get("outputs", [])
    logger.info(f"CES response contained {len(outputs)} output(s).", extra=log_extra)

    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            logger.info("CES output (non-object).", extra={**log_extra, "ces_output": output})
            continue

        logger.info(
            f"CES output {index + 1}/{len(outputs)}",
            extra={
                **log_extra,
                "ces_output": _redact_output_for_log(output),
                "ces_output_index": index,
                "ces_turn_index": output.get("turnIndex"),
            },
        )

        diagnostic_info = output.get("diagnosticInfo")
        if not diagnostic_info:
            continue

        encoded = json.dumps(diagnostic_info, default=str)
        if len(encoded) > _MAX_LOGGED_FIELD_BYTES:
            # Drop the span tree, which is the bulk of the payload, and keep the
            # conversation messages that are actually useful when debugging.
            trimmed = {
                "messages": diagnostic_info.get("messages"),
                "rootSpan": {
                    k: v
                    for k, v in (diagnostic_info.get("rootSpan") or {}).items()
                    if k != "childSpans"
                },
                "_truncated": True,
                "_original_bytes": len(encoded),
            }
            logger.info(
                "CES diagnosticInfo (trimmed)",
                extra={**log_extra, "ces_diagnostic_info": trimmed},
            )
        else:
            logger.info(
                "CES diagnosticInfo",
                extra={**log_extra, "ces_diagnostic_info": diagnostic_info},
            )


async def _call_ces(
    ces_url: str,
    ces_payload: dict[str, Any],
    headers: dict[str, str],
    log_extra: dict[str, Any],
) -> dict[str, Any]:
    """
    Posts the turn to CES, retrying once without optional config fields on a 400.

    The configured timeout is a budget for the *whole* call, not per attempt:
    two sequential attempts each allowed the full timeout would double the worst
    case and defeat the point of sizing it against the Genesys deadline.
    """
    client = _get_http_client()
    deadline = time.monotonic() + config.CES_TIMEOUT_SECONDS

    ces_response = await client.post(ces_url, json=ces_payload, headers=headers)

    if ces_response.status_code == 400:
        if _strip_unsupported_config_fields(ces_payload, ces_response.text, log_extra):
            remaining = deadline - time.monotonic()
            if remaining < _MIN_RETRY_BUDGET_SECONDS:
                logger.warning(
                    "Skipping CES retry: only %.2fs of the timeout budget remains.",
                    remaining,
                    extra={**log_extra, "ces_retry_skipped": True},
                )
            else:
                ces_response = await client.post(
                    ces_url,
                    json=ces_payload,
                    headers=headers,
                    timeout=httpx.Timeout(
                        remaining, connect=min(config.CES_CONNECT_TIMEOUT_SECONDS, remaining)
                    ),
                )

    if config.DEBUG_MODE:
        logger.info(
            f"Received CES response status: {ces_response.status_code}", extra=log_extra
        )

    ces_response.raise_for_status()
    return ces_response.json()



@app.post("/v1/postUtterance")
async def handle_chat(
    raw_request: Request,
    request: ChatRequest,
    api_key: str | None = Header(None, alias="api-key"),
    x_api_key: str | None = Header(None, alias="x-api-key"),
):
    """
    Entry point for Genesys Bot Connector utterances.

    Establishes per-turn log correlation and converts any unhandled fault into a
    graceful handoff, so a bug here surfaces to the caller as an escalation
    rather than a 500. The turn itself is handled by _handle_chat_inner.

    Genesys sends the shared secret as `api-key`; `x-api-key` is accepted too so
    that an integration configured against either convention authenticates.
    """
    adapter_session_id = str(uuid.uuid4())
    log_extra = {
        "adapter_session_id": adapter_session_id,
        "genesysConversationId": request.genesysConversationId,
        "botSessionId": request.botSessionId
    }

    # Correlate every line for this turn with the Cloud Run request and with the
    # Genesys-side conversation, so an incident can be traced across both systems.
    logging_utils.bind_request_context(
        trace_header=raw_request.headers.get("x-cloud-trace-context"),
        correlation_id=raw_request.headers.get("inin-correlation-id"),
        adapter_session_id=adapter_session_id,
        genesysConversationId=request.genesysConversationId,
    )

    try:
        return await _handle_chat_inner(raw_request, request, api_key or x_api_key, log_extra)
    except HTTPException:
        # Authentication and configuration failures are deliberately surfaced to
        # Genesys as hard errors -- they indicate a setup problem that should be
        # visible during integration testing rather than silently degraded.
        raise
    except Exception:
        logger.error(
            "Unhandled error while processing utterance; returning graceful handoff.",
            extra={**log_extra, "adapter_error": True},
            exc_info=True,
        )
        return _failure_response(
            "Sorry, I'm having trouble right now. Let me connect you with someone who can help."
        )
    finally:
        logging_utils.clear_request_context()


async def _handle_chat_inner(
    raw_request: Request,
    request: ChatRequest,
    api_key: str | None,
    log_extra: dict[str, Any],
):
    """Core turn handling. Wrapped by handle_chat, which converts faults into a handoff."""
    if config.DEBUG_MODE:
        logger.info("--- Incoming Request ---", extra=log_extra)
        logger.info(
            "Headers",
            extra={**log_extra, "headers": logging_utils.redact_headers(raw_request.headers)},
        )
        logger.info(f"Body: {request.model_dump_json(indent=2)}", extra=log_extra)
        logger.info("------------------------", extra=log_extra)
    else:
        logger.info("Received request from Genesys", extra=log_extra)

    if not _verify_api_key(api_key):
        logger.warning("Forbidden: Invalid or missing API Key", extra=log_extra)
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing API Key")

    deployment_id = None
    session_ttl_duration = None
    if request.parameters:
        deployment_id = request.parameters.get("__deployment_id") or request.parameters.get("_deployment_id")
        if "_session_ttl" in request.parameters:
            raw_ttl = request.parameters.get("_session_ttl")
            try:
                if isinstance(raw_ttl, str):
                    raw_ttl = raw_ttl.strip()
                ttl_seconds = int(raw_ttl)
                if 1 <= ttl_seconds <= 86400:
                    session_ttl_duration = f"{ttl_seconds}s"
                else:
                    logger.warning(f"Invalid _session_ttl value (must be between 1 and 86400): {raw_ttl}", extra=log_extra)
            except (ValueError, TypeError):
                logger.warning(f"Failed to parse _session_ttl as integer: {raw_ttl}", extra=log_extra)

    session = await _load_or_create_session(request.genesysConversationId, deployment_id, log_extra)
    deployment_id = session.deployment_id
    turn_count = session.turn_count
    is_new_session = session.is_new_session

    if not deployment_id:
        logger.warning("Missing __deployment_id in parameters and no active session found.", extra=log_extra)
        raise HTTPException(status_code=400, detail="Missing __deployment_id in parameters and no active session found.")

    if not re.match(r"^projects/[^/]+/locations/[^/]+/apps/[^/]+/deployments/[^/]+$", deployment_id):
        logger.warning(f"Invalid __deployment_id format: {deployment_id}", extra=log_extra)
        raise HTTPException(status_code=400, detail="Invalid __deployment_id format. Expected: projects/*/locations/*/apps/*/deployments/*")

    parts = deployment_id.split('/')
    app_id = "/".join(parts[:6])
    location = parts[3]

    access_token = await _get_access_token()

    input_text_to_log = request.inputMessage.text
    if not config.DEBUG_MODE:
        input_text_to_log = "<REDACTED>"
    logger.info(f"Processing message for deployment {deployment_id}: {input_text_to_log}", extra=log_extra)

    input_text = request.inputMessage.text
    if request.inputMessage.content:
        for item in request.inputMessage.content:
            if item.contentType == "ButtonResponse" and item.buttonResponse:
                input_text = item.buttonResponse.payload or item.buttonResponse.text

    ces_variables = {}
    if request.parameters:
        for key, value in request.parameters.items():
            if not key.startswith("_"):
                ces_variables[key] = value



    ces_session_id = f"{app_id}/sessions/{request.genesysConversationId}"
    ces_url = f"https://ces.googleapis.com/v1/{ces_session_id}:runSession"

    inputs = []
    if ces_variables:
        inputs.append({"variables": ces_variables})

    if is_new_session:
        inputs.append({"event": {"event": "session_start"}})

    inputs.append({"text": input_text})

    session_config = {
        "session": ces_session_id,
        "deployment": deployment_id
    }
    if config.CES_EXCLUDE_DIAGNOSTIC_INFO:
        session_config["excludeDiagnosticInfo"] = True
    if session_ttl_duration:
        if config.CES_ENABLE_SESSION_TTL:
            session_config["sessionTtl"] = session_ttl_duration
        else:
            logger.warning(
                "_session_ttl supplied but CES_ENABLE_SESSION_TTL is false; ignoring.",
                extra=log_extra,
            )

    ces_payload = {
        "config": session_config,
        "inputs": inputs
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-goog-request-params": f"location=locations/{location}"
    }

    if config.DEBUG_MODE:
        logger.info(f"Sending request to CES URL: {ces_url}", extra=log_extra)
        logger.info("Sending payload", extra={**log_extra, "ces_request": ces_payload})

    try:
        data = await _call_ces(ces_url, ces_payload, headers, log_extra)
    except httpx.TimeoutException:
        # Distinct from a CES error: this means the budget was exhausted, which
        # is an operator-tunable condition rather than a backend fault. Alert on
        # `ces_timeout` separately so a latency regression is not mistaken for
        # CES returning errors.
        logger.error(
            f"CES call exceeded the {config.CES_TIMEOUT_SECONDS}s budget; handing off.",
            extra={
                **log_extra,
                "adapter_error": True,
                "ces_timeout": True,
                "ces_timeout_budget_seconds": config.CES_TIMEOUT_SECONDS,
            },
            exc_info=True,
        )
        return _failure_response(
            "Sorry, I'm having trouble right now. Let me connect you with someone who can help."
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error connecting to CES: {e.response.text}",
            extra={**log_extra, "adapter_error": True, "ces_status_code": e.response.status_code},
            exc_info=True,
        )
        return _failure_response(
            "Sorry, I'm having trouble right now. Let me connect you with someone who can help."
        )
    except Exception:
        logger.error(
            "Unexpected error connecting to CES.",
            extra={**log_extra, "adapter_error": True},
            exc_info=True,
        )
        return _failure_response(
            "Sorry, I'm having trouble right now. Let me connect you with someone who can help."
        )

    _log_ces_response(data, log_extra)

    reply_messages = []
    intent = "generic"
    bot_state = "MOREDATA"
    confidence = 1.0
    genesys_parameters = {}
    ces_turn_index = 0

    for output in data.get("outputs", []):
        raw_turn_index = output.get("turnIndex")
        if isinstance(raw_turn_index, int) and raw_turn_index > ces_turn_index:
            ces_turn_index = raw_turn_index

        if "text" in output:
            reply_messages.append({
                "type": "Text",
                "text": output["text"]
            })

        if "payload" in output:
            custom_payload = output["payload"]
            if isinstance(custom_payload, dict) and "genesys" in custom_payload:
                genesys_messages = custom_payload["genesys"]
                if isinstance(genesys_messages, list):
                    for msg in genesys_messages:
                        reply_messages.append(msg)
                        logger.info("Added rich content message to reply.", extra=log_extra)

        if "endSession" in output:
            bot_state = "COMPLETE"
            logger.info("Session completed by CES.", extra=log_extra)

            await _delete_session(request.genesysConversationId, log_extra)

            end_session_metadata = output["endSession"].get("metadata", {})
            session_escalated = end_session_metadata.get("session_escalated", False)

            params = end_session_metadata.get("params", {})
            if session_escalated:
                intent = "live-agent-handoff"
                if "reason" in params:
                    genesys_parameters["escalationReason"] = to_genesys_param(params["reason"])
            else:
                intent = "end-session"

            for k, v in params.items():
                if k != "reason" and not k.startswith("VBg_msg_"):
                    genesys_parameters[k] = to_genesys_param(v)

    if not reply_messages and bot_state != "COMPLETE":
         reply_messages.append({
             "type": "Text",
             "text": ""
         })

    # Intent rotation source, in order of preference:
    #   1. CES `turnIndex` -- authoritative, carried in the response itself, and
    #      therefore immune to which Cloud Run instance served the turn.
    #   2. The session store counter -- shared across instances via Firestore.
    #   3. The in-process counter -- per-instance, so it can drift if a session
    #      moves between instances. Only reachable when Firestore is unavailable.
    effective_turn = ces_turn_index or turn_count
    rotation_source = "ces_turn_index" if ces_turn_index else session.source

    if rotation_source in ("memory", "memory_degraded"):
        # Tier 3: CES omitted turnIndex and the shared counter was unavailable.
        # Surfaces only as intermittent Architect NoMatchError after a scale-up — alert on rotation_degraded.
        logger.warning(
            "Intent rotation is using the in-process turn counter; it can drift "
            "if this conversation is later served by another Cloud Run instance.",
            extra={
                **log_extra,
                "rotation_source": rotation_source,
                "rotation_degraded": True,
                # Only the Firestore-outage variant is an adapter fault; plain
                # "memory" means no session collection was configured at all.
                "adapter_error": rotation_source == "memory_degraded",
            },
        )

    computed_intent = intent
    if intent == "generic":
        if effective_turn % 2 == 0:
            computed_intent = "generic-even"
        else:
            computed_intent = "generic-odd"

    response = {
        "replymessages": reply_messages,
        "intent": computed_intent,
        "confidence": confidence,
        "botState": bot_state
    }

    if genesys_parameters:
        response["parameters"] = {
            str(k): to_genesys_param(v) for k, v in genesys_parameters.items()
        }

    logger.info(
        f"Responding with intent '{computed_intent}' (turn {effective_turn}, botState {bot_state}).",
        extra={
            **log_extra,
            "intent": computed_intent,
            "botState": bot_state,
            "turn": effective_turn,
            "rotation_source": rotation_source,
        },
    )

    if config.DEBUG_MODE:
        logger.info("--- Outgoing Response to Genesys ---", extra=log_extra)
        logger.info("Outgoing response", extra={**log_extra, "genesys_response": response})
        logger.info("------------------------------------", extra=log_extra)

    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting up Genesys Chat Adapter for CXAS on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
