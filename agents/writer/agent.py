import pika
import json
import os
import structlog
import redis
import requests
import time
from utils import connect_rabbitmq, reconnect_on_failure

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
REDIS_URL    = os.getenv("REDIS_URL",    "redis://:devpassword@redis:6379/0")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://host.docker.internal:11434")
MODEL_NAME   = os.getenv("MODEL_NAME",   "deepseek-coder:6.7b-instruct-q4_K_M")


class CodeWriterAgent:
    def __init__(self):
        self.ollama_host = OLLAMA_HOST
        self.model       = MODEL_NAME
        self.redis       = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection  = None
        self.channel     = None
        logger.info("code_writer_agent_created", model=self.model)

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='code_writer', durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='code_writer', on_message_callback=self.callback)
        logger.info("code_writer_channel_ready")

    def generate_code(self, prompt: str, language: str, requirements: list) -> dict:
        system_prompt = f"""You are an expert {language} programmer. Generate clean, production-ready code.

Requirements:
{chr(10).join(f"- {req}" for req in requirements) if requirements else "- None specified"}

RULES:
1. Output ONLY the code, no explanations outside code
2. Include proper error handling
3. Add type hints for Python
4. Follow best practices
5. Make code testable and modular"""

        try:
            response = requests.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt}
                    ],
                    "stream": False,
                    "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 2048}
                },
                timeout=180
            )

            if response.status_code != 200:
                return {"success": False, "error": f"Ollama error: {response.status_code} - {response.text}"}

            code = response.json()['message']['content']

            # Strip markdown code fences
            if "```" in code:
                parts = code.split("```")
                if len(parts) >= 2:
                    code = parts[1]
                    if code.lower().startswith(language.lower()):
                        code = code[len(language):].strip()

            return {"success": True, "code": code.strip()}

        except requests.exceptions.Timeout:
            return {"success": False, "error": "Ollama request timed out after 180s"}
        except Exception as e:
            logger.error("code_generation_failed", error=str(e))
            return {"success": False, "error": str(e)}

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            logger.info("generating_code", request_id=request_id)

            result = self.generate_code(
                prompt=message['prompt'],
                language=message.get('language', 'python'),
                requirements=message.get('requirements', [])
            )

            raw   = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}

            if result['success']:
                state['code']          = result['code']
                state['current_stage'] = 'verifier'
                state['iterations']    = state.get('iterations', 0) + 1
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

                ch.basic_publish(
                    exchange='',
                    routing_key='verifier',
                    body=json.dumps({
                        "request_id":     request_id,
                        "code":           result['code'],
                        "language":       message.get('language', 'python'),
                        "max_iterations": message.get('max_iterations', 5)
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                logger.info("code_generated_sent_to_verifier", request_id=request_id)
            else:
                state['current_stage'] = 'failed'
                state['errors']        = state.get('errors', []) + [result['error']]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                logger.error("code_generation_failed", request_id=request_id, error=result['error'])

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("code_writer_agent_starting")
        reconnect_on_failure(self, CodeWriterAgent.setup_channel)


if __name__ == "__main__":
    agent = CodeWriterAgent()
    agent.start()