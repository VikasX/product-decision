import logging

import pytest
from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError

from app.db.diagnostics import log_mongo_failure


@pytest.mark.parametrize(
    "error, category",
    [
        (OperationFailure("private URI and credentials", code=18), "authentication"),
        (OperationFailure("private URI and credentials", code=13), "authorization"),
        (ConfigurationError("private URI and credentials"), "configuration"),
        (ServerSelectionTimeoutError("private URI and credentials"), "network_or_server_selection"),
    ],
)
def test_mongo_diagnostics_log_only_safe_metadata(error, category, caplog):
    log_mongo_failure(logging.getLogger("mongo_test"), "health_ping", error)
    assert f"category={category}" in caplog.text
    assert f"exception={type(error).__name__}" in caplog.text
    assert "private URI and credentials" not in caplog.text
    assert "Traceback" not in caplog.text
