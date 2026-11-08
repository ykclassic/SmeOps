import logging

from app.core.logging import configure_http_client_logging


def test_http_client_info_logs_are_disabled_to_prevent_url_credential_leaks():
    configure_http_client_logging()

    for logger_name in ("httpx", "httpcore"):
        logger = logging.getLogger(logger_name)
        assert logger.getEffectiveLevel() >= logging.WARNING
        assert not logger.isEnabledFor(logging.INFO)
