import pika
import json
import os
import re
import structlog
import redis
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


class CodeWriterAgent:
    def __init__(self):
        self.redis      = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection = None
        self.channel    = None
        logger.info("code_writer_agent_created", mode="groq")

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='code_writer', durable=True)
        self.channel.queue_declare(queue='verifier',    durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='code_writer', on_message_callback=self.callback)
        logger.info("code_writer_channel_ready")

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

    def generate_code(self, prompt: str, language: str) -> str:
        """Generate code using Groq API."""
        try:
            from groq import Groq
            
            if not GROQ_API_KEY:
                raise Exception("GROQ_API_KEY not set in environment")
            
            client = Groq(api_key=GROQ_API_KEY)
            
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": f"You are an expert {language} programmer. Write clean, efficient, production-ready code. Return ONLY the code, no markdown fences, no explanations."
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
            logger.info("code_generated_via_groq", length=len(code))
            return code.strip()
            
        except Exception as e:
            logger.error("groq_generation_failed", error=str(e))
            raise

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            prompt     = message['prompt']
            language   = message.get('language', 'python')
            
            logger.info("generating_code", request_id=request_id)

            # Generate code (ignoring requirements for now - Groq handles it via prompt)
            code = self.generate_code(prompt=prompt, language=language)

            # Update workflow state
            raw   = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}
            
            state['code']          = code
            state['current_stage'] = 'verifier'
            state['iterations']    = state.get('iterations', 0) + 1
            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

            # Send to verifier
            self.channel.basic_publish(
                exchange='',
                routing_key='verifier',
                body=json.dumps({
                    "request_id":     request_id,
                    "code":           code,
                    "language":       language,
                    "max_iterations": message.get('max_iterations', 5)
                }),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            
            logger.info("code_generated_sent_to_verifier", request_id=request_id)
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            
            # Mark as failed
            try:
                raw   = self.redis.get(f"workflow:{request_id}")
                state = json.loads(raw) if raw else {}
                state['current_stage'] = 'failed'
                state['errors']        = state.get('errors', []) + [str(e)]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            except:
                pass
            
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("code_writer_agent_starting")
        reconnect_on_failure(self, CodeWriterAgent.setup_channel)


if __name__ == "__main__":
    agent = CodeWriterAgent()
    agent.start()
