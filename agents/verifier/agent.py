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
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


class VerifierAgent:
    def __init__(self):
        self.ollama_host = OLLAMA_HOST
        self.model       = MODEL_NAME
        self.redis       = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection  = None
        self.channel     = None
        logger.info("verifier_agent_created", model=self.model)

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='verifier', durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='verifier', on_message_callback=self.callback)
        logger.info("verifier_channel_ready")

    def verify_code(self, code: str, language: str) -> dict:
        """Verify code using Groq."""
        try:
            from groq import Groq
            
            client = Groq(api_key=os.getenv("GROQ_API_KEY"))
            
            prompt = f"""Review this {language} code for errors, bugs, and issues:

    {code}

    Return JSON only:
    {{"has_issues": true/false, "severity": "none/low/medium/high/critical", "issues": ["list of issues"]}}"""

            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1000,
            )
            
            result_text = response.choices[0].message.content.strip()
            
            # Extract JSON
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()
            
            result = json.loads(result_text)
            return result
            
        except Exception as e:
            logger.error("groq_verification_failed", error=str(e))
            return {"has_issues": False, "severity": "none", "issues": []}

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            logger.info("verifying_code", request_id=request_id)

            result     = self.verify_code(message['code'], message.get('language', 'python'))
            raw        = self.redis.get(f"workflow:{request_id}")
            state      = json.loads(raw) if raw else {}
            severity   = str(result.get('severity', 'none')).lower()
            has_issues = bool(result.get('has_issues', result.get('has_errors', False)))
            issues     = result.get('issues', [])

            if not has_issues or severity in ['low', 'none']:
                # ✅ Pass to tester
                ch.basic_publish(
                    exchange='',
                    routing_key='tester',
                    body=json.dumps({
                        "request_id":     request_id,
                        "code":           message['code'],
                        "language":       message.get('language', 'python'),
                        "max_iterations": message.get('max_iterations', 5)
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                state['current_stage'] = 'tester'
                logger.info("verification_passed_to_tester",
                            request_id=request_id, severity=severity)
            else:
                # ❌ Send to improver
                ch.basic_publish(
                    exchange='',
                    routing_key='improver',
                    body=json.dumps({
                        "request_id":     request_id,
                        "code":           message['code'],
                        "language":       message.get('language', 'python'),
                        "issues":         issues,
                        "max_iterations": message.get('max_iterations', 5)
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                state['current_stage'] = 'improver'
                state['errors'] = state.get('errors', []) + [f"Verification severity: {severity}"]
                logger.info("verification_issues_sent_to_improver",
                            request_id=request_id, severity=severity)

            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            try:
                raw   = self.redis.get(f"workflow:{request_id}")
                state = json.loads(raw) if raw else {}
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [f"Verifier error: {str(e)}"]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            except Exception:
                pass
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("verifier_agent_starting")
        reconnect_on_failure(self, VerifierAgent.setup_channel)


if __name__ == "__main__":
    agent = VerifierAgent()
    agent.start()
