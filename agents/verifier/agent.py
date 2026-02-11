import pika
import json
import os
import structlog
import redis
from anthropic import Anthropic

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

class VerifierAgent:
    def __init__(self):
        self.anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        
        self.redis = redis.from_url(
            os.getenv("REDIS_URL", "redis://:devpassword@redis:6379/0"),
            decode_responses=True
        )
        
        rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
        params = pika.URLParameters(rabbitmq_url)
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue='verifier', durable=True)
        
        logger.info("verifier_agent_initialized")
    
    def verify_code(self, code: str, language: str) -> dict:
        prompt = f"""Analyze this {language} code for:
1. Syntax errors
2. Logic issues
3. Security vulnerabilities
4. Best practice violations

Code:
```{language}
{code}
```

Respond in JSON format:
{{
    "has_errors": true/false,
    "issues": [
        {{"type": "syntax/logic/security", "description": "...", "line": 5}}
    ],
    "severity": "critical/high/medium/low/none"
}}"""

        try:
            response = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            )
            
            result_text = response.content[0].text
            
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()
            
            result = json.loads(result_text)
            return {"success": True, "verification": result}
        
        except Exception as e:
            logger.error("verification_failed", error=str(e))
            return {"success": False, "error": str(e)}
    
    def callback(self, ch, method, properties, body):
        try:
            message = json.loads(body)
            request_id = message['request_id']
            
            logger.info("verifying_code", request_id=request_id)
            
            result = self.verify_code(message['code'], message['language'])
            
            if result['success']:
                verification = result['verification']
                
                if not verification.get('has_errors', False) or verification.get('severity') in ['low', 'none']:
                    # Pass to tester
                    tester_message = {
                        "request_id": request_id,
                        "code": message['code'],
                        "language": message['language'],
                        "max_iterations": message['max_iterations']
                    }
                    
                    ch.basic_publish(
                        exchange='',
                        routing_key='tester',
                        body=json.dumps(tester_message),
                        properties=pika.BasicProperties(delivery_mode=2)
                    )
                    
                    state = json.loads(self.redis.get(f"workflow:{request_id}"))
                    state['current_stage'] = 'tester'
                    self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                    
                    logger.info("verification_passed", request_id=request_id)
                else:
                    # Send to improver
                    improver_message = {
                        "request_id": request_id,
                        "code": message['code'],
                        "language": message['language'],
                        "issues": verification.get('issues', []),
                        "max_iterations": message['max_iterations']
                    }
                    
                    ch.basic_publish(
                        exchange='',
                        routing_key='improver',
                        body=json.dumps(improver_message),
                        properties=pika.BasicProperties(delivery_mode=2)
                    )
                    
                    state = json.loads(self.redis.get(f"workflow:{request_id}"))
                    state['current_stage'] = 'improver'
                    state['errors'] = state.get('errors', []) + [f"Verification issues: {verification.get('severity')}"]
                    self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                    
                    logger.info("verification_failed_sent_to_improver", request_id=request_id)
            
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("message_processing_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    
    def start(self):
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='verifier', on_message_callback=self.callback)
        logger.info("verifier_agent_started")
        self.channel.start_consuming()

if __name__ == "__main__":
    agent = VerifierAgent()
    agent.start()