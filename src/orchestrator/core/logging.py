import logging
import traceback

from orchestrator.core.security import redact_data, redact_text


class SecretRedactionFilter(logging.Filter):
    """Sanitize rendered messages and tracebacks, including preinstalled handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, (dict, list, tuple)):
            record.msg = redact_data(record.msg)
        record.msg = redact_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        # Formatters may print structured extra fields instead of the message.
        protected = {"msg", "args", "exc_info", "exc_text", "stack_info"}
        extras = {key: value for key, value in record.__dict__.items() if key not in protected}
        record.__dict__.update(redact_data(extras))
        return True


def configure_logging(level: str) -> None:
    """Configure the standard-library logger without replacing host handlers."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # basicConfig is a no-op when the hosting application already has handlers.
    # Apply the same filter to root and explicitly configured child handlers.
    loggers = [logging.getLogger()]
    loggers.extend(
        value for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
                handler.addFilter(SecretRedactionFilter())
