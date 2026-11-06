from pathlib import Path

import pytest
from pydantic import ValidationError

from quartet.config import InsecureEnvFile, InvalidSettings, Settings, load_settings


def test_secrets_are_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@localhost:5432/q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abc")
    s = load_settings()
    assert "hunter2" not in repr(s) and "sk-ant-test-abc" not in repr(s)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test-abc"


def test_defaults(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/q")
    s = load_settings()
    assert s.admin_database_url is None
    assert s.anthropic_api_key is None and s.openai_api_key is None
    assert s.otlp_endpoint == "http://127.0.0.1:4318"
    assert s.home == Path("~/.quartet").expanduser()


DB = "postgresql+psycopg://u:hunter2@localhost:5432/q"


def _env_file(path: Path, body: str, mode: int = 0o600) -> Path:
    path.write_text(body)
    path.chmod(mode)
    return path


def test_cwd_dotenv_is_ignored(tmp_path):
    (tmp_path / ".env").write_text(f"QUARTET_DATABASE_URL={DB}\n")
    with pytest.raises(InvalidSettings, match="QUARTET_DATABASE_URL"):
        load_settings()


def test_env_file_var_is_honoured(monkeypatch, tmp_path):
    f = _env_file(tmp_path / "custom.env", f"QUARTET_DATABASE_URL={DB}\n")
    monkeypatch.setenv("QUARTET_ENV_FILE", str(f))
    assert load_settings().database_url.get_secret_value() == DB


def test_explicit_arg_beats_env_var(monkeypatch, tmp_path):
    a = _env_file(tmp_path / "a.env", "QUARTET_DATABASE_URL=postgresql://a@h/a\n")
    b = _env_file(tmp_path / "b.env", "QUARTET_DATABASE_URL=postgresql://b@h/b\n")
    monkeypatch.setenv("QUARTET_ENV_FILE", str(b))
    assert load_settings(a).database_url.get_secret_value() == "postgresql://a@h/a"


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_writable_env_file_is_rejected(tmp_path, mode):
    f = _env_file(tmp_path / "x.env", f"QUARTET_DATABASE_URL={DB}\n", mode)
    with pytest.raises(InsecureEnvFile) as ei:
        load_settings(f)
    assert str(f) in str(ei.value) and "hunter2" not in str(ei.value)


def test_missing_explicit_env_file_is_error(tmp_path):
    with pytest.raises(InsecureEnvFile):
        load_settings(tmp_path / "nope.env")


def test_missing_env_file_var_target_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARTET_ENV_FILE", str(tmp_path / "nope.env"))
    with pytest.raises(InsecureEnvFile):
        load_settings()


def test_default_home_env_file_is_picked_up(tmp_path):
    d = tmp_path / "home" / ".quartet"
    d.mkdir()
    _env_file(d / ".env", f"QUARTET_DATABASE_URL={DB}\n")
    assert load_settings().database_url.get_secret_value() == DB


def test_directory_env_file_is_rejected(tmp_path):
    with pytest.raises(InsecureEnvFile):
        load_settings(tmp_path)


def test_db_url_with_passfile_is_rejected_without_leaking(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", DB + "?passfile=x&host=evil")
    with pytest.raises(InvalidSettings) as ei:
        load_settings()
    assert "hunter2" not in str(ei.value) and "evil" not in str(ei.value)
    assert "QUARTET_DATABASE_URL" in str(ei.value)


def test_admin_db_url_is_validated_too(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", DB)
    monkeypatch.setenv("QUARTET_ADMIN_DATABASE_URL", "mysql://u:hunter2@h/q")
    with pytest.raises(InvalidSettings) as ei:
        load_settings()
    assert "QUARTET_ADMIN_DATABASE_URL" in str(ei.value) and "hunter2" not in str(ei.value)


def test_non_postgres_scheme_is_rejected(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "mysql://u:hunter2@localhost/q")
    with pytest.raises(InvalidSettings) as ei:
        load_settings()
    assert "hunter2" not in str(ei.value)


def test_sslmode_is_accepted(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", DB + "?sslmode=require&connect_timeout=3")
    assert (
        load_settings()
        .database_url.get_secret_value()
        .endswith("sslmode=require&connect_timeout=3")
    )


def test_otlp_endpoint_allowlist(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", DB)
    monkeypatch.setenv("OTLP_ENDPOINT", "http://evil.example:4318")
    with pytest.raises(InvalidSettings, match="otlp_endpoint"):
        load_settings()
    monkeypatch.setenv("OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert load_settings().otlp_endpoint == "http://127.0.0.1:4318"


@pytest.mark.parametrize(
    "suffix",
    [
        "#?passfile=x",
        "?passfile=x#",
        " x",
        "\n",
        "\x01",
        "\t",
        "?%70assfile=x",
        "?options=-c%20x",
        "?service=s",
        "?hostaddr=1.2.3.4",
    ],
)
def test_db_url_bypass_attempts_are_rejected(monkeypatch, suffix):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql://u:hunter2@h/db" + suffix)
    with pytest.raises(InvalidSettings) as ei:
        load_settings()
    assert "hunter2" not in str(ei.value)


def test_db_url_host_query_param_is_accepted_because_libpq_yields_allowed_key(monkeypatch):
    # libpq parses `?host=evil` to the allowed key `host` (host is legitimately part of a DB URL),
    # so the allowlist cannot reject it; the credential-exfil vectors are passfile/service/etc.
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql://u:hunter2@h/db?host=evil")
    assert load_settings().database_url.get_secret_value().endswith("host=evil")


def test_db_url_with_driver_and_sslmode_still_accepted(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db?sslmode=require")
    assert load_settings().database_url.get_secret_value().endswith("sslmode=require")


def test_psycopg2_driver_is_rejected(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg2://u:hunter2@h/db")
    with pytest.raises(InvalidSettings) as ei:
        load_settings()
    assert "hunter2" not in str(ei.value)


def test_db_url_with_nul_byte_is_rejected_when_passed_directly():
    # a NUL cannot travel through the process environment, but can via an init kwarg
    with pytest.raises(ValidationError) as ei:
        Settings(QUARTET_DATABASE_URL="postgresql://u:hunter2@h/db\x00")
    assert "hunter2" not in str(ei.value)


def test_libpq_url_strips_only_the_leading_driver():
    from quartet.config import libpq_url

    assert libpq_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert libpq_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    tricky = "postgresql://u:p@h/db?application_name=postgresql+psycopg://x"
    assert libpq_url(tricky) == tricky


def test_validate_db_url_rejects_without_echoing_value():
    from quartet.config import validate_db_url

    validate_db_url("postgresql://u:p@h/db")
    with pytest.raises(ValueError) as err:
        validate_db_url("postgresql://u:secretpw@h/db?passfile=x")
    assert "secretpw" not in str(err.value) and "passfile" in str(err.value)


def test_home_is_tilde_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/q")
    monkeypatch.setenv("QUARTET_HOME", "~/x")
    assert load_settings().home == Path.home() / "x"
    assert Path.home() == tmp_path
