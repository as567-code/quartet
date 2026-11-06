import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from quartet.db.session import make_engine, session_scope

pytestmark = pytest.mark.integration


def test_engine_errors_do_not_log_password(caplog):
    eng = make_engine(SecretStr("postgresql+psycopg://u:hunter2@127.0.0.1:1/q"))
    with caplog.at_level("DEBUG"), pytest.raises(sa.exc.OperationalError) as ei:
        eng.connect()
    assert "hunter2" not in caplog.text and "hunter2" not in str(ei.value)


def test_session_scope_rolls_back_on_error(app_engine):
    with pytest.raises(RuntimeError), session_scope(app_engine) as s:
        s.execute(sa.text("SELECT 1"))
        raise RuntimeError


def test_session_scope_commits_on_success(app_engine):
    with session_scope(app_engine) as s:
        assert s.execute(sa.text("SELECT 1")).scalar_one() == 1


def test_app_session_fixture_connects_as_app_role(app_session):
    assert app_session.execute(sa.text("SELECT current_user")).scalar_one() == "quartet_app"
