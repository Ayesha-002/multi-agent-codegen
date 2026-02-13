import pika
import json
import os
import structlog
import redis
import requests
from utils import connect_rabbitmq, reconnect_on_failure

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

RABBITMQ_URL = os.getenv("RABBITMQ_URL")
REDIS_URL    = os.getenv("REDIS_URL")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
MODEL_NAME   = os.getenv("MODEL_NAME",  "deepseek-coder:6.7b-instruct-q4_K_M")


class ImproverAgent:
    def __init__(self):
        self.ollama_host = OLLAMA_HOST
        self.model       = MODEL_NAME
        self.redis       = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection  = None
        self.channel     = None
        logger.info("improver_agent_created", model=self.model)

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='improver', durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='improver', on_message_callback=self.callback)
        logger.info("improver_channel_ready")

    def improve_code(self, code: str, language: str, issues: list) -> dict:
        issues_text = "\n".join([
            f"- {i.get('description', str(i))}" if isinstance(i, dict) else f"- {i}"
            for i in issues
        ]) or "- Improve code quality and fix any potential issues"

        prompt = f"""You are an expert {language} programmer. Fix ALL the following issues in the code below.

Issues to fix:
{issues_text}

Current code:
```{language}
{code}
```

IMPORTANT: Respond with ONLY the fixed {language} code. No explanations, no markdown fences, just the raw code."""

        try:
            response = requests.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 2048}
                },
                timeout=180
            )

            if response.status_code != 200:
                return {"success": False, "error": f"Ollama error: {response.status_code}"}

            improved = response.json()['message']['content'].strip()

            # Strip markdown fences if present
            if "```" in improved:
                parts = improved.split("```")
                if len(parts) >= 2:
                    improved = parts[1]
                    if improved.lower().startswith(language.lower()):
                        improved = improved[len(language):].strip()

            return {"success": True, "code": improved.strip()}

        except requests.exceptions.Timeout:
            return {"success": False, "error": "Ollama timed out after 180s"}
        except Exception as e:
            logger.error("improve_exception", error=str(e))
            return {"success": False, "error": str(e)}

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            raw        = self.redis.get(f"workflow:{request_id}")
            state      = json.loads(raw) if raw else {}
            current_iter = state.get('iterations', 0)
            max_iter     = message.get('max_iterations', 5)

            # Check iteration limit
            if current_iter >= max_iter:
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [f"Max iterations ({max_iter}) reached"]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                ch.basic_ack(delivery_tag=method.delivery_tag)
                logger.warning("max_iterations_reached",
                               request_id=request_id, iterations=current_iter)
                return

            logger.info("improving_code",
                        request_id=request_id, iteration=current_iter)

            result = self.improve_code(
                code=message['code'],
                language=message.get('language', 'python'),
                issues=message.get('issues', [])
            )

            if result['success']:
                state['code']          = result['code']
                state['current_stage'] = 'verifier'
                state['iterations']    = current_iter + 1
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

                ch.basic_publish(
                    exchange='',
                    routing_key='verifier',
                    body=json.dumps({
                        "request_id":     request_id,
                        "code":           result['code'],
                        "language":       message.get('language', 'python'),
                        "max_iterations": max_iter
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                logger.info("improved_sent_to_verifier",
                            request_id=request_id, iteration=current_iter + 1)
            else:
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [result['error']]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                logger.error("improvement_failed",
                             request_id=request_id, error=result['error'])

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("improver_agent_starting")
        reconnect_on_failure(self, ImproverAgent.setup_channel)


if __name__ == "__main__":
    agent = ImproverAgent()
    agent.start()