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

import logging
import os
import time

from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_SECRET_FETCH_ATTEMPTS = 3
_SECRET_FETCH_BACKOFF_SECONDS = 0.5


def _env_flag(name: str, default: bool) -> bool:
    """Reads a boolean environment variable, tolerating unset/blank values."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Reads a float env var, clamped to [minimum, maximum]. Bad or out-of-range
    values are logged and the default is used — an unbounded or zero timeout
    would defeat the Genesys deadline budget."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using default {default}.")
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            f"{name}={value} is outside the supported range "
            f"[{minimum}, {maximum}]; using default {default}."
        )
        return default
    return value


def _env_str(name: str) -> str | None:
    """Reads an optional string env var; blank or whitespace-only counts as unset."""
    return (os.environ.get(name) or "").strip() or None


def resolve_secret(secret_value: str) -> str:
    """Resolves a 'projects/...' value via Secret Manager, else returns it verbatim.
    Retried: this runs at import, so one transient GSM error would crash-loop the revision."""
    if secret_value and secret_value.startswith("projects/"):
        last_error: Exception | None = None
        for attempt in range(1, _SECRET_FETCH_ATTEMPTS + 1):
            try:
                client = secretmanager.SecretManagerServiceClient()
                response = client.access_secret_version(name=secret_value)
                return response.payload.data.decode("UTF-8").strip()
            except Exception as e:  # noqa: BLE001 - retried and re-raised below
                last_error = e
                logger.warning(
                    "Failed to resolve secret from GSM (%s), attempt %d/%d: %s",
                    secret_value,
                    attempt,
                    _SECRET_FETCH_ATTEMPTS,
                    e,
                )
                if attempt < _SECRET_FETCH_ATTEMPTS:
                    time.sleep(_SECRET_FETCH_BACKOFF_SECONDS * attempt)
        logger.error(
            f"Failed to resolve secret from GSM ({secret_value}) after "
            f"{_SECRET_FETCH_ATTEMPTS} attempts: {last_error}"
        )
        raise last_error
    return secret_value


def _resolve_api_keys(raw: str | None) -> list[str]:
    """Comma-separated so a new key can be accepted alongside the old one
    during rotation. Each entry may be a literal or a Secret Manager name."""
    if not raw:
        return []
    return [resolved for entry in raw.split(",") if (resolved := resolve_secret(entry.strip()))]


DEBUG_MODE = _env_flag("DEBUG", False)

API_KEYS = _resolve_api_keys(os.environ.get("API_KEY"))

FIRESTORE_SESSIONS_COLLECTION = _env_str("FIRESTORE_SESSIONS_COLLECTION")

# Named Firestore database holding that collection. Unset means None, which the
# Firestore client resolves to the project's "(default)" database.
FIRESTORE_DATABASE_ID = _env_str("FIRESTORE_DATABASE_ID")

# diagnosticInfo is ~97% of the response body and unused by this adapter.
# Kept in DEBUG so traces stay available for troubleshooting.
CES_EXCLUDE_DIAGNOSTIC_INFO = _env_flag("CES_EXCLUDE_DIAGNOSTIC_INFO", not DEBUG_MODE)

# EXPERIMENTAL, allowlist-only: CES rejects sessionTtl as an unknown field
# without access. Off by default; the adapter strips it and retries once on rejection.
CES_ENABLE_SESSION_TTL = _env_flag("CES_ENABLE_SESSION_TTL", False)

# Whole-call budget for CES. Genesys abandons a slow postUtterance and takes its
# Failure branch, so this must stay under that deadline for the graceful handoff
# to land. Genesys does not publish the deadline (botSessionTimeout is a session
# TTL, not a per-turn one), so 7.0 is a conservative guess, not a measured bound.
CES_TIMEOUT_SECONDS = _env_float("CES_TIMEOUT_SECONDS", 7.0, minimum=0.5, maximum=30.0)

# Connect is held tight: with a warm pool a slow handshake means a network fault,
# so fail fast and leave budget for the retry.
CES_CONNECT_TIMEOUT_SECONDS = min(3.0, CES_TIMEOUT_SECONDS)
