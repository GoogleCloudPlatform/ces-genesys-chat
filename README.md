# Genesys Chat Adapter for CXAS

The **Genesys Chat Adapter for CXAS** is a lightweight middleware application designed to seamlessly connect Genesys Cloud chat deployments with Google Cloud's CX Agent Studio (CXAS) agents. 

## Executive Overview

Modern customer service operations often rely on sophisticated bot experiences managed centrally on platforms like Google Cloud. Meanwhile, customer interactions happen on established platforms like Genesys Cloud. 

This adapter acts as a high-speed bridge between the two platforms. Instead of forcing you to build complex, custom integrations directly inside Genesys, this adapter handles all the translation work automatically:

*   **Real-time Passthrough:** It listens to incoming messages from Genesys chat interactions.
*   **Format Translation:** It translates the proprietary Genesys message structure into a format that Google's CXAS agents understand natively.
*   **Intelligent Routing:** It automatically identifies the correct underlying application and deployment within CXAS.
*   **Context Continuity:** It preserves custom user parameters and session context, preventing the AI from losing track of the conversation history.
*   **Escalation Handling:** It natively understands when the Google AI decides to transfer the user to a human agent, gracefully formatting the response to inform Genesys to execute the handover.

By utilizing this adapter, organizations can rapidly deploy advanced Google Cloud AI agents directly over their existing Genesys chat infrastructure without locking themselves into complex, interdependent architectures.

---

## Developer Guide

The Genesys Chat Adapter for CXAS is a Python 3.13+ application built on top of the **FastAPI** web framework. It relies on standard internal Google `httpx` and `google-auth` mechanisms to interact with the CES gRPC REST transcoding interface (`ces.googleapis.com`).

### Architecture
1. **Endpoint**: `POST /v1/postUtterance` receives the JSON payload from a Genesys Webhook / Bot Connector.
2. **Authentication**: The API is secured via a required `API-KEY` header, verifiable against an environment variable (or Google Secret Manager path).
3. **Session Routing**: The adapter dynamically constructs the CES endpoint by extracting the application identity and location directly from either the `__deployment_id` or `_deployment_id` parameter in the Genesys request payload. **A single adapter can route traffic for multiple distinct bots**.
4. **Stateful Turn Tracking**: Mappings of session IDs to deployment IDs and turn counts are stored persistently:
   * By default, these are kept in-memory for testing purposes via a TTLCache.
   * For production, set `FIRESTORE_SESSIONS_COLLECTION` to store session documents in Firestore. Old session documents are automatically cleaned up using Firestore's TTL policy based on the `expiry_time` field (set to 24h into the future).
5. **Session TTL Override**: You can dynamically override the default 30-minute Google session lifetime by passing the `_session_ttl` parameter in the Genesys request (validated between 1 and 86400 seconds).

### Loop Prevention (Intent Rotation)

#### The Limitation
To prevent infinite loops and wasteful processing of resources, the Genesys BYOB (Bring Your Own Bot) Bot Connector v1 automatically terminates any chatbot session that returns the same intent name for more than 3 consecutive interactions without slot value updates. It will raise a `NoMatchError` and fail the session. 

#### The Workaround (Intent Rotation)
To bypass this restriction, this adapter tracks conversation turns (`turn_count`) statefully and alternates the returned intent name between `generic-even` and `generic-odd` on successive turns. This resets Genesys BYOB's loop detection counter on every transaction, allowing the conversation to proceed indefinitely:

*   **Odd Turns**: The adapter returns the intent as `generic-odd`.
*   **Even Turns**: The adapter returns the intent as `generic-even`.
*   **On Session End**: When CXAS signals session completion, it returns the intent as `end-session`.
*   **On Escalation**: On handover, it returns the intent as `live-agent-handoff`.

#### Registering the Intents in Genesys
To support this workaround, you must register the `generic-even`, `generic-odd`, `end-session`, and `live-agent-handoff` intents in your Genesys Bot Connector configuration. 

Below is the `curl` command and the corresponding payload structure to update the Bot List configuration via the Genesys Cloud Admin API:

```bash
curl -X PUT 'https://api.usw2.pure.cloud/api/v2/integrations/botconnector/YOUR_BOT_CONNECTOR_INTEGRATION_ID/bots' \
  -H 'Authorization: Bearer YOUR_GENESYS_ACCESS_TOKEN' \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "chatBots": [
      {
        "id": "YOUR_CXAS_APP_ID",
        "name": "ces-genesys-chat-agent",
        "description": "CXAS Bot integration",
        "versions": [
          {
            "version": "1.0",
            "supportedLanguages": ["en-US"],
            "intents": [
              {"name": "generic-even"},
              {"name": "generic-odd"},
              {"name": "end-session"},
              {"name": "live-agent-handoff"}
            ]
          }
        ]
      }
    ]
  }'
```

### Prerequisites
*   Python 3.13+
*   The `uv` package manager (recommended) or `pip`.
*   A Google Cloud Project with the `ces.googleapis.com` API enabled and Firestore enabled (if using Firestore session mapping).
*   Application Default Credentials (ADC) configured locally, or a Service Account when deployed.

### Local Development Setup

1. **Clone the repository** and navigate into the root directory.
2. **Install dependencies** using `uv`:
   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```
3. **Authenticate** with Google Cloud:
   ```bash
   gcloud auth application-default login
   ```
4. **Set your environment variables**:
   Create a local setup script or export directly:
   ```bash
   export API_KEY="your-secret-local-dev-key"
   export DEBUG="true"
   export FIRESTORE_SESSIONS_COLLECTION="ces_sessions" # Optional: Uses Firestore for scalable session tracking
   ```
5. **Run the server**:
   ```bash
   uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
   ```

### System Parameters / Input Variables

The adapter reserves specific parameter keys starting with an underscore (`_`) to control runtime configurations dynamically from your Genesys Architect Flow.

#### 1. Bot Deployment Routing (`_deployment_id` or `__deployment_id`)
Required to dictate which Google CX Agent Studio deployment this session routes to.
* **Format**: `projects/{project}/locations/{location}/apps/{app_id}/deployments/{deployment_id}`

#### 2. Session Time-to-Live Override (`_session_ttl`)
Optional. Overrides Google's default 30-minute session lifetime. It accepts a string representation of the seconds to keep the session active since the last interaction.
* **Bounds**: Min `1` second, Max `86400` seconds (24 hours).
* **Example**: `"3600"` (1 hour).

**Sample Genesys request variables payload**:
```json
{
    "botId": "ces-genesys-chat-agent",
    "botVersion": "1.0",
    "inputMessage": {
        "type": "Text",
        "text": "hi"
    },
    "languageCode": "en-us",
    "genesysConversationId": "a5bf086a-b019-4d50-a5dd-8b394720b71f",
    "parameters": {
        "_deployment_id": "projects/my-project/locations/us/apps/123/deployments/456",
        "_session_ttl": "3600"
    }
}
```

### Session End & Escalation Parameters

When a session is completed (`botState` becomes `COMPLETE`) or escalated to a human agent (`intent` becomes `live-agent-handoff`), any custom parameters or metadata returned by the Google CXAS agent (such as target queues or handoff reasons) are formatted and returned in the `parameters` field of the JSON response.

#### Strict String Coercion
The Genesys Cloud Bot Connector v1 integration strictly enforces a **flat string-to-string JSON map** for `"parameters"`. To comply with this constraint and prevent Genesys from dropping the response:
* The adapter **unconditionally coerces all output parameter keys and values to string types** (e.g., converting a numeric `priority: 10` to `"priority": "10"`) before returning them to Genesys.
* Ensure your Genesys Architect flow receives these variables as strings and parses/converts them locally inside the flow if necessary.

### Deployment (Cloud Run)

The application is fully configured to be deployed as a containerless Python service on Google Cloud Run via Buildpacks (defined via `Procfile`).

1. Ensure the Google Cloud SDK is installed and authorized.
2. Copy the template `script/values.sh.example` to `script/values.sh` and configure your target `PROJECT_ID`, `SERVICE_NAME`, `LOCATION` and the Google Secret Manager path for your `API_KEY`.
3. Execute the deployment script:
   ```bash
   ./script/deploy.sh
   ```

The script will automatically allocate memory, set concurrency values, attach the Service Account, and inject the `API_KEY` from Secret Manager directly into the runtime environment.

### Logging and Monitoring

*   **Production (Default)**: Logs are kept clean at the `INFO` level. Structural validation errors (`422 Unprocessable Content`) are inherently logged to identify schema drift between Genesys and the Adapter.
*   **Debug Mode**: Set the environment variable `DEBUG="true"` to force the application to dump the full raw JSON input from Genesys and the full outbound payload to CES for deep functional troubleshooting.

### CX Agent Studio testing payloads
This section contains example before LLM callbacks which will override `LlmResponse` when the test keyword is detected in the user response. Use as needed for testing the integration, these are not intended for production agent builds.

#### Quick reply
In the CX Agent Studio console, add **Before LLM** callback to the Root Agent on your testing agent.

Paste this python code as the callback function:

```python
import json
import base64

def get_user_text(context: CallbackContext) -> str:
  return context.user_content.parts[0].text

def get_payload_part(payload_string: str):
  return Part(inline_data=ces_internal.Blob(data=base64.b64encode
  (payload_string.encode("utf-8")), mime_type="application/json"))

def before_model_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:
  user_text = get_user_text(callback_context)
  if "quick reply" in user_text.lower():
    payload = {
      "genesys": [
          {
              "type": "Structured",
              "text": "Structured Type Payload",
              "content": [
                  {
                      "contentType": "QuickReply",
                      "quickReply": {
                          "text": "QuickReply1",
                          "payload": "quickreply1"
                      }
                  },
                  {
                      "contentType": "QuickReply",
                      "quickReply": {
                          "text": "QuickReply2",
                          "payload": "quickreply2"
                      }
                  }
              ]
          }
      ]
  }
    payload_json_string = json.dumps(payload)
    return LlmResponse(content=Content(parts=[Part(text="[NGA Quick](https://developer.genesys.cloud/commdigital/textbots/botconnector-customer-api-spec) *Reply* _Payload_"), get_payload_part(payload_json_string)], role="model"))
```

Once the deployment version is updated, you can trigger this payload response with text `"quick reply"`.

#### Content Card
In the CX Agent Studio console, add **Before LLM** callback to the Root Agent on your testing agent.

Paste this python code as the callback function:

```python
import json
import base64

def get_user_text(context: CallbackContext) -> str:
  return context.user_content.parts[0].text

def get_payload_part(payload_string: str):
  return Part(inline_data=ces_internal.Blob(data=base64.b64encode
  (payload_string.encode("utf-8")), mime_type="application/json"))

def before_model_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:
  user_text = get_user_text(callback_context)
  if "content card" in user_text.lower():
    payload = {
      "genesys": [
          {
              "type": "Structured",
              "text": "Structured Type Payload",
              "content": [
                  {
                      "contentType": "Card",
                      "card": {
                          "title": "Content Card Title",
                          "description": "Content Card Description",
                          "image": "https://docs.cloud.google.com/static/customer-engagement-ai/conversational-agents/ps/images/root-sub-agent.png",
                          "defaultAction": {
                              "type": "Link",
                              "url": "http://www.google.com/"
                          },
                          "actions": [
                              {
                                  "type": "Link",
                                  "text": "Actions Text",
                                  "url": "https://docs.cloud.google.com/customer-engagement-ai/conversational-agents/ps"
                              # }
                              },
                              {
                                  "type": "Postback",
                                  "text": "Postback Text",
                                  "payload": "Postback Payload"
                              }
                          ]
                      }
                  }
              ]
          }
      ]
  }
    payload_json_string = json.dumps(payload)
    return LlmResponse(content=Content(parts=[Part(text="[NGA Content](https://developer.genesys.cloud/commdigital/textbots/botconnector-customer-api-spec) *Card* _Payload_"), get_payload_part(payload_json_string)], role="model"))
```

Once the deployment version is updated, you can trigger this payload response with text `"content card"`.

#### Carousel
In the CX Agent Studio console, add **Before LLM** callback to the Root Agent on your testing agent.

Paste this python code as the callback function:

```python
import json
import base64

def get_user_text(context: CallbackContext) -> str:
  return context.user_content.parts[0].text

def get_payload_part(payload_string: str):
  return Part(inline_data=ces_internal.Blob(data=base64.b64encode
  (payload_string.encode("utf-8")), mime_type="application/json"))

def before_model_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:
  user_text = get_user_text(callback_context)
  if "carousel" in user_text.lower():
    payload = {
      "genesys": [
          {
              "type": "Structured",
              "text": "Structured Type Payload",
              "content": [
                  {
                      "contentType": "Carousel",
                      "carousel": {
                          "cards": [
                              {
                                  "title": "Carousel Title 1",
                                  "description": "Carousel Description 1",
                                  "image": "https://docs.cloud.google.com/static/customer-engagement-ai/conversational-agents/ps/images/root-sub-agent.png",
                                  "defaultAction": {
                                      "type": "Link",
                                      "url": "http://www.google.com/"
                                  },
                                  "actions": [
                                      {
                                          "type": "Link",
                                          "text": "Actions Link 1",
                                          "url": "http://www.google.com/"
                                      },
                                      {
                                          "type": "Postback",
                                          "text": "Postback Text 1",
                                          "payload": "postbackPayload1"
                                      }
                                  ]
                              },
                              {
                                  "title": "Carousel Title 2",
                                  "description": "Carousel Description 2",
                                  "image": "https://docs.cloud.google.com/static/customer-engagement-ai/conversational-agents/ps/images/web-widget-architecture.png",
                                  "defaultAction": {
                                      "type": "Link",
                                      "url": "https://store.google.com/us/"
                                  },
                                  "actions": [
                                      {
                                          "type": "Link",
                                          "text": "Actions Link 2",
                                          "url": "https://store.google.com/us/"
                                      },
                                      {
                                          "type": "Postback",
                                          "text": "Postback Text 2",
                                          "payload": "postbackPayload2"
                                      }
                                  ]
                              }
                          ]
                      }
                  }
              ]
          }
      ]
  }
    payload_json_string = json.dumps(payload)
    return LlmResponse(content=Content(parts=[Part(text="[NGA](https://developer.genesys.cloud/commdigital/textbots/botconnector-customer-api-spec) *Carousel* _Payload_"), get_payload_part(payload_json_string)], role="model"))
```

Once the deployment version is updated, you can trigger this payload response with text `"carousel"`.