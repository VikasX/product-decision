import logging

from pymongo.errors import ConfigurationError, ConnectionFailure, OperationFailure, PyMongoError


# Record safe diagnostic categories without raw exception text or connection details.
def log_mongo_failure(logger: logging.Logger, operation: str, error: PyMongoError) -> None:
    # Read a numeric MongoDB error code if one exists; it helps classify the failure safely.
    code = getattr(error, "code", None)
    # Reject unusual non-numeric codes; booleans are also excluded even though Python treats them as integers.
    if not isinstance(code, int) or isinstance(code, bool):
        # Discard an unexpected code value before logging it.
        code = None
    # Start with a broad database category, then narrow it using known codes and exception classes.
    category = "database"
    if code == 18:
        # MongoDB code 18 means the supplied database credentials were rejected.
        category = "authentication"
    elif code == 13:
        # MongoDB code 13 means the user lacks permission for this database operation.
        category = "authorization"
    elif isinstance(error, ConfigurationError):
        # Configuration failures may involve the URI format, DNS options or driver setup.
        category = "configuration"
    elif isinstance(error, ConnectionFailure):
        # Connection failures include unreachable servers or being unable to select an available server.
        category = "network_or_server_selection"
    elif isinstance(error, OperationFailure):
        # Other database-operation failures keep a broad operation category.
        category = "operation"
    # Never format exception text, details, connection strings or tracebacks.
    # Log only the operation label, category, exception class and numeric code.
    logger.error(
        "MongoDB failure operation=%s category=%s exception=%s code=%s",
        operation,
        category,
        type(error).__name__,
        code,
    )
