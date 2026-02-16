import pika
import json
import os
import re
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

# Languages we support
SUPPORTED_LANGUAGES = [
    "python", "javascript", "typescript", "java", "go",
    "rust", "c", "cpp", "csharp", "php", "ruby", "swift",
    "kotlin", "sql", "bash", "html", "css"
]


class PromptAnalyzerAgent:
    def __init__(self):
        self.ollama_host = OLLAMA_HOST
        self.model       = MODEL_NAME
        self.redis       = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection  = None
        self.channel     = None
        logger.info("analyzer_agent_created", model=self.model)

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='analyzer',     durable=True)
        self.channel.queue_declare(queue='code_writer',  durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(
            queue='analyzer', on_message_callback=self.callback)
        logger.info("analyzer_channel_ready")

    # ------------------------------------------------------------------ #
    #  Quick rule-based language detection (no LLM needed)
    # ------------------------------------------------------------------ #
    def detect_language_from_prompt(self, prompt: str) -> str | None:
        prompt_lower = prompt.lower()
        for lang in SUPPORTED_LANGUAGES:
            # Match whole word only
            if re.search(rf'\b{lang}\b', prompt_lower):
                return lang
        # Common aliases
        aliases = {
            "js": "javascript", "ts": "typescript",
            "node": "javascript", "nodejs": "javascript",
            "c++": "cpp", "c#": "csharp", ".net": "csharp",
            "golang": "go", "shell": "bash"
        }
        for alias, lang in aliases.items():
            if alias in prompt_lower:
                return lang
        return None

    # ------------------------------------------------------------------ #
    #  LLM-based prompt analysis
    # ------------------------------------------------------------------ #
    def analyze_prompt(self, prompt: str, language: str | None) -> dict:
        lang_instruction = (
            f'Language is already specified as "{language}".'
            if language
            else "Language is NOT specified."
        )

        analysis_prompt = f"""You are a software requirements analyst. Analyze this coding request and determine if it has enough information to write good code.

User request: "{prompt}"

{lang_instruction}

Evaluate these aspects:
1. Is the main task/goal clear?
2. Is the programming language clear or can it be reasonably inferred?
3. Are there any critical missing details that would prevent writing good code?

Respond ONLY with valid JSON:
{{
    "is_clear": true,
    "confidence": 0.95,
    "inferred_language": "python",
    "enriched_prompt": "Write a Python function that...",
    "missing_info": [],
    "questions": [],
    "requirements": ["requirement 1", "requirement 2"]
}}

Rules:
- is_clear: true if you can write good code without asking anything
- confidence: 0.0 to 1.0 how confident you are
- inferred_language: best guess at language even if not specified (use "python" as default if truly unclear)
- enriched_prompt: rewrite the prompt to be more precise and detailed
- missing_info: list critical things missing (empty if clear)
- questions: list questions to ask user ONLY if is_clear=false (max 3 questions)
- requirements: list of specific technical requirements you inferred

Be LIBERAL about marking is_clear=true. Only ask questions if truly needed.
Examples of clear enough: "write sorting function", "make a calculator", "REST API for users"
Examples that need clarification: "write code" (no task), "fix this" (no code provided)"""

        try:
            response = requests.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": analysis_prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 1024}
                },
                timeout=120
            )

            if response.status_code != 200:
                return self._default_analysis(prompt, language)

            raw = response.json()['message']['content'].strip()

            # Extract JSON
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            start = raw.find('{')
            end   = raw.rfind('}') + 1
            if start != -1 and end > start:
                raw = raw[start:end]

            result = json.loads(raw)

            # Ensure all required fields exist
            result.setdefault("is_clear",         True)
            result.setdefault("confidence",        0.8)
            result.setdefault("inferred_language", language or "python")
            result.setdefault("enriched_prompt",   prompt)
            result.setdefault("missing_info",      [])
            result.setdefault("questions",         [])
            result.setdefault("requirements",      [])

            # Override language if user explicitly provided one
            if language:
                result["inferred_language"] = language

            return result

        except Exception as e:
            logger.error("analysis_failed", error=str(e))
            return self._default_analysis(prompt, language)

    def _default_analysis(self, prompt: str, language: str | None) -> dict:
        """Fallback when LLM fails - assume clear, use python"""
        return {
            "is_clear":         True,
            "confidence":       0.7,
            "inferred_language": language or "python",
            "enriched_prompt":  prompt,
            "missing_info":     [],
            "questions":        [],
            "requirements":     []
        }

    # ------------------------------------------------------------------ #
    #  Message handler
    # ------------------------------------------------------------------ #
    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            prompt     = message['prompt']
            # Language from user (may be None or default)
            user_lang  = message.get('language')
            # Treat "python" as unspecified if not in original prompt
            # (coordinator defaults to python, but user may not have said it)
            explicit_lang = message.get('explicit_language', False)
            language = user_lang if explicit_lang else self.detect_language_from_prompt(prompt)

            logger.info("analyzing_prompt", request_id=request_id,
                        prompt_preview=prompt[:80])

            raw   = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}

            # Check if user already answered clarification questions
            user_answers = message.get('user_answers', {})

            analysis = self.analyze_prompt(prompt, language)

            logger.info("analysis_complete",
                        request_id=request_id,
                        is_clear=analysis['is_clear'],
                        language=analysis['inferred_language'],
                        confidence=analysis['confidence'])

            if analysis['is_clear'] or user_answers:
                # ✅ Enough info — enrich prompt and send to writer
                final_prompt = analysis['enriched_prompt']

                # If user answered questions, append answers to prompt
                if user_answers:
                    answers_text = "\n".join(
                        [f"- {q}: {a}" for q, a in user_answers.items()]
                    )
                    final_prompt = f"{final_prompt}\n\nAdditional clarifications:\n{answers_text}"

                state['current_stage']    = 'writer'
                state['language']         = analysis['inferred_language']
                state['original_prompt']  = prompt
                state['enriched_prompt']  = final_prompt
                state['requirements']     = analysis.get('requirements', [])
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

                # Merge user requirements with inferred ones
                all_requirements = list(set(
                    message.get('requirements', []) +
                    analysis.get('requirements', [])
                ))

                self.channel.basic_publish(
                    exchange='',
                    routing_key='code_writer',
                    body=json.dumps({
                        "request_id":     request_id,
                        "prompt":         final_prompt,
                        "language":       analysis['inferred_language'],
                        "requirements":   all_requirements,
                        "max_iterations": message.get('max_iterations', 5)
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                logger.info("sent_to_writer",
                            request_id=request_id,
                            language=analysis['inferred_language'])
            else:
                # ❓ Need clarification — store questions, wait for user
                state['current_stage'] = 'needs_clarification'
                state['questions']     = analysis['questions']
                state['missing_info']  = analysis['missing_info']
                state['analysis']      = analysis
                state['original_message'] = message
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

                logger.info("needs_clarification",
                            request_id=request_id,
                            questions=analysis['questions'])

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("analyzer_agent_starting")
        reconnect_on_failure(self, PromptAnalyzerAgent.setup_channel)


if __name__ == "__main__":
    agent = PromptAnalyzerAgent()
    agent.start()