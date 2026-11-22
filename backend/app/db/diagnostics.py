import logging

from pymongo.errors import ConfigurationError, ConnectionFailure, OperationFailure, PyMongoError


def log_mongo_failure(logger: logging.Logger, operation: str, error: PyMongoError) -> None:
    code = getattr(error, "code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    category = "database"
    if code == 18:
        category = "authentication"
    elif code == 13:
        category = "authorization"
    elif isinstance(error, ConfigurationError):
        category = "configuration"
    elif isinstance(error, ConnectionFailure):
        category = "network_or_server_selection"
    elif isinstance(error, OperationFailure):
        category = "operation"
    # Never format exception text, details, connection strings or tracebacks.
    logger.error(
        "MongoDB failure operation=%s category=%s exception=%s code=%s",
        operation,
        category,
        type(error).__name__,
        code,
    )
