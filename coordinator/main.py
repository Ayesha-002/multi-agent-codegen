from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import structlog
import redis.asyncio as redis
import pika
import json
import uuid
import time
import os
from datetime import datetime
from sanitizer import sanitize_generated_code

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

app = FastAPI(
    title="Multi-Agent Code Generator",
    description="AI-powered code generation with automatic prompt clarification",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global clients
redis_client       = None
rabbitmq_connection = None
rabbitmq_channel   = None

# ------------------------------------------------------------------ #
#  Request / Response models
# ------------------------------------------------------------------ #

class CodeGenerationRequest(BaseModel):
    prompt: str
    language: Optional[str] = None      # Optional now
    max_iterations: int = 5
    requirements: Optional[List[str]] = None

class ClarificationAnswer(BaseModel):
    answers: Dict[str, str]             # {"question": "answer"}

class CodeGenerationResponse(BaseModel):
    request_id: str
    status: str                         # processing/needs_clarification/completed/failed
    code: Optional[str]           = None
    language: Optional[str]       = None
    iterations: int               = 0
    errors: Optional[List[str]]   = None
    # Clarification fields
    questions: Optional[List[str]]      = None
    missing_info: Optional[List[str]]   = None
    enriched_prompt: Optional[str]      = None
    # Test results
    test_results: Optional[Dict]        = None
    message: Optional[str]             = None

# ------------------------------------------------------------------ #
#  Startup / Shutdown
# ------------------------------------------------------------------ #

def get_rabbitmq_connection(url: str):
    for attempt in range(15):
        try:
            params = pika.URLParameters(url)
            params.heartbeat = 0
            return pika.BlockingConnection(params)
        except Exception:
            if attempt < 14:
                time.sleep(3)
            else:
                raise

@app.on_event("startup")
async def startup():
    global redis_client, rabbitmq_connection, rabbitmq_channel
    try:
        redis_client = await redis.from_url(
            os.getenv("REDIS_URL"),
            encoding="utf-8",
            decode_responses=True
        )
        rabbitmq_connection = get_rabbitmq_connection(os.getenv("RABBITMQ_URL"))
        rabbitmq_channel    = rabbitmq_connection.channel()

        for queue in ["analyzer", "code_writer", "verifier", "tester", "improver"]:
            rabbitmq_channel.queue_declare(queue=queue, durable=True)

        logger.info("coordinator_started")
    except Exception as e:
        logger.error("startup_failed", error=str(e))
        raise

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()
    if rabbitmq_connection:
        try:
            rabbitmq_connection.close()
        except Exception:
            pass

# ------------------------------------------------------------------ #
#  Helper
# ------------------------------------------------------------------ #

def detect_explicit_language(prompt: str, language: Optional[str]) -> tuple[Optional[str], bool]:
    """Returns (language, was_it_explicitly_stated)"""
    if not language:
        return None, False
    # Check if language appears in prompt
    langs = ["python", "javascript", "typescript", "java", "go", "rust",
             "c", "cpp", "csharp", "php", "ruby", "swift", "kotlin",
             "sql", "bash", "html", "css", "js", "ts"]
    prompt_lower = prompt.lower()
    for lang in langs:
        if lang in prompt_lower:
            return language, True
    return language, bool(language)


# ------------------------------------------------------------------ #
#  Endpoints
# ------------------------------------------------------------------ #

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "coordinator",
        "version": "2.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/generate", response_model=CodeGenerationResponse)
async def generate_code(request: CodeGenerationRequest):
    """
    Submit a coding request. Can be vague - the system will ask for
    clarification if needed, or intelligently infer missing details.
    """
    request_id = str(uuid.uuid4())
    lang, explicit = detect_explicit_language(request.prompt, request.language)

    # Initial state
    state = {
        "request_id":    request_id,
        "current_stage": "analyzer",
        "iterations":    0,
        "code":          None,
        "test_results":  None,
        "errors":        [],
        "created_at":    datetime.utcnow().isoformat(),
        "updated_at":    datetime.utcnow().isoformat(),
        "original_prompt": request.prompt,
        "language":      lang
    }
    await redis_client.setex(f"workflow:{request_id}", 3600, json.dumps(state))

    # Send to analyzer first
    rabbitmq_channel.basic_publish(
        exchange='',
        routing_key='analyzer',
        body=json.dumps({
            "request_id":        request_id,
            "prompt":            request.prompt,
            "language":          lang,
            "explicit_language": explicit,
            "requirements":      request.requirements or [],
            "max_iterations":    request.max_iterations
        }),
        properties=pika.BasicProperties(delivery_mode=2)
    )

    logger.info("workflow_initiated", request_id=request_id,
                prompt_preview=request.prompt[:80])

    return CodeGenerationResponse(
        request_id=request_id,
        status="processing",
        message="Your request is being analyzed. Poll /status/{request_id} for updates."
    )


@app.post("/clarify/{request_id}", response_model=CodeGenerationResponse)
async def submit_clarification(request_id: str, answers: ClarificationAnswer):
    """
    Submit answers to clarification questions.
    Call this when status is 'needs_clarification'.
    """
    raw = await redis_client.get(f"workflow:{request_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Request not found or expired")

    state = json.loads(raw)

    if state['current_stage'] != 'needs_clarification':
        raise HTTPException(
            status_code=400,
            detail=f"Request is not awaiting clarification. Current stage: {state['current_stage']}"
        )

    original_message = state.get('original_message', {})
    original_message['user_answers'] = answers.answers

    # Pair questions with answers for context
    questions = state.get('questions', [])
    paired    = {q: answers.answers.get(q, answers.answers.get(str(i), ""))
                 for i, q in enumerate(questions)}
    original_message['user_answers'] = paired

    state['current_stage'] = 'analyzer'  # Re-run analyzer with answers
    state['iterations']    = 0
    await redis_client.setex(f"workflow:{request_id}", 3600, json.dumps(state))

    # Re-send to analyzer with answers
    rabbitmq_channel.basic_publish(
        exchange='',
        routing_key='analyzer',
        body=json.dumps(original_message),
        properties=pika.BasicProperties(delivery_mode=2)
    )

    logger.info("clarification_submitted", request_id=request_id)

    return CodeGenerationResponse(
        request_id=request_id,
        status="processing",
        message="Clarification received. Generating code now."
    )


@app.get("/status/{request_id}", response_model=CodeGenerationResponse)
async def get_status(request_id: str):
    """Get the current status of a code generation request."""
    raw = await redis_client.get(f"workflow:{request_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Request not found or expired (1h TTL)")

    state = json.loads(raw)
    stage = state['current_stage']
    code = sanitize_generated_code(state.get('code'), language=state.get('language') or "python")
    if code != state.get('code'):
        state['code'] = code
        await redis_client.setex(f"workflow:{request_id}", 3600, json.dumps(state))

    if stage == 'needs_clarification':
        return CodeGenerationResponse(
            request_id=request_id,
            status="needs_clarification",
            questions=state.get('questions', []),
            missing_info=state.get('missing_info', []),
            iterations=state.get('iterations', 0),
            message="Please answer the questions and POST to /clarify/{request_id}"
        )

    return CodeGenerationResponse(
        request_id=request_id,
        status=stage,
        code=code,
        language=state.get('language'),
        iterations=state.get('iterations', 0),
        errors=state.get('errors') or None,
        enriched_prompt=state.get('enriched_prompt'),
        test_results=state.get('test_results'),
        message="Completed!" if stage == "completed" else
                "Generation failed after max iterations." if stage == "failed" else
                f"Processing... current stage: {stage}"
    )


@app.get("/history")
async def get_history():
    """List recent requests stored in Redis."""
    keys = await redis_client.keys("workflow:*")
    results = []
    for key in keys[:20]:  # Limit to 20
        raw = await redis_client.get(key)
        if raw:
            state = json.loads(raw)
            results.append({
                "request_id":  state.get('request_id'),
                "status":      state.get('current_stage'),
                "language":    state.get('language'),
                "iterations":  state.get('iterations', 0),
                "created_at":  state.get('created_at'),
                "prompt":      state.get('original_prompt', '')[:100]
            })
    results.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return {"total": len(results), "requests": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
