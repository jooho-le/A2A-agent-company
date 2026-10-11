"""Do not publish SDK payloads, prompts, Source or provider prose into logs."""

import logging

from orchestrator.core.logging import SecretRedactionFilter, configure_logging

_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _exception_types(value: object) -> tuple[str, ...]:
    if isinstance(value, BaseException):
        return (type(value).__name__,)
    if isinstance(value, (tuple, list)):
        return tuple(name for item in value for name in _exception_types(item))
    if isinstance(value, dict):
        return tuple(name for item in value.values() for name in _exception_types(item))
    return ()


class AgentSDKRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in {"a2a", "mcp"} or record.name.startswith(("a2a.", "mcp.")):
            types = _exception_types(record.args)
            record.msg = "Protocol SDK log details omitted [REDACTED]"
            if types:
                record.msg += " (" + ", ".join(sorted(set(types))) + ")"
            record.args = ()
            # SDK exception traces may contain model/provider input that cannot
            # be identified by a secret field name. Retain type, not raw text.
            if record.exc_info:
                record.exc_text = f"{record.exc_info[0].__name__}: SDK error details omitted"
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = "SDK error details omitted [REDACTED]"
            if record.stack_info:
                record.stack_info = "SDK stack details omitted [REDACTED]"
            # Custom formatters may expose SDK bodies via extra fields. Actual
            # Task/context IDs and diagnostic events live in canonical Trace.
            for key in set(record.__dict__) - _STANDARD_FIELDS - {"exc_text", "message"}:
                record.__dict__[key] = "[REDACTED]"
            record.__dict__.pop("message", None)
            SecretRedactionFilter().filter(record)
        return True


def configure_agent_logging(level: str) -> None:
    configure_logging(level)
    loggers = [logging.getLogger()]
    loggers.extend(
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, AgentSDKRedactionFilter) for item in handler.filters):
                # Must precede the existing filter's rendered getMessage().
                handler.filters.insert(0, AgentSDKRedactionFilter())
