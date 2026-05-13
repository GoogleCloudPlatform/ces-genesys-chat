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

### Request Structure Constraints

To function properly, your Genesys Bot Connector must include a `_deployment_id` as a custom parameter. This dictates where the traffic runs.

**Format Pattern**:
`projects/{project}/locations/{location}/apps/{app_id}/deployments/{deployment_id}`

**Sample Genesys Payload Definition**:
```json
{
    "botSessionId": "c86e246...",
    "inputMessage": {
        "text": "help me with my invoice",
        "type": "Text"
    },
    ...
    "parameters": {
        "_deployment_id": "projects/my-project/locations/us/apps/123/deployments/456"
    }
}
```

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