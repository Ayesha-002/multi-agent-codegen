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

# Language detection (expanded)
LANG_KEYWORDS = {
    "python":     r"\b(python|py|django|flask|fastapi|pandas|numpy|pytest|pip)\b",
    "javascript": r"\b(javascript|js|node|react|vue|angular|express|npm|typescript|ts)\b",
    "java":       r"\b(java|spring|maven|gradle|junit)\b",
    "go":         r"\b(golang|go\b)\b",
    "rust":       r"\b(rust|cargo)\b",
    "cpp":        r"\b(c\+\+|cpp)\b",
    "c":          r"\b\bc\b(?![\+\#])",
    "csharp":     r"\b(c#|csharp|\.net|asp\.net)\b",
    "php":        r"\b(php|laravel|wordpress)\b",
    "ruby":       r"\b(ruby|rails)\b",
    "swift":      r"\b(swift|ios|swiftui)\b",
    "kotlin":     r"\b(kotlin|android)\b",
}

class SmartAnalyzerAgent:
    def __init__(self):
        self.redis      = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection = None
        self.channel    = None
        logger.info("smart_analyzer_created", mode="conversational")

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='analyzer',    durable=True)
        self.channel.queue_declare(queue='code_writer', durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='analyzer', on_message_callback=self.callback)
        logger.info("analyzer_channel_ready")

    def detect_language(self, text: str) -> str:
        """Detect language from text, default to Python."""
        text_lower = text.lower()
        for lang, pattern in LANG_KEYWORDS.items():
            if re.search(pattern, text_lower, re.IGNORECASE):
                return lang
        return "python"  # Default

    def is_code_submission(self, text: str) -> bool:
        """Detect if user submitted code to improve/fix."""
        # Check for code indicators
        code_indicators = [
            r"def\s+\w+\(",           # Python function
            r"function\s+\w+\(",      # JS function
            r"class\s+\w+",           # Class definition
            r"import\s+\w+",          # Imports
            r"#include\s*<",          # C/C++ include
            r"public\s+class",        # Java class
            r"fn\s+\w+\(",            # Rust function
            r"\w+\s*=\s*function",    # JS function assignment
            r"async\s+def",           # Async function
            r"for\s*\(",              # For loop
            r"while\s*\(",            # While loop
            r"if\s*\(",               # If statement
        ]
        
        for pattern in code_indicators:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        
        # Check if mostly code-like (has semicolons, brackets, etc.)
        code_chars = text.count('{') + text.count('}') + text.count(';') + text.count('(') + text.count(')')
        if code_chars > len(text.split()) * 0.3:  # 30% of words have code chars
            return True
            
        return False

    def extract_user_intent(self, text: str) -> dict:
        """Extract what user wants from natural language."""
        text_lower = text.lower()
        
        # Detect mode: improve existing code vs. generate new code
        if self.is_code_submission(text):
            mode = "improve"
            # Try to extract the code block
            code_match = re.search(r'```[\w]*\n(.*?)```', text, re.DOTALL)
            if code_match:
                code = code_match.group(1)
            else:
                # Assume the whole message is code
                code = text
            
            # Extract improvement request
            improvement_keywords = ["fix", "improve", "optimize", "debug", "refactor", "clean up", "add", "make better"]
            request = "improve and fix this code"
            for keyword in improvement_keywords:
                if keyword in text_lower:
                    request = f"{keyword} this code"
                    break
            
            return {
                "mode": "improve",
                "code": code,
                "request": request
            }
        else:
            # Generate new code mode
            mode = "generate"
            
            # Clean up casual language
            prompt = text.strip()
            
            # Remove conversational filler
            casual_prefixes = [
                r"^(hey|hi|hello|yo)\s*,?\s*",
                r"^(can you|could you|please)\s+",
                r"^(i need|i want|make me|write me|give me|create)\s+",
                r"^(help me|show me)\s+",
            ]
            for prefix in casual_prefixes:
                prompt = re.sub(prefix, "", prompt, flags=re.IGNORECASE)
            
            # Ensure it starts with an action verb
            action_verbs = ["write", "create", "make", "build", "develop", "implement", "generate"]
            has_action = any(prompt.lower().startswith(verb) for verb in action_verbs)
            
            if not has_action:
                prompt = f"Write a program that {prompt}"
            
            return {
                "mode": "generate",
                "prompt": prompt.strip()
            }

    def enrich_prompt(self, intent: dict, language: str) -> str:
        """Convert user intent into a detailed prompt for the writer."""
        if intent["mode"] == "improve":
            return f"""Improve and optimize this {language} code:

{intent['code']}

Requirements:
- Fix any bugs or errors
- Improve code quality and readability
- Add error handling
- Add type hints and docstrings
- Optimize performance where possible
- Follow best practices for {language}

Return ONLY the improved code, no explanations."""

        else:
            # Generate mode
            prompt = intent["prompt"]
            
            # Add language if not mentioned
            if language not in prompt.lower():
                prompt = f"{prompt} in {language.capitalize()}"
            
            # Add standard requirements
            return f"""{prompt}

Requirements:
- Add proper error handling
- Include type hints/annotations
- Add docstrings/comments
- Follow best practices
- Make it production-ready
- Handle edge cases

Write clean, efficient, well-documented code."""

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            user_input = message['prompt']
            user_lang  = message.get('language')

            logger.info("analyzing_input", request_id=request_id, preview=user_input[:100])

            raw   = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}

            # Smart analysis
            intent   = self.extract_user_intent(user_input)
            language = user_lang if user_lang else self.detect_language(user_input)
            enriched = self.enrich_prompt(intent, language)

            logger.info("analysis_complete",
                        request_id=request_id,
                        mode=intent["mode"],
                        language=language)

            # Always send to writer (never ask clarification)
            state['current_stage']   = 'writer'
            state['language']        = language
            state['mode']            = intent["mode"]
            state['original_input']  = user_input
            state['enriched_prompt'] = enriched
            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))

            self.channel.basic_publish(
                exchange='',
                routing_key='code_writer',
                body=json.dumps({
                    "request_id":     request_id,
                    "prompt":         enriched,
                    "language":       language,
                    "mode":           intent.get("mode", "generate"),
                    "requirements":   message.get('requirements', []),
                    "max_iterations": message.get('max_iterations', 5),
                }),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            
            logger.info("sent_to_writer", request_id=request_id, language=language, mode=intent["mode"])

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e), traceback=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("smart_analyzer_starting")
        reconnect_on_failure(self, SmartAnalyzerAgent.setup_channel)


if __name__ == "__main__":
    agent = SmartAnalyzerAgent()
    agent.start()