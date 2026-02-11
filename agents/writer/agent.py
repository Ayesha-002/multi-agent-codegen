import pika
import json
import os
import structlog
from ollama import Client
from typing import Dict, Any
import redis

logger = structlog.get_logger()

class CodeWriterAgent:
    def __init__(self):
        # Initialize Ollama client (connects to host machine)
        self.ollama = Client(host=os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434"))
        self.model = os.getenv("MODEL_NAME", "deepseek-coder:33b-instruct-q4_K_M")
        
        # Redis for state
        self.redis = redis.from_url(
            os.getenv("REDIS_URL", "redis://:devpassword@redis:6379/0"),
            decode_responses=True
        )
        
        # RabbitMQ setup
        rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
        params = pika.URLParameters(rabbitmq_url)
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue='code_writer', durable=True)
        
        logger.info("code_writer_agent_initialized", model=self.model)
    
    def generate_code(self, prompt: str, language: str, requirements: list) -> Dict[str, Any]:
        """
        Generate code using local LLM
        """
        system_prompt = f"""You are an expert {language} programmer. Generate clean, production-ready code based on the user's requirements.

Requirements:
{chr(10).join(f"- {req}" for req in requirements)}

CRITICAL RULES:
1. Output ONLY the code, no explanations
2. Include proper error handling
3. Add type hints (for Python) or appropriate type annotations
4. Follow best practices and design patterns
5. Make code testable and modular"""

        try:
            response = self.ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                options={
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "num_predict": 2048
                }
            )
            
            code = response['message']['content']
            
            # Clean up markdown code blocks if present
            if "```" in code:
                code = code.split("```")[1]
                if code.startswith(language):
                    code = code[len(language):].strip()
            
            return {
                "success": True,
                "code": code.strip(),
                "model": self.model
            }
        
        except Exception as e:
            logger.error("code_generation_failed", error=str(e))
            return {
                "success": False,
                "error": str(e)
            }
    
    def callback(self, ch, method, properties, body):
        """
        Process incoming messages
        """
        try:
            message = json.loads(body)
            request_id = message['request_id']
            
            logger.info("processing_request", request_id=request_id)
            
            # Generate code
            result = self.generate_code(
                prompt=message['prompt'],
                language=message['language'],
                requirements=message.get('requirements', [])
            )
            
            if result['success']:
                # Update workflow state
                state = json.loads(self.redis.get(f"workflow:{request_id}"))
                state['code'] = result['code']
                state['current_stage'] = 'verifier'
                state['iterations'] += 1
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                
                # Send to verifier
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
                
                logger.info("code_generated_sent_to_verifier", request_id=request_id)
            else:
                # Mark as failed
                state = json.loads(self.redis.get(f"workflow:{request_id}"))
                state['current_stage'] = 'failed'
                state['errors'].append(result['error'])
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            
            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("message_processing_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    
    def start(self):
        """
        Start consuming messages
        """
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='code_writer', on_message_callback=self.callback)
        
        logger.info("code_writer_agent_started")
        self.channel.start_consuming()

if __name__ == "__main__":
    agent = CodeWriterAgent()
    agent.start()