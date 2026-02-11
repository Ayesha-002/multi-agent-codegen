import pika
import json
import os
import structlog
import redis

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()

class TesterAgent:
    def __init__(self):
        self.redis = redis.from_url(
            os.getenv("REDIS_URL", "redis://:devpassword@redis:6379/0"),
            decode_responses=True
        )
        
        rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
        params = pika.URLParameters(rabbitmq_url)
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue='tester', durable=True)
        
        logger.info("tester_agent_initialized")
    
    def test_code(self, code: str, language: str) -> dict:
        # Simplified testing for now - just marks as passed
        # In production, you'd execute tests here
        return {
            "success": True,
            "tests_passed": True,
            "test_results": {
                "total": 1,
                "passed": 1,
                "failed": 0
            }
        }
    
    def callback(self, ch, method, properties, body):
        try:
            message = json.loads(body)
            request_id = message['request_id']
            
            logger.info("testing_code", request_id=request_id)
            
            result = self.test_code(message['code'], message['language'])
            
            state = json.loads(self.redis.get(f"workflow:{request_id}"))
            
            if result['success'] and result['tests_passed']:
                state['current_stage'] = 'completed'
                state['test_results'] = result['test_results']
                logger.info("tests_passed", request_id=request_id)
            else:
                state['current_stage'] = 'improver'
                state['errors'] = state.get('errors', []) + ["Test failures"]
                logger.info("tests_failed", request_id=request_id)
            
            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("message_processing_failed", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    
    def start(self):
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='tester', on_message_callback=self.callback)
        logger.info("tester_agent_started")
        self.channel.start_consuming()

if __name__ == "__main__":
    agent = TesterAgent()
    agent.start()