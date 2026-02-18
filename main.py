from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
import httpx  # Async HTTP client for non-blocking API calls
import uvicorn
import os
import json
from datetime import datetime
from pydantic import BaseModel
from dotenv import load_dotenv
from user_agents import parse as parse_ua

# Import email utilities
from email_utils import convert_html_to_pdf, send_email_via_smtp

# Load environment variables from .env file if it exists
load_dotenv()


app = FastAPI()

# ==========================================
# BOOMI CONFIGURATION
# ==========================================
# Use environment variables for credentials, fallback to defaults for backward compatibility
BOOMI_URL = os.getenv("BOOMI_URL", "https://c02-usa-west.integrate-test.boomi.com/ws/simple/createGetAIData")
BOOMI_USERNAME = os.getenv("BOOMI_USERNAME", "Chatbot-POC@multiquipinc-U9F4Z5.0FN17D")
BOOMI_PASSWORD = os.getenv("BOOMI_PASSWORD", "f9c03846-70a1-4d53-bc80-bab2cfdfe35d")

# ==========================================
# MEMORY STORAGE
# ==========================================
# Stores the messages for every active user session.
session_storage = {} 

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default_guest"

class FeedbackRequest(BaseModel):
    question: str
    response: str
    rating: str  # "positive" or "negative"
    comment: str

class EmailRequest(BaseModel):
    email: str
    question: str
    response_html: str

# Initialize Jinja2 templates
templates = Jinja2Templates(directory="templates")

  

# ==========================================

# ROUTES

# ==========================================

@app.get("/", response_class=HTMLResponse)
async def get_ui(request: Request):
    """Serve desktop or mobile template based on User-Agent.
    When mobile users enable 'Request Desktop Site', the browser changes
    the UA string to a desktop one, so this automatically serves index.html."""
    ua_string = request.headers.get("user-agent", "")
    user_agent = parse_ua(ua_string)
    
    if user_agent.is_mobile or user_agent.is_tablet:
        return templates.TemplateResponse("mobile.html", {"request": request})
    else:
        return templates.TemplateResponse("index.html", {"request": request})

@app.post("/chat")
async def chat_endpoint(payload: ChatRequest):
    user_message = payload.message
    session_id = payload.session_id

    # 1. Initialize memory if needed
    if session_id not in session_storage:
        session_storage[session_id] = []
    
    # 2. Add User Message to Memory
    session_storage[session_id].append({"role": "user", "content": user_message})

    # 3. Context Pinning Logic (First 2 + Last 20)
    # This ensures the bot remembers the initial context (e.g. Model Number) 
    # while maintaining the recent conversation flow.
    history = session_storage[session_id]
    
    # Logic: If total history length exceeds 12 (2 pinned + 10 rotated),
    # slice it to keep the first 2 and the last 10.
    if len(history) > 12:
        full_history_payload = history[:2] + history[-10:]
    else:
        full_history_payload = history

    try:
        # 4. Async API Call to Boomi
        # Using httpx.AsyncClient to prevent blocking the server while waiting for external API.
        # Explicitly setting 300s timeout for all phases (connect, read, write, pool)
        timeout_config = httpx.Timeout(300.0, connect=300.0, read=300.0, write=300.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            response = await client.post(
                BOOMI_URL,
                headers={"Content-Type": "application/json"},
                json=full_history_payload, # Sends the list of history
                auth=(BOOMI_USERNAME, BOOMI_PASSWORD)
            )
            
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and data:
                bot_reply = data[0].get("content", "No content")
            elif isinstance(data, dict):
                bot_reply = data.get("content", "No content")
            else:
                bot_reply = "Empty response"
            
            # 4. Add Bot Reply to Memory
            session_storage[session_id].append({"role": "assistant", "content": bot_reply})
            
            return {"reply": bot_reply}
        else:
            # If error, remove the last user message so we don't confuse the AI next time
            session_storage[session_id].pop() 
            return {"reply": f"**Error {response.status_code}:** Unable to fetch data."}
            
    except httpx.RequestError as e:
        session_storage[session_id].pop() 
        return {"reply": f"**Connection Error:** {str(e)}"}
    except Exception as e:
        session_storage[session_id].pop() 
        return {"reply": f"**System Error:** {str(e)}"}

@app.post("/feedback")
async def save_feedback(feedback: FeedbackRequest):
    file_path = "User_Feedback.json"
    
    # Check if file exists, if not create list
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            json.dump([], f)

    FEEDBACK_API_URL = "https://securebqa.multiquip.com/ws/simple/executeUserfeedback"

    try:
        # Read existing data
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = [] # Handle corrupt file

        # Append new feedback
        new_entry = feedback.dict()
        data.append(new_entry)

        # Write back to local file
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
            
        # --- SEND TO EXTERNAL API ---
        try:
            timeout_config = httpx.Timeout(10.0) # Short timeout for feedback
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                # Fire and forget (await but don't fail main request)
                ext_response = await client.post(
                    FEEDBACK_API_URL,
                    headers={"Content-Type": "application/json"},
                    json=new_entry,
                    auth=(BOOMI_USERNAME, BOOMI_PASSWORD)
                )
                if ext_response.status_code == 200:
                    print("✅ External feedback submitted successfully")
                else:
                    print(f"⚠️ External feedback failed: {ext_response.status_code} - {ext_response.text}")
        except Exception as api_error:
            print(f"⚠️ Error submitting external feedback: {str(api_error)}")
        # ----------------------------

        return JSONResponse(content={"message": "Feedback saved successfully"}, status_code=200)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# Mount static files only for specific image assets (security improvement)
if os.path.exists("multiquip.png") or os.path.exists("multiquip_title.png"):
    # Create a custom static file handler that only serves allowed files
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    
    ALLOWED_STATIC_FILES = {
        "multiquip.png",
        "multiquip_title.png",
        "bot.png",
        "Bot_Thinking.png"
    }
    
    @app.get("/static/{filename}")
    async def serve_static(filename: str):
        if filename not in ALLOWED_STATIC_FILES:
            raise HTTPException(status_code=404, detail="File not found")
        if not os.path.exists(filename):
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(filename)


# ==========================================
# EMAIL ENDPOINT
# ==========================================
@app.post("/send-email")
async def send_email_endpoint(req: EmailRequest):
    # 1. Generate PDF
    # We pass a dict to the helper to format it
    pdf_content = convert_html_to_pdf({"question": req.question, "response_html": req.response_html})
    
    if not pdf_content:
        return {"success": False, "message": "PDF Generation Failed"}
    
    # 2. Send Email
    success = send_email_via_smtp(req.email, pdf_content)
    
    if success:
        return {"success": True}
    else:
        return {"success": False, "message": "Failed to send email (SMTP Error)"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000 , timeout_keep_alive=300)