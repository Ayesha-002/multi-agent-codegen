import pika
import json
import os
import structlog
import redis
import requests
import re
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

    @staticmethod
    def remove_repeated_output(code: str) -> str:
        """Trim accidental duplicated code blocks from model output."""
        cleaned = code.strip()
        if len(cleaned) < 120:
            return cleaned

        # Fast path: repeated long character prefix.
        prefix_len = min(180, max(60, len(cleaned) // 4))
        marker = cleaned[:prefix_len]
        repeat_at = cleaned.find(marker, prefix_len)
        if repeat_at > 0:
            return cleaned[:repeat_at].rstrip()

        # Robust path: repeated block starting at a later line.
        lines = cleaned.splitlines()
        if len(lines) < 12:
            return cleaned

        first_line = lines[0].strip()
        for idx in range(8, len(lines)):
            if lines[idx].strip() != first_line:
                continue
            matched = 0
            while idx + matched < len(lines) and matched < len(lines):
                if lines[idx + matched] != lines[matched]:
                    break
                matched += 1
            if matched >= 8:
                return "\n".join(lines[:idx]).rstrip()

        # Catch repeated code blocks even when the output starts with prose.
        block_size = 8
        min_match = 12
        max_start = min(30, len(lines) - block_size)
        for start in range(max_start):
            marker = lines[start:start + block_size]
            for idx in range(start + block_size, len(lines) - block_size + 1):
                if lines[idx:idx + block_size] != marker:
                    continue
                matched = 0
                while start + matched < len(lines) and idx + matched < len(lines):
                    if lines[start + matched] != lines[idx + matched]:
                        break
                    matched += 1
                if matched >= min_match:
                    return "\n".join(lines[:idx]).rstrip()
        return cleaned

    def improve_code(self, code: str, language: str, issues: list | None = None) -> str:
        """Generate code using Groq (free & fast)."""
        try:
            from groq import Groq
            
            client = Groq(api_key=os.getenv("GROQ_API_KEY"))
            issues = issues or []
            issue_lines = []
            for item in issues:
                if isinstance(item, dict):
                    issue_lines.append(f"- {item.get('description', str(item))}")
                else:
                    issue_lines.append(f"- {item}")
            issues_text = "\n".join(issue_lines) if issue_lines else "- Improve robustness and readability"
            prompt = (
                f"Improve this {language} code.\n\n"
                f"Issues to fix:\n{issues_text}\n\n"
                f"Code:\n{code}\n\n"
                "Return ONLY the improved code, no markdown fences, no explanations."
            )
            
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": f"You are an expert {language} programmer. Write clean, efficient, production-ready code. Return ONLY code, no markdown fences, no explanations."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            
            code = response.choices[0].message.content.strip()
            
            # Remove markdown fences if present
            if code.startswith("```"):
                code = re.sub(r"^```[\w]*\n", "", code)
                code = re.sub(r"\n```$", "", code)
            code = self.remove_repeated_output(code)
            return code.strip()
            
        except Exception as e:
            logger.error("groq_generation_failed", error=str(e))
            raise
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

            improved_code = self.improve_code(
                code=message['code'],
                language=message.get('language', 'python'),
                issues=message.get('issues', [])
            )

            state['code']          = improved_code
            state['current_stage'] = 'verifier'
            state['iterations']    = current_iter + 1
            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

            ch.basic_publish(
                exchange='',
                routing_key='verifier',
                body=json.dumps({
                    "request_id":     request_id,
                    "code":           improved_code,
                    "language":       message.get('language', 'python'),
                    "max_iterations": max_iter
                }),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            logger.info("improved_sent_to_verifier",
                        request_id=request_id, iteration=current_iter + 1)

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            try:
                raw   = self.redis.get(f"workflow:{request_id}")
                state = json.loads(raw) if raw else {}
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [f"Improver error: {str(e)}"]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            except Exception:
                pass
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("improver_agent_starting")
        reconnect_on_failure(self, ImproverAgent.setup_channel)


if __name__ == "__main__":
    agent = ImproverAgent()
    agent.start()
