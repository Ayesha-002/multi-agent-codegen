import pika
import time
import structlog
import threading

logger = structlog.get_logger()

def connect_rabbitmq(url: str, retries: int = 15, delay: int = 5):
    """
    Connect to RabbitMQ with retry logic.
    heartbeat=0 disables heartbeat timeout - prevents drops during long LLM calls.
    """
    for attempt in range(retries):
        try:
            params = pika.URLParameters(url)
            params.heartbeat = 0                      # Disable heartbeat - fixes timeout during LLM calls
            params.blocked_connection_timeout = 300   # 5 min blocked timeout
            params.connection_attempts = 3
            params.retry_delay = 2
            params.socket_timeout = 10

            connection = pika.BlockingConnection(params)
            logger.info("rabbitmq_connected", attempt=attempt + 1)
            return connection

        except Exception as e:
            logger.warning(
                "rabbitmq_connection_failed",
                attempt=attempt + 1,
                max_retries=retries,
                error=str(e),
                retry_in=delay
            )
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise Exception(f"Could not connect to RabbitMQ after {retries} attempts: {e}")


def reconnect_on_failure(agent_instance, setup_func):
    """
    Wrapper to auto-reconnect if connection drops during consuming.
    Calls setup_func(agent_instance) to re-initialize channel after reconnect.
    """
    while True:
        try:
            setup_func(agent_instance)
            logger.info("starting_consumer")
            agent_instance.channel.start_consuming()
        except pika.exceptions.ConnectionClosedByBroker as e:
            logger.warning("connection_closed_by_broker", error=str(e), reconnecting=True)
            time.sleep(5)
        except pika.exceptions.AMQPChannelError as e:
            logger.error("amqp_channel_error", error=str(e))
            time.sleep(5)
        except pika.exceptions.AMQPConnectionError as e:
            logger.warning("amqp_connection_error", error=str(e), reconnecting=True)
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("agent_stopped")
            try:
                agent_instance.connection.close()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error("unexpected_error", error=str(e), reconnecting=True)
            time.sleep(5)