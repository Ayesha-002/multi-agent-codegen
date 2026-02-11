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

class ImproverAgent:
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
        self.channel.queue_declare(queue='improver', durable=True)
        
        logger.info("improver_agent_initialized")
    
    def improve_code(self, code: str, language: str, issues: list) -> dict:
        issues_text = "\n".join([f"- {issue.get('description', issue)}" for issue in issues])
        
        prompt = f"""Fix the following issues in this {language} code:

Issues:
{issues_text}

Current Code:
```{language}
{code}
```

Provide ONLY the fixed code, no explanations."""

        try:
            response = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            
            improved_code = response.content[0].text
            
            if "```" in improved_code:
                parts = improved_code.split("```")
                if len(parts) >= 2:
                    improved_code = parts[1]
                    if improved_code.startswith(language):
                        improved_code = improved_code[len(language):].strip()
            
            return {"success": True, "code": improved_code.strip()}
        
        except Exception as e:
            logger.error("improvement_failed", error=str(e))
            return {"success": False, "error": str(e)}
    
    def callback(self, ch, method, properties, body):
        try:
            message = json.loads(body)
            request_id = message['request_id']
            
            state = json.loads(self.redis.get(f"workflow:{request_id}"))
            
            if state['iterations'] >= message['max_iterations']:
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + ["Max iterations reached"]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                ch.basic_ack(delivery_tag=method.delivery_tag)
                logger.warning("max_iterations_reached", request_id=request_id)
                return
            
            logger.info("improving_code", request_id=request_id)
            
            result = self.improve_code(
                message['code'],
                message['language'],
                message.get('issues', [])
            )
            
            if result['success']:
                state['code'] = result['code']
                state['current_stage'] = 'verifier'
                state['iterations'] = state.get('iterations', 0) + 1
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
                
                verifier_message = {
                    "request_id": request_id,
                    "code": result['code'],
                    "language": message['language'],
                    "max_iterations": message['max_iterations']
                }
                
                ch.basic_publish(
                    exchange='',
                    routing_key='verifier',
                    body=json.dumps(verifier_message),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                
                logger.info("code_improved", request_id=request_id)
            else:
                state['current_stage'] = 'failed'
                state['errors'] = state.get('errors', []) + [result['error']]
                self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("message_processing_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    
    def start(self):
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='improver', on_message_callback=self.callback)
        logger.info("improver_agent_started")
        self.channel.start_consuming()

if __name__ == "__main__":
    agent = ImproverAgent()
    agent.start()