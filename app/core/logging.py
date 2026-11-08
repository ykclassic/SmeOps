import logging


def configure_http_client_logging() -> None:
    """Prevent HTTP client request URLs from leaking credentials into application logs.

    Telegram Bot API credentials are embedded in request paths. HTTPX logs full
    request URLs at INFO, so both HTTPX and HTTP Core must remain WARNING+.
    """
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
