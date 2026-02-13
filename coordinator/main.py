from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import structlog
import redis.asyncio as redis
import pika
import json
import uuid
from datetime import datetime
import os

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

app = FastAPI(title="Multi-Agent Code Generator")

redis_client: Optional[redis.Redis] = None
rabbitmq_connection = None
rabbitmq_channel = None

class CodeGenerationRequest(BaseModel):
    prompt: str
    language: str = "python"
    max_iterations: int = 5
    requirements: Optional[List[str]] = None

class CodeGenerationResponse(BaseModel):
    request_id: str
    status: str
    code: Optional[str] = None
    tests: Optional[List[Dict[str, Any]]] = None
    iterations: int = 0
    errors: Optional[List[str]] = None

class WorkflowState(BaseModel):
    request_id: str
    current_stage: str
    iterations: int
    code: Optional[str] = None
    test_results: Optional[Dict] = None
    errors: List[str] = []
    created_at: str
    updated_at: str

@app.on_event("startup")
async def startup():
    global redis_client, rabbitmq_connection, rabbitmq_channel

    try:
        # ✅ Uses env vars properly - no hardcoded passwords
        redis_url    = os.getenv("REDIS_URL")
        rabbitmq_url = os.getenv("RABBITMQ_URL")

        redis_client = await redis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )

        # Retry RabbitMQ connection (coordinator starts before RabbitMQ is ready)
        import time
        for attempt in range(15):
            try:
                params = pika.URLParameters(rabbitmq_url)
                params.heartbeat = 0
                rabbitmq_connection = pika.BlockingConnection(params)
                rabbitmq_channel    = rabbitmq_connection.channel()
                break
            except Exception:
                if attempt < 14:
                    time.sleep(3)
                else:
                    raise

        queues = ["code_writer", "verifier", "tester", "improver", "results"]
        for queue in queues:
            rabbitmq_channel.queue_declare(queue=queue, durable=True)

        logger.info("coordinator_started",
                    redis_connected=True, rabbitmq_connected=True)

    except Exception as e:
        logger.error("startup_failed", error=str(e))
        raise

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()
    if rabbitmq_connection:
        rabbitmq_connection.close()

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "coordinator"}

@app.post("/generate", response_model=CodeGenerationResponse)
async def generate_code(request: CodeGenerationRequest):
    request_id = str(uuid.uuid4())
    
    state = WorkflowState(
        request_id=request_id,
        current_stage="writer",
        iterations=0,
        created_at=datetime.utcnow().isoformat(),
        updated_at=datetime.utcnow().isoformat()
    )
    
    await redis_client.setex(f"workflow:{request_id}", 3600, state.json())
    
    message = {
        "request_id": request_id,
        "prompt": request.prompt,
        "language": request.language,
        "requirements": request.requirements or [],
        "max_iterations": request.max_iterations
    }
    
    rabbitmq_channel.basic_publish(
        exchange='',
        routing_key='code_writer',
        body=json.dumps(message),
        properties=pika.BasicProperties(delivery_mode=2, content_type='application/json')
    )
    
    logger.info("workflow_initiated", request_id=request_id)
    
    return CodeGenerationResponse(request_id=request_id, status="processing", iterations=0)

@app.get("/status/{request_id}", response_model=CodeGenerationResponse)
async def get_status(request_id: str):
    state_json = await redis_client.get(f"workflow:{request_id}")
    
    if not state_json:
        raise HTTPException(status_code=404, detail="Request not found")
    
    state = WorkflowState.parse_raw(state_json)
    
    return CodeGenerationResponse(
        request_id=request_id,
        status=state.current_stage,
        code=state.code,
        iterations=state.iterations,
        errors=state.errors if state.errors else None
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)