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

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Union
import uuid
from datetime import datetime, timezone, timedelta

import google.auth
import google.auth.transport.requests
import httpx
import uvicorn
from cachetools import TTLCache
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from google.cloud import firestore

# Import local modules
from src import logging_utils
from src import config

# Set up structured logger
logger = logging_utils.setup_logger(__name__)

app = FastAPI(title="Genesys Chat Adapter for CXAS")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error(f"Validation Error: {exc.errors()}", extra={"body": body.decode('utf-8')})
    return JSONResponse(status_code=422, content={"detail": exc.errors(), "body": body.decode('utf-8')})

# Global authentication setup
credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
auth_req = google.auth.transport.requests.Request()

# Global session cache to retain __deployment_id between turns
session_cache = TTLCache(maxsize=10000, ttl=3600)

FIRESTORE_SESSIONS_COLLECTION = config.FIRESTORE_SESSIONS_COLLECTION

if FIRESTORE_SESSIONS_COLLECTION:
    firestore_db = firestore.AsyncClient(project=project, credentials=credentials)
    logger.info(f"Using Firestore collection '{FIRESTORE_SESSIONS_COLLECTION}' for session tracking.")
else:
    logger.warning("Session-to-deployment-id mapping is happening in-memory. This is only recommended for testing and should not be used in production.")


class ButtonResponse(BaseModel):
    type: str = Field(..., description="Describes the button that resulted in the Button Response, e.g., 'Button', 'QuickReply'.")
    text: str = Field(..., description="The response text from the button click.")
    payload: str = Field(..., description="The response payload associated with the clicked button.")

class ContentItem(BaseModel):
    contentType: str = Field(..., description="The type of content in this element, e.g., 'ButtonResponse'")
    content: Optional[str] = None
    buttonResponse: Optional[ButtonResponse] = None

class InputMessage(BaseModel):
    type: str = Field(..., description="The type of the utterance being sent, e.g., 'Text', 'Structured'")
    text: str = Field(..., description="The text of the message being sent to the bot.")
    content: Optional[List[ContentItem]] = Field(default_factory=list, description="For rich data content.")

class ChatRequest(BaseModel):
    botId: str
    botVersion: str
    inputMessage: InputMessage
    languageCode: str
    botSessionId: str
    botSessionTimeout: int
    genesysConversationId: str
    chatBot: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = Field(default_factory=dict)


@app.post("/v1/postUtterance")
async def handle_chat(
    raw_request: Request,
    request: ChatRequest,
    api_key: Optional[str] = Header(None, alias="api-key")
):
    """
    Receives chat utterances from Genesys, parses them, and forwards them
    to the CES backend using gRPC Transcoding via httpx.
    """
    adapter_session_id = str(uuid.uuid4())
    log_extra = {
        "adapter_session_id": adapter_session_id,
        "genesysConversationId": request.genesysConversationId,
        "botSessionId": request.botSessionId
    }

    if config.DEBUG_MODE:
        logger.info("--- Incoming Request ---", extra=log_extra)
        logger.info(f"Headers: {dict(raw_request.headers)}", extra=log_extra)
        logger.info(f"Body: {request.model_dump_json(indent=2)}", extra=log_extra)
        logger.info("------------------------", extra=log_extra)
    else:
        logger.info("Received request from Genesys", extra=log_extra)

    expected_api_key = config.API_KEY
    if not expected_api_key or api_key != expected_api_key:
        logger.warning("Forbidden: Invalid or missing API Key", extra=log_extra)
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing API Key")

    deployment_id = None
    if request.parameters:
        deployment_id = request.parameters.get("__deployment_id") or request.parameters.get("_deployment_id")
    
    is_new_session = False
    turn_count = 1
    
    if FIRESTORE_SESSIONS_COLLECTION:
        doc_ref = firestore_db.collection(FIRESTORE_SESSIONS_COLLECTION).document(request.genesysConversationId)
        if deployment_id:
            is_new_session = True
            expiry_time = datetime.now(timezone.utc) + timedelta(hours=24)
            await doc_ref.set({
                "deployment_id": deployment_id,
                "expiry_time": expiry_time,
                "turn_count": turn_count
            })
            logger.info(f"New session started and deployment_id {deployment_id} stored in Firestore.", extra=log_extra)
        else:
            doc = await doc_ref.get()
            if doc.exists:
                doc_dict = doc.to_dict()
                deployment_id = doc_dict.get("deployment_id")
                turn_count = doc_dict.get("turn_count", 0) + 1
                await doc_ref.update({"turn_count": turn_count})
                logger.info(f"Retrieved deployment_id {deployment_id} from Firestore. Turn: {turn_count}", extra=log_extra)
    else:
        if deployment_id:
            is_new_session = True
            session_cache[request.genesysConversationId] = {
                "deployment_id": deployment_id,
                "turn_count": turn_count
            }
            logger.info(f"New session started and deployment_id {deployment_id} stored in memory.", extra=log_extra)
        else:
            session_data = session_cache.get(request.genesysConversationId)
            if session_data:
                deployment_id = session_data.get("deployment_id")
                turn_count = session_data.get("turn_count", 0) + 1
                session_cache[request.genesysConversationId]["turn_count"] = turn_count
                logger.info(f"Retrieved deployment_id {deployment_id} from memory. Turn: {turn_count}", extra=log_extra)

    if not deployment_id:
        logger.warning("Missing __deployment_id in parameters and no active session found.", extra=log_extra)
        raise HTTPException(status_code=400, detail="Missing __deployment_id in parameters and no active session found.")

    if not re.match(r"^projects/[^/]+/locations/[^/]+/apps/[^/]+/deployments/[^/]+$", deployment_id):
        logger.warning(f"Invalid __deployment_id format: {deployment_id}", extra=log_extra)
        raise HTTPException(status_code=400, detail="Invalid __deployment_id format. Expected: projects/*/locations/*/apps/*/deployments/*")

    parts = deployment_id.split('/')
    app_id = "/".join(parts[:6])
    location = parts[3]

    # Refresh token if necessary
    if not credentials.valid:
        credentials.refresh(auth_req)

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

    ces_payload = {
        "config": {
            "session": ces_session_id,
            "deployment": deployment_id
        },
        "inputs": inputs
    }

    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "x-goog-request-params": f"location=locations/{location}"
    }

    if config.DEBUG_MODE:
        logger.info(f"Sending request to CES URL: {ces_url}", extra=log_extra)
        logger.info(f"Sending payload: {ces_payload}", extra=log_extra)

    try:
        async with httpx.AsyncClient() as client:
            ces_response = await client.post(ces_url, json=ces_payload, headers=headers, timeout=10.0)
            
            if config.DEBUG_MODE:
                logger.info(f"Received CES response status: {ces_response.status_code}", extra=log_extra)
                logger.info(f"Received CES response body: {ces_response.text}", extra=log_extra)
            
            ces_response.raise_for_status()
            data = ces_response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error connecting to CES: {e.response.text}", extra=log_extra)
        raise HTTPException(status_code=502, detail="Error communicating with CES backend.")
    except Exception as e:
        logger.error(f"Unexpected error connecting to CES: {e}", extra=log_extra)
        raise HTTPException(status_code=500, detail="Internal server error.")

    reply_messages = []
    intent = "generic"
    bot_state = "MOREDATA"
    confidence = 1.0
    genesys_parameters = {}

    for output in data.get("outputs", []):
        if "text" in output:
            reply_messages.append({
                "type": "Text",
                "text": output["text"]
            })
            
        if "endSession" in output:
            bot_state = "COMPLETE"
            logger.info("Session completed by CES.", extra=log_extra)
            
            if FIRESTORE_SESSIONS_COLLECTION:
                await firestore_db.collection(FIRESTORE_SESSIONS_COLLECTION).document(request.genesysConversationId).delete()
                logger.info("Cleaned up session from Firestore.", extra=log_extra)
            else:
                session_cache.pop(request.genesysConversationId, None)
                logger.info("Cleaned up session from memory.", extra=log_extra)
            
            end_session_metadata = output["endSession"].get("metadata", {})
            session_escalated = end_session_metadata.get("session_escalated", False)

            if session_escalated:
                intent = "live-agent-handoff"
                params = end_session_metadata.get("params", {})
                if "reason" in params:
                    genesys_parameters["escalationReason"] = params["reason"]
                
                for k, v in params.items():
                    if k != "reason" and not k.startswith("VBg_msg_"):
                        genesys_parameters[k] = v
            else:
                intent = "end-session"

    if not reply_messages and bot_state != "COMPLETE":
         reply_messages.append({
             "type": "Text",
             "text": ""
         })

    computed_intent = intent
    if intent == "generic":
        if turn_count % 2 == 0:
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
        response["parameters"] = genesys_parameters

    if config.DEBUG_MODE:
        logger.info("--- Outgoing Response to Genesys ---", extra=log_extra)
        logger.info(f"Body: {json.dumps(response, indent=2)}", extra=log_extra)
        logger.info("------------------------------------", extra=log_extra)

    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting up Genesys Chat Adapter for CXAS on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
