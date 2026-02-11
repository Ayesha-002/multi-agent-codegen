import pika
import json
import os
import structlog
from typing import Dict, Any
import redis
import requests

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

class CodeWriterAgent:
    def __init__(self):
        self.ollama_host = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
        self.model = os.getenv("MODEL_NAME", "deepseek-coder:6.7b-instruct-q4_K_M")
        
        self.redis = redis.from_url(
            os.getenv("REDIS_URL", "redis://:devpassword@redis:6379/0"),
            decode_responses=True
        )
        
        rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
        params = pika.URLParameters(rabbitmq_url)
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue='code_writer', durable=True)
        
        logger.info("code_writer_agent_initialized", model=self.model)
    
    def generate_code(self, prompt: str, language: str, requirements: list) -> Dict[str, Any]:
        system_prompt = f"""You are an expert {language} programmer. Generate clean, production-ready code.

Requirements:
{chr(10).join(f"- {req}" for req in requirements)}

RULES:
1. Output ONLY the code, no explanations
2. Include proper error handling
3. Add type hints (for Python)
4. Follow best practices
5. Make code testable"""

        try:
            response = requests.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "num_predict": 2048
                    }
                },
                timeout=120
            )
            
            if response.status_code != 200:
                return {"success": False, "error": f"Ollama API error: {response.status_code}"}
            
            result = response.json()
            code = result['message']['content']
            
            if "```" in code:
                parts = code.split("```")
                if len(parts) >= 2:
                    code = parts[1]
                    if code.startswith(language):
                        code = code[len(language):].strip()
            
            return {"success": True, "code": code.strip(), "model": self.model}
        
        except Exception as e:
            logger.error("code_generation_failed", error=str(e))
            return {"success": False, "error": str(e)}
    
    def callback(self, ch, method, properties, body):
        try:
            message = json.loads(body)
            request_id = message['request_id']
            
            logger.info("processing_request", request_id=request_id)
            
            result = self.generate_code(
                prompt=message['prompt'],
                language=message['language'],
                requirements=message.get('requirements', [])
            )
            
            if result['success']:
                state = json.loads(self.redis.get(f"workflow:{request_id}"))
                state['code'] = result['code']
                state['current_stage'] = 'verifier'
                state['iterations'] = state.get('iterations', 0) + 1
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                
                verifier_message = {
                    "request_id": request_id,
                    "code": result['code'],
                    "language": message['language'],
                    "max_iterations": message['max_iterations']
                }
                
                ch.basic_publish(
                    exchange='',
                    routing_key='verifier',
                    body=json.dumps(verifier_message),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                
                logger.info("code_generated", request_id=request_id)
            else:
                state = json.loads(self.redis.get(f"workflow:{request_id}"))
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [result['error']]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("message_processing_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    
    def start(self):
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='code_writer', on_message_callback=self.callback)
        logger.info("code_writer_agent_started")
        self.channel.start_consuming()

if __name__ == "__main__":
    agent = CodeWriterAgent()
    agent.start()