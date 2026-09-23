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

  # projects/... path -> resolved in-process; bare name -> mounted by Cloud Run.
  if [[ "$GENESYS_API_KEY_SECRET" =~ ^projects/ ]]; then
    ENV_VARS_LIST+=("API_KEY=$GENESYS_API_KEY_SECRET")
  else
    SECRETS_LIST+=("API_KEY=$GENESYS_API_KEY_SECRET:latest")
  fi

  if [[ "$FIRESTORE_SESSIONS_COLLECTION" ]]; then
     ENV_VARS_LIST+=("FIRESTORE_SESSIONS_COLLECTION=$FIRESTORE_SESSIONS_COLLECTION")
  fi

  if [[ "$FIRESTORE_DATABASE_ID" ]]; then
     ENV_VARS_LIST+=("FIRESTORE_DATABASE_ID=$FIRESTORE_DATABASE_ID")
  fi

  if [[ "$DEBUG" ]]; then
     ENV_VARS_LIST+=("DEBUG=$DEBUG")
  fi

  if [[ "$CES_TIMEOUT_SECONDS" ]]; then
     ENV_VARS_LIST+=("CES_TIMEOUT_SECONDS=$CES_TIMEOUT_SECONDS")
  fi

  if [[ "$CES_EXCLUDE_DIAGNOSTIC_INFO" ]]; then
     ENV_VARS_LIST+=("CES_EXCLUDE_DIAGNOSTIC_INFO=$CES_EXCLUDE_DIAGNOSTIC_INFO")
  fi

  if [[ "$CES_ENABLE_SESSION_TTL" ]]; then
     ENV_VARS_LIST+=("CES_ENABLE_SESSION_TTL=$CES_ENABLE_SESSION_TTL")
  fi

  ENV_VARS_STRING=$(IFS=,; echo "${ENV_VARS_LIST[*]}")
  SECRETS_STRING=$(IFS=,; echo "${SECRETS_LIST[*]}")

  # Build the flag list conditionally. Passing `--set-secrets ""` (which happens
  # whenever the API key is a projects/... path, so SECRETS_LIST is empty) tells
  # Cloud Run to clear all secret bindings rather than leave them untouched.
  DEPLOY_FLAGS=()
  if [[ -n "$ENV_VARS_STRING" ]]; then
    DEPLOY_FLAGS+=(--set-env-vars "$ENV_VARS_STRING")
  fi
  if [[ -n "$SECRETS_STRING" ]]; then
    DEPLOY_FLAGS+=(--set-secrets "$SECRETS_STRING")
  fi

  gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --service-account "$SERVICE_ACCOUNT" \
    "${DEPLOY_FLAGS[@]}" \
    --cpu "$CPU" \
    --memory "$MEMORY" \
    --platform managed \
    --quiet
done
