import logging
import re
import time

import pytest
from pydantic import SecretStr

from quartet.config import Settings
from quartet.telemetry.redaction import (
    REDACTED,
    RedactingFilter,
    Redactor,
    install_log_redaction,
    redact_value,
    redactor_from_settings,
)

FAKES = [
    "sk-ant-api03-" + "A" * 40,
    "sk-proj-" + "b" * 40,
    "sk-" + "c" * 48,
    "ghs_" + "d" * 36,
    "ghp_" + "e" * 36,
    "github_pat_" + "f" * 40,
]


@pytest.fixture(autouse=True)
def _restore_record_factory():
    factory = logging.getLogRecordFactory()
    import quartet.telemetry.redaction as mod

    saved = dict(mod._factory_state)
    yield
    logging.setLogRecordFactory(factory)
    mod._factory_state.update(saved)


@pytest.mark.parametrize("secret", FAKES)
def test_patterns_scrubbed(secret):
    out = Redactor().redact(f"error: key {secret} rejected")
    assert secret not in out and REDACTED in out


def test_header_and_bearer_forms():
    r = Redactor()
    for s in (
        "Authorization: Bearer abcdefgh12345678",
        "x-api-key: zzzzzzzzzzzz",
        '"api_key": "k123456789"',
    ):
        assert REDACTED in r.redact(s)
    assert "k123456789" not in r.redact('"api_key": "k123456789"')


def test_known_values_scrubbed_and_short_values_ignored():
    assert "hunter2-very-long" not in Redactor(["hunter2-very-long"]).redact("pw=hunter2-very-long")
    assert Redactor(["abc"]).redact("abc") == "abc"


def test_postgres_password_scrubbed_host_kept():
    out = Redactor().redact("postgresql://quartet_app:s3cr3tpass@127.0.0.1:5432/q")
    assert "s3cr3tpass" not in out and "127.0.0.1:5432" in out


def test_idempotent_and_recursive():
    r = Redactor()
    text = f"a {FAKES[0]} b"
    assert r.redact(r.redact(text)) == r.redact(text)
    assert FAKES[1] not in str(redact_value({"x": [FAKES[1]]}, r))
    assert redact_value({"n": 3}, r) == {"n": 3}


def test_redact_value_redacts_dict_keys_too():
    r = Redactor()
    out = redact_value({FAKES[0]: {FAKES[1]: [1, "ok"]}}, r)
    assert FAKES[0] not in str(out) and FAKES[1] not in str(out)
    assert out == {REDACTED: {REDACTED: [1, "ok"]}}


def test_redactor_from_settings_covers_keys_and_db_passwords():
    settings = Settings(
        _env_file=None,
        QUARTET_DATABASE_URL="postgresql+psycopg://app:app-pw-secret1@127.0.0.1:5432/q",
        QUARTET_ADMIN_DATABASE_URL="postgresql://adm:adm-pw-secret2@127.0.0.1:5432/q",
        ANTHROPIC_API_KEY=SecretStr("plain-anthropic-key-1"),
        OPENAI_API_KEY=SecretStr("plain-openai-key-2"),
    )
    text = "app-pw-secret1 adm-pw-secret2 plain-anthropic-key-1 plain-openai-key-2"
    assert redactor_from_settings(settings).redact(text) == " ".join([REDACTED] * 4)


def test_bad_format_string_does_not_raise_and_is_redacted(caplog):
    install_log_redaction(Redactor())
    with caplog.at_level(logging.INFO):
        logging.getLogger("quartet.test").info("n=%d", FAKES[0])  # mismatched args
    assert caplog.records and REDACTED in caplog.text
    assert FAKES[0] not in caplog.text


def test_logging_filter(caplog):
    install_log_redaction(Redactor())
    with caplog.at_level(logging.INFO):
        logging.getLogger("quartet.test.child").warning("got %s", FAKES[0])
        logging.getLogger("anthropic._base_client").info("hdr %(k)s", {"k": FAKES[1]})
    assert caplog.records and REDACTED in caplog.text
    assert FAKES[0] not in caplog.text and FAKES[1] not in caplog.text


def test_install_is_idempotent_and_swaps_redactor(caplog):
    install_log_redaction(Redactor())
    factory = logging.getLogRecordFactory()
    install_log_redaction(Redactor(["swapped-secret-value"]))
    assert logging.getLogRecordFactory() is factory
    logging.getLogger("quartet.test").warning("x swapped-secret-value")
    assert caplog.records and REDACTED in caplog.text
    assert "swapped-secret-value" not in caplog.text


def test_missing_mapping_key_does_not_raise(caplog):
    install_log_redaction(Redactor())
    logging.getLogger("quartet.test").warning("%(missing)s", {"other": FAKES[0]})
    assert caplog.records and REDACTED in caplog.text
    assert FAKES[0] not in caplog.text


def test_redacting_filter_for_handlers():
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "k=%s", (FAKES[0],), None)
    assert RedactingFilter(Redactor()).filter(record)
    assert FAKES[0] not in record.getMessage() and record.args == ()


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("ANTHROPIC_API_KEY=plainvalue123456", "plainvalue123456"),
        ("password=s3cr3t host=db", "s3cr3t"),
        ("Authorization: Basic dXNlcjpzZWNyZXQ=", "dXNlcjpzZWNyZXQ"),
        ("Authorization: token abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ],
)
def test_extended_leak_cases(text, leak):
    out = Redactor().redact(text)
    assert leak not in out and REDACTED in out


def test_word_boundary_on_sk_patterns():
    for text in ("task-3-redaction-and-tracing-helpers", "desk-0123456789abcdef0123456789"):
        assert Redactor().redact(text) == text


def test_percent_encoded_known_secret():
    r = Redactor(["p@ss:word-long"])
    assert "p%40ss%3Aword-long" not in r.redact("pw=p%40ss%3Aword-long")
    assert "p%40ss" not in r.redact("postgresql://u:p%40ss%3Aword-long@h/db")


def test_exception_text_redacted_in_logs(caplog):
    install_log_redaction(Redactor())
    with caplog.at_level(logging.INFO):
        try:
            raise ValueError(f"bad key {FAKES[0]}")
        except ValueError:
            logging.getLogger("quartet.test").exception("failed")
    assert "ValueError" in caplog.text and REDACTED in caplog.text
    assert FAKES[0] not in caplog.text


def test_stack_info_redacted_in_logs(caplog):
    install_log_redaction(Redactor())
    with caplog.at_level(logging.INFO):
        logging.getLogger("quartet.test").info(f"stack {FAKES[0]}", stack_info=True)
    assert "Stack (most recent call last)" in caplog.text and REDACTED in caplog.text
    assert FAKES[0] not in caplog.text


def test_redacting_filter_survives_bad_format_string():
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "n=%d", (FAKES[0],), None)
    assert RedactingFilter(Redactor()).filter(record)
    assert FAKES[0] not in record.getMessage() and REDACTED in record.getMessage()


@pytest.mark.parametrize("prefix", ["\\n", "%3D", "_", "="])
@pytest.mark.parametrize("key", [FAKES[0], FAKES[2]])
def test_sk_keys_redacted_after_escapes(prefix, key):
    out = Redactor().redact(f"x{prefix}{key}")
    assert key not in out and REDACTED in out


def test_sk_boundary_still_protects_words():
    for text in ("task-3-redaction-and-tracing-helpers", "desk-0123456789abcdefghij"):
        assert Redactor().redact(text) == text


def test_alphabetic_values_do_not_swallow_following_text():
    r = Redactor()
    assert r.redact("password=secret host=db").endswith("host=db")
    assert r.redact("api-key: mysecretvalue and more").endswith("and more")
    assert "secret " not in r.redact("password=secret host=db")
    assert "dXNlcjpz" not in r.redact("Authorization: Basic dXNlcjpz")


@pytest.mark.parametrize(
    ("header", "credential"),
    [
        ("Authorization: Negotiate YIIabc123456", "YIIabc123456"),
        ("Authorization: NTLM TlRMTVNTUAAB", "TlRMTVNTUAAB"),
        ("Authorization: ApiKey " + "k-" + "1234567890", "k-1234567890"),
        ("Authorization: SSWS 00abcDEF123456", "00abcDEF123456"),
        ("Authorization: Basic dXNlcjpzZWNyZXQ=", "dXNlcjpzZWNyZXQ"),
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"),
        ("Authorization: token abcdefghijklmnop1234", "abcdefghijklmnop1234"),
        ("Authorization: Digest abcdef1234567890", "abcdef1234567890"),
    ],
)
def test_authorization_value_fully_redacted(header, credential):
    out = Redactor().redact(header)
    assert credential not in out and REDACTED in out


def test_json_authorization_keeps_following_fields():
    out = Redactor().redact('"authorization": "Bearer abcdefgh12345678", "next": 1')
    assert "abcdefgh12345678" not in out and REDACTED in out and '"next": 1' in out


@pytest.mark.parametrize(
    ("text", "leak", "keep"),
    [
        ("password=secret host=db", "secret", "host=db"),
        ("api-key: mysecretvalue and more", "mysecretvalue", "and more"),
        ("x-api-key: zzzzzzzzzzzz", "zzzzzzzzzzzz", None),
        ('"api_key": "k123456789"', "k123456789", None),
        ("ANTHROPIC_API_KEY=plainvalue123456", "plainvalue123456", None),
    ],
)
def test_single_token_keys_redact_one_token(text, leak, keep):
    out = Redactor().redact(text)
    assert leak not in out and REDACTED in out
    if keep is not None:
        assert keep in out


CRED = "Zq9" + "x" * 17
KEYWORDS = ["Authorization", "x-api-key", "api_key", "api-key", "password", "passwd"]
SCHEMES = ["", "Basic ", "Bearer ", "Token ", "Digest ", "ApiKey ", "Negotiate ", "NTLM ", "SSWS "]
SEPARATORS = [": ", "=", '": "']


@pytest.mark.parametrize("keyword", KEYWORDS)
@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("separator", SEPARATORS)
def test_keyword_scheme_separator_matrix_never_leaks(keyword, scheme, separator):
    out = Redactor().redact(f"{keyword}{separator}{scheme}{CRED}")
    assert CRED not in out and REDACTED in out


@pytest.mark.parametrize(
    ("text", "leaks"),
    [
        ('password: "my secret pass"', ["my", "secret", "pass"]),
        ("password='my secret pass'", ["my", "secret", "pass"]),
        ('{"api_key": "two words here", "x": 1}', ["two", "words", "here"]),
        ('password: "Basic my secret pass"', ["my", "secret", "pass"]),
        ("api_key='Token two words'", ["two", "words"]),
        ('"authorization": "Bearer ab\\"cd1234567890", "next": 1', ["cd1234567890"]),
        ('password: "Basic abc def"', ["abc", "def"]),
        ('password: "Token abc def ghi"', ["abc", "def", "ghi"]),
        ("password: 'Token abc def'", ["abc", "def"]),
        ('password: Basic "abc def"', ["abc", "def"]),
        ('Authorization: Bearer "abc def"', ["abc", "def"]),
    ],
)
def test_quoted_multi_word_values_fully_redacted(text, leaks):
    out = Redactor().redact(text)
    assert REDACTED in out
    # Look only past the first separator so "pass" is not hidden by the field name "password".
    value_part = re.split(r"[:=]\s*", out, maxsplit=1)[-1]
    for word in leaks:
        assert word not in value_part


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password: "Basic my secret pass"', f'password: "{REDACTED}"'),
        ("api_key='Token two words'", f"api_key='{REDACTED}'"),
        (
            '"authorization": "Bearer ab\\"cd1234567890", "next": 1',
            f'"authorization": "{REDACTED}", "next": 1',
        ),
    ],
)
def test_quoted_values_keep_their_quotes_around_redaction(text, expected):
    assert Redactor().redact(text) == expected


CRED3 = "alpha" + "beta " + "gamma" + "delta " + "zeta" + "omega"
CRED3_WORDS = ["alphabeta", "gammadelta", "zetaomega"]


@pytest.mark.parametrize("keyword", KEYWORDS)
@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_three_word_matrix_never_leaks(keyword, scheme, quote):
    out = Redactor().redact(f"{keyword}: {quote}{scheme}{CRED3}{quote}, next")
    assert not any(word in out for word in CRED3_WORDS)
    assert f"{quote}{REDACTED}{quote}" in out and ", next" in out


def test_quote_scanning_is_linear_on_adversarial_input():
    text = "password: " + '" ' * 50_000
    start = time.perf_counter()
    Redactor().redact(text)
    assert time.perf_counter() - start < 0.5


def test_quoted_value_keeps_following_json_field_and_unquoted_keeps_text():
    out = Redactor().redact('{"api_key": "two words here", "x": 1}')
    assert '"x": 1' in out
    assert "host=db" in Redactor().redact("password=secret host=db")
