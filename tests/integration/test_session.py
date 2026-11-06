import pytest
import sqlalchemy as sa

from quartet.db.session import session_scope

pytestmark = pytest.mark.integration


def test_session_scope_rolls_back_on_error(app_engine):
    with pytest.raises(RuntimeError), session_scope(app_engine) as s:
        s.execute(sa.text("SELECT 1"))
        raise RuntimeError


def test_session_scope_commits_on_success(app_engine):
    with session_scope(app_engine) as s:
        assert s.execute(sa.text("SELECT 1")).scalar_one() == 1


def test_app_session_fixture_connects_as_app_role(app_session):
    assert app_session.execute(sa.text("SELECT current_user")).scalar_one() == "quartet_app"
