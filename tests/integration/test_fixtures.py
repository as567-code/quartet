import pathlib

import pytest

from tests.conftest import list_test_databases

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_fresh_db_dropped_when_test_fails(pytester, admin_url):  # Review Focus 2
    before = set(list_test_databases(admin_url))
    pytester.syspathinsert(REPO_ROOT)
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile(
        "import pytest\n"
        "@pytest.mark.integration\n"
        "def test_boom(fresh_db):\n"
        "    raise RuntimeError('boom')\n"
    )
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    assert set(list_test_databases(admin_url)) == before


def test_migrated_db_has_roles_and_alembic_config(migrated_db, alembic_config):
    assert migrated_db.name.startswith("quartet_test_")
    cfg = alembic_config(migrated_db.migrator_url)
    assert cfg.attributes["url"] == migrated_db.migrator_url
    assert "://" not in repr(migrated_db)  # URLs carry passwords: never in repr
