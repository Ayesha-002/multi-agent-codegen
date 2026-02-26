import json
import os

import pika
import redis
import structlog

from sanitizer import detect_repetition_issues, sanitize_generated_code
from utils import connect_rabbitmq, reconnect_on_failure

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

logger = structlog.get_logger()

RABBITMQ_URL = os.getenv("RABBITMQ_URL")
REDIS_URL = os.getenv("REDIS_URL")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


class VerifierAgent:
    def __init__(self):
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection = None
        self.channel = None
        logger.info("verifier_agent_created")

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue="verifier", durable=True)
        self.channel.queue_declare(queue="tester", durable=True)
        self.channel.queue_declare(queue="improver", durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue="verifier", on_message_callback=self.callback)
        logger.info("verifier_channel_ready")

    def verify_code(self, code: str, language: str) -> dict:
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

            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            return json.loads(result_text)
        except Exception as e:
            logger.error("groq_verification_failed", error=str(e))
            return {
                "has_issues": True,
                "severity": "high",
                "issues": [f"Verification failed: {str(e)}"],
            }

    def callback(self, ch, method, properties, body):
        request_id = None
        try:
            message = json.loads(body)
            request_id = message["request_id"]
            language = message.get("language", "python")
            logger.info("verifying_code", request_id=request_id)

            cleaned_code = sanitize_generated_code(message["code"], language=language) or ""
            repetition_issues = detect_repetition_issues(cleaned_code, language=language)
            result = self.verify_code(cleaned_code, language)

            raw = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}
            severity = str(result.get("severity", "none")).lower()
            has_issues = bool(result.get("has_issues", result.get("has_errors", False)))
            issues = list(result.get("issues", []))
            if repetition_issues:
                has_issues = True
                severity = "high"
                issues.extend(repetition_issues)
                state["errors"] = state.get("errors", []) + repetition_issues
                logger.warning("repetition_detected", request_id=request_id, issues=repetition_issues)

            state["code"] = cleaned_code

            if not has_issues or severity in ["low", "none"]:
                self.channel.basic_publish(
                    exchange="",
                    routing_key="tester",
                    body=json.dumps(
                        {
                            "request_id": request_id,
                            "code": cleaned_code,
                            "language": language,
                            "max_iterations": message.get("max_iterations", 5),
                        }
                    ),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                state["current_stage"] = "tester"
                logger.info("verification_passed_to_tester", request_id=request_id, severity=severity)
            else:
                self.channel.basic_publish(
                    exchange="",
                    routing_key="improver",
                    body=json.dumps(
                        {
                            "request_id": request_id,
                            "code": cleaned_code,
                            "language": language,
                            "issues": issues,
                            "max_iterations": message.get("max_iterations", 5),
                        }
                    ),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                state["current_stage"] = "improver"
                state["errors"] = state.get("errors", []) + [f"Verification severity: {severity}"]
                logger.info("verification_issues_sent_to_improver", request_id=request_id, severity=severity)

            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            logger.error("callback_error", error=str(e))
            try:
                raw = self.redis.get(f"workflow:{request_id}")
                state = json.loads(raw) if raw else {}
                state["current_stage"] = "failed"
                state["errors"] = state.get("errors", []) + [f"Verifier error: {str(e)}"]
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
