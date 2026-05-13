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

# Check for values.sh
if [ ! -f "$(dirname "$0")/values.sh" ]; then
  echo "Error: values.sh not found. Please create it from values.sh.example first."
  exit 1
fi

source $(dirname "$0")/values.sh

if [ -z "$NGROK_DOMAIN" ]; then
  echo "Error: NGROK_DOMAIN is not set in values.sh."
  exit 1
fi

echo "Starting ngrok tunnel on port 8000 with domain $NGROK_DOMAIN..."
ngrok http --domain="$NGROK_DOMAIN" 8000
