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

import os
import logging
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

def resolve_secret(secret_value: str) -> str:
    """
    Resolves a secret value. If it starts with 'projects/', it fetches it from
    Google Secret Manager; otherwise, it returns the value as is.
    """
    if secret_value and secret_value.startswith("projects/"):
        try:
            client = secretmanager.SecretManagerServiceClient()
            response = client.access_secret_version(name=secret_value)
            return response.payload.data.decode("UTF-8").strip()
        except Exception as e:
            logger.error(f"Failed to resolve secret from GSM ({secret_value}): {e}")
            raise e
    return secret_value

DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"
API_KEY = resolve_secret(os.environ.get("API_KEY"))
FIRESTORE_SESSIONS_COLLECTION = os.environ.get("FIRESTORE_SESSIONS_COLLECTION")
