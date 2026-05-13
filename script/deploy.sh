#!/bin/bash

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

source $(dirname "$0")/values.sh

for REGION in "${REGIONS[@]}"
do
  echo "Deploying to region: $REGION"

  ENV_VARS_LIST=()
  SECRETS_LIST=()

  # Flexible secret handling
  if [[ "$GENESYS_API_KEY_SECRET" =~ ^projects/ ]]; then
    ENV_VARS_LIST+=("API_KEY=$GENESYS_API_KEY_SECRET")
  else
    SECRETS_LIST+=("API_KEY=$GENESYS_API_KEY_SECRET:latest")
  fi

  if [[ "$FIRESTORE_SESSIONS_COLLECTION" ]]; then
     ENV_VARS_LIST+=("FIRESTORE_SESSIONS_COLLECTION=$FIRESTORE_SESSIONS_COLLECTION")
  fi

  if [[ "$DEBUG" ]]; then
     ENV_VARS_LIST+=("DEBUG=$DEBUG")
  fi

  # Join lists with commas
  ENV_VARS_STRING=$(IFS=,; echo "${ENV_VARS_LIST[*]}")
  SECRETS_STRING=$(IFS=,; echo "${SECRETS_LIST[*]}")

  gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --service-account "$SERVICE_ACCOUNT" \
    --set-env-vars "$ENV_VARS_STRING" \
    --set-secrets "$SECRETS_STRING" \
    --cpu "$CPU" \
    --memory "$MEMORY" \
    --platform managed \
    --quiet
done
