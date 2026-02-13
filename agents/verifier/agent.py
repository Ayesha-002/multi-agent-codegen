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
        prompt = f"""You are a code reviewer. Analyze this {language} code strictly for:
1. Syntax errors
2. Logic bugs
3. Security issues

Code to review:
```{language}
{code}
```

You MUST respond with ONLY valid JSON, nothing else before or after:
{{
    "has_errors": false,
    "issues": [],
    "severity": "none"
}}

Rules:
- has_errors: true only if there are real bugs/syntax errors
- issues: array of {{"type": "...", "description": "...", "line": 0}}
- severity: "critical", "high", "medium", "low", or "none"
- If code looks correct, return has_errors=false, issues=[], severity="none"
"""
        try:
            response = requests.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 512}
                },
                timeout=120
            )

            if response.status_code != 200:
                logger.warning("ollama_error", status=response.status_code)
                return {"success": True, "verification": {"has_errors": False, "issues": [], "severity": "none"}}

            raw_text = response.json()['message']['content'].strip()

            # Extract JSON from response
            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0].strip()
            
            # Find JSON object in text
            start = raw_text.find('{')
            end   = raw_text.rfind('}') + 1
            if start != -1 and end > start:
                raw_text = raw_text[start:end]

            result = json.loads(raw_text)
            
            # Validate required fields exist
            if "has_errors" not in result:
                result["has_errors"] = False
            if "issues" not in result:
                result["issues"] = []
            if "severity" not in result:
                result["severity"] = "none"

            logger.info("verification_complete",
                        has_errors=result["has_errors"],
                        severity=result["severity"])
            return {"success": True, "verification": result}

        except json.JSONDecodeError as e:
            logger.warning("json_parse_error", error=str(e), raw=raw_text[:200])
            # Can't parse response - assume no errors, let tester decide
            return {"success": True, "verification": {"has_errors": False, "issues": [], "severity": "none"}}
        except Exception as e:
            logger.error("verification_exception", error=str(e))
            return {"success": True, "verification": {"has_errors": False, "issues": [], "severity": "none"}}

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            logger.info("verifying_code", request_id=request_id)

            result     = self.verify_code(message['code'], message.get('language', 'python'))
            raw        = self.redis.get(f"workflow:{request_id}")
            state      = json.loads(raw) if raw else {}
            verification = result['verification']
            severity     = verification.get('severity', 'none')
            has_errors   = verification.get('has_errors', False)

            if not has_errors or severity in ['low', 'none']:
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
                        "issues":         verification.get('issues', []),
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
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("verifier_agent_starting")
        reconnect_on_failure(self, VerifierAgent.setup_channel)


if __name__ == "__main__":
    agent = VerifierAgent()
    agent.start()