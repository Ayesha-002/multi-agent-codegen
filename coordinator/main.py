from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import structlog
import redis.asyncio as redis
import pika
import json
import uuid
import asyncio
from datetime import datetime

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

app = FastAPI(title="Multi-Agent Code Generator")

# Global state
redis_client: Optional[redis.Redis] = None
rabbitmq_connection = None
rabbitmq_channel = None

# Request/Response Models
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
    execution_time_seconds: Optional[float] = None

# Workflow State
class WorkflowState(BaseModel):
    request_id: str
    current_stage: str
    iterations: int
    code: Optional[str]
    test_results: Optional[Dict]
    errors: List[str]
    created_at: datetime
    updated_at: datetime

@app.on_event("startup")
async def startup():
    global redis_client, rabbitmq_connection, rabbitmq_channel
    
    # Initialize Redis
    redis_client = await redis.from_url(
        "redis://:devpassword@redis:6379/0",
        encoding="utf-8",
        decode_responses=True
    )
    
    # Initialize RabbitMQ
    import os
    rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
    params = pika.URLParameters(rabbitmq_url)
    rabbitmq_connection = pika.BlockingConnection(params)
    rabbitmq_channel = rabbitmq_connection.channel()
    
    # Declare queues
    queues = ["code_writer", "verifier", "tester", "improver", "results"]
    for queue in queues:
        rabbitmq_channel.queue_declare(queue=queue, durable=True)
    
    logger.info("coordinator_started", redis_connected=True, rabbitmq_connected=True)

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()
    if rabbitmq_connection:
        rabbitmq_connection.close()
    logger.info("coordinator_shutdown")

@app.post("/generate", response_model=CodeGenerationResponse)
async def generate_code(request: CodeGenerationRequest, background_tasks: BackgroundTasks):
    """
    Main endpoint to trigger code generation workflow
    """
    request_id = str(uuid.uuid4())
    
    # Initialize workflow state
    state = WorkflowState(
        request_id=request_id,
        current_stage="writer",
        iterations=0,
        code=None,
        test_results=None,
        errors=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    
    # Store in Redis
    await redis_client.setex(
        f"workflow:{request_id}",
        3600,  # 1 hour TTL
        state.json()
    )
    
    # Publish to code_writer queue
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
        properties=pika.BasicProperties(
            delivery_mode=2,  # Persistent
            content_type='application/json'
        )
    )
    
    logger.info("workflow_initiated", request_id=request_id, prompt=request.prompt[:100])
    
    return CodeGenerationResponse(
        request_id=request_id,
        status="processing",
        iterations=0
    )

@app.get("/status/{request_id}", response_model=CodeGenerationResponse)
async def get_status(request_id: str):
    """
    Check workflow status
    """
    state_json = await redis_client.get(f"workflow:{request_id}")
    
    if not state_json:
        raise HTTPException(status_code=404, detail="Request not found")
    
    state = WorkflowState.parse_raw(state_json)
    
    if state.current_stage == "completed":
        return CodeGenerationResponse(
            request_id=request_id,
            status="completed",
            code=state.code,
            tests=state.test_results.get("tests") if state.test_results else None,
            iterations=state.iterations,
            errors=state.errors if state.errors else None
        )
    elif state.current_stage == "failed":
        return CodeGenerationResponse(
            request_id=request_id,
            status="failed",
            code=state.code,
            iterations=state.iterations,
            errors=state.errors
        )
    else:
        return CodeGenerationResponse(
            request_id=request_id,
            status="processing",
            iterations=state.iterations
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)