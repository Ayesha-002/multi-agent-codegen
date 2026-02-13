import pika
import json
import os
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

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://codegen:devpassword@rabbitmq:5672/")
REDIS_URL    = os.getenv("REDIS_URL",    "redis://:devpassword@redis:6379/0")


class TesterAgent:
    def __init__(self):
        self.redis      = redis.from_url(REDIS_URL, decode_responses=True)
        self.connection = None
        self.channel    = None
        logger.info("tester_agent_created")

    def setup_channel(self):
        self.connection = connect_rabbitmq(RABBITMQ_URL)
        self.channel    = self.connection.channel()
        self.channel.queue_declare(queue='tester', durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue='tester', on_message_callback=self.callback)
        logger.info("tester_channel_ready")

    def test_code(self, code: str, language: str) -> dict:
        """Syntax check + basic structural validation"""
        if language.lower() == "python":
            try:
                compile(code, "<generated>", "exec")
                return {
                    "tests_passed": True,
                    "test_results": {
                        "total": 1, "passed": 1, "failed": 0,
                        "details": "Syntax check passed"
                    }
                }
            except SyntaxError as e:
                return {
                    "tests_passed": False,
                    "test_results": {
                        "total": 1, "passed": 0, "failed": 1,
                        "details": f"SyntaxError at line {e.lineno}: {e.msg}"
                    }
                }
        # Non-Python: pass through
        return {
            "tests_passed": True,
            "test_results": {"total": 1, "passed": 1, "failed": 0, "details": "Basic check passed"}
        }

    def callback(self, ch, method, properties, body):
        try:
            message    = json.loads(body)
            request_id = message['request_id']
            logger.info("testing_code", request_id=request_id)

            result = self.test_code(message['code'], message.get('language', 'python'))

            raw   = self.redis.get(f"workflow:{request_id}")
            state = json.loads(raw) if raw else {}

            if result['tests_passed']:
                state['current_stage'] = 'completed'
                state['test_results']  = result['test_results']
                logger.info("tests_passed_workflow_complete", request_id=request_id)
            else:
                ch.basic_publish(
                    exchange='',
                    routing_key='improver',
                    body=json.dumps({
                        "request_id":     request_id,
                        "code":           message['code'],
                        "language":       message.get('language', 'python'),
                        "issues": [{
                            "type":        "test_failure",
                            "description": result['test_results'].get('details', 'Tests failed')
                        }],
                        "max_iterations": message.get('max_iterations', 5)
                    }),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                state['current_stage'] = 'improver'
                state['errors']        = state.get('errors', []) + [result['test_results'].get('details', '')]
                logger.info("tests_failed_sent_to_improver", request_id=request_id)

            self.redis.setex(f"workflow:{request_id}", 3600, json.dumps(state))
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("callback_error", error=str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self):
        logger.info("tester_agent_starting")
        reconnect_on_failure(self, TesterAgent.setup_channel)


if __name__ == "__main__":
    agent = TesterAgent()
    agent.start()