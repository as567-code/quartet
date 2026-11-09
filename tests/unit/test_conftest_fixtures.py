"""The shared ``admin_url`` fixture fails cleanly (no traceback) on a bad env file."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_admin_url_fails_cleanly_on_insecure_env_file(pytester, monkeypatch, tmp_path):
    env = tmp_path / "insecure.env"
    env.write_text("QUARTET_ADMIN_DATABASE_URL=postgresql://u:hunter2@h/db\n")
    env.chmod(0o666)
    monkeypatch.setenv("QUARTET_ENV_FILE", str(env))
    pytester.syspathinsert(REPO_ROOT)
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile("def test_needs_db(admin_url):\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    out = result.stdout.str()
    result.stdout.fnmatch_lines([f"env file {env} must not be group- or world-writable"])
    for unwanted in ("InsecureEnvFile", "Traceback", "During handling", "hunter2"):
        assert unwanted not in out, unwanted


def test_admin_url_fails_when_settings_invalid(pytester, monkeypatch, tmp_path):
    env = tmp_path / "bad.env"
    env.write_text("QUARTET_DATABASE_URL=not-a-url\n")
    env.chmod(0o600)
    monkeypatch.setenv("QUARTET_ENV_FILE", str(env))
    monkeypatch.delenv("QUARTET_ADMIN_DATABASE_URL", raising=False)
    pytester.syspathinsert(REPO_ROOT)
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile("def test_needs_db(admin_url):\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    out = result.stdout.str()
    assert "database_url" in out.lower()  # the field is reported by its env alias
    assert "not-a-url" not in out
    assert "During handling" not in out


def test_admin_url_still_skips_when_nothing_configured(pytester, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.quartet/.env
    for var in ("QUARTET_ENV_FILE", "QUARTET_ADMIN_DATABASE_URL", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(var, raising=False)
    pytester.syspathinsert(REPO_ROOT)
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile("def test_needs_db(admin_url):\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(skipped=1)
