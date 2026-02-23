import pika
import time
import structlog

logger = structlog.get_logger()

def connect_rabbitmq(url: str, retries: int = 15, delay: int = 5):
    for attempt in range(retries):
        try:
            params = pika.URLParameters(url)
            params.heartbeat = 0
            params.blocked_connection_timeout = 300
            connection = pika.BlockingConnection(params)
            logger.info("rabbitmq_connected", attempt=attempt + 1)
            return connection
        except Exception as e:
            logger.warning("rabbitmq_connection_failed",
                           attempt=attempt + 1, error=str(e))
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise Exception(f"RabbitMQ unavailable after {retries} attempts")

def reconnect_on_failure(agent_instance, setup_func):
    while True:
        try:
            setup_func(agent_instance)
            logger.info("starting_consumer")
            agent_instance.channel.start_consuming()
        except pika.exceptions.ConnectionClosedByBroker as e:
            logger.warning("connection_closed_by_broker", error=str(e))
            time.sleep(5)
        except pika.exceptions.AMQPConnectionError as e:
            logger.warning("amqp_connection_error", error=str(e))
            time.sleep(5)
        except KeyboardInterrupt:
            try:
                agent_instance.connection.close()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error("unexpected_error", error=str(e))
            time.sleep(5)