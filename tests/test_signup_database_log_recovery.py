"""Handwritten SQLAlchemy/HTTP fixtures only. Unsafe captured logs never propagate to pytest/stdout."""
import io
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy import create_engine

from mcp_tools.tools.unit_runner import _new_test_password


@pytest.mark.parametrize('fixed,hide_parameters', [(False, False), (True, False), (False, True)])
def test_database_error_handling_and_hidden_binds_prevent_hash_in_crash_log(fixed, hide_parameters):
    from argon2 import PasswordHasher
    # Only runtime synthetic data; never load real user/application credentials.
    encoded = PasswordHasher().hash(_new_test_password())
    stream = io.StringIO()
    logger = logging.Logger('isolated-trusted-log-fixture')
    logger.propagate = False
    logger.addHandler(logging.StreamHandler(stream))
    app = FastAPI()
    engine = create_engine('sqlite://', hide_parameters=hide_parameters)
    rolled_back = []

    def commit():
        raise OperationalError('INSERT INTO users(password_hash) VALUES (:value)', {'value': encoded},
                               RuntimeError('trusted storage fault'), hide_parameters=engine.hide_parameters)

    @app.post('/api/auth/signup')
    def signup():
        try:
            commit()
        except IntegrityError:
            rolled_back.append(True)
            raise HTTPException(status_code=409, detail='DUPLICATE')
        except SQLAlchemyError:
            if not fixed:
                raise
            rolled_back.append(True)
            logger.error('STORAGE_OPERATION_FAILED')
            raise HTTPException(status_code=500, detail='STORAGE_OPERATION_FAILED')

    # Model a server exception boundary: an uncaught DB exception can render binds in its traceback.
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            response = client.post('/api/auth/signup')
            assert response.status_code == 500
    except OperationalError:
        logger.exception('SERVER_ERROR')
    leaked = encoded in stream.getvalue()
    assert leaked is (not fixed and not hide_parameters)
    assert bool(rolled_back) is fixed
    engine.dispose()
    # Suppressing binds is additional protection; only the local handler provides rollback/safe HTTP behavior.
