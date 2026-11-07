import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "db_bootstrap.py"
spec = importlib.util.spec_from_file_location("db_bootstrap", SCRIPT)
assert spec is not None and spec.loader is not None
db_bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db_bootstrap)


def test_main_rejects_unvalidated_admin_url(monkeypatch, capsys):
    monkeypatch.setenv("QUARTET_ADMIN_DATABASE_URL", "postgresql://u:p4ssw0rd@h/db?passfile=x")
    monkeypatch.setenv("QUARTET_MIGRATOR_PASSWORD", "m")
    monkeypatch.setenv("QUARTET_APP_PASSWORD", "a")

    def boom(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(db_bootstrap, "bootstrap", boom)
    assert db_bootstrap.main([]) == 2
    err = capsys.readouterr().err
    assert "passfile" in err and "p4ssw0rd" not in err


def test_main_strips_driver_and_bootstraps(monkeypatch):
    monkeypatch.setenv("QUARTET_ADMIN_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("QUARTET_MIGRATOR_PASSWORD", "m")
    monkeypatch.setenv("QUARTET_APP_PASSWORD", "a")
    seen = []
    monkeypatch.setattr(db_bootstrap, "bootstrap", lambda *a: seen.append(a))
    assert db_bootstrap.main([]) == 0
    assert seen == [("postgresql://u:p@h/db", "m", "a")]
