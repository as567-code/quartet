import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from quartet.db.session import make_engine


def test_engine_errors_do_not_log_password(caplog):
    eng = make_engine(SecretStr("postgresql+psycopg://u:hunter2@127.0.0.1:1/q"))
    with caplog.at_level("DEBUG"), pytest.raises(sa.exc.OperationalError) as ei:
        eng.connect()
    assert "hunter2" not in caplog.text and "hunter2" not in str(ei.value)
