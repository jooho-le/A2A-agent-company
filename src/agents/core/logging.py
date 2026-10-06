"""Sanitize SDK protobuf logging before it becomes opaque key/value text."""

import logging

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtoMessage

from orchestrator.core.logging import SecretRedactionFilter, configure_logging
from orchestrator.core.security import redact_data


def _structured_argument(value: object) -> object:
    if isinstance(value, ProtoMessage):
        return redact_data(MessageToDict(value))
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: SDK error details omitted"
    if isinstance(value, tuple):
        return tuple(_structured_argument(item) for item in value)
    if isinstance(value, list):
        return [_structured_argument(item) for item in value]
    if isinstance(value, dict):
        return redact_data({key: _structured_argument(item) for key, item in value.items()})
    return value


class AgentSDKRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "a2a" or record.name.startswith("a2a."):
            record.msg = _structured_argument(record.msg)
            record.args = _structured_argument(record.args)
            # SDK exception traces may contain model/provider input that cannot
            # be identified by a secret field name. Retain type, not raw text.
            if record.exc_info:
                record.exc_text = f"{record.exc_info[0].__name__}: SDK error details omitted"
                record.exc_info = None
            # Run the normal filter immediately after protobuf conversion, even
            # for hosts that installed their own log handler.
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
