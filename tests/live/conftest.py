"""Live-test fixtures. Nothing here reads keys or settings at import time: ``make check``
imports this package even though the tests are deselected. A missing key FAILS the test (an
explicit ``-m live`` shows intent to spend money), it does not skip it.
"""

import os
from collections.abc import Generator, Iterator
from pathlib import Path

import openai
import pluggy
import pytest
from anthropic import DefaultAsyncHttpxClient

from quartet.config import InsecureEnvFile, InvalidSettings, Settings, load_settings
from quartet.llm.base import Provider
from quartet.llm.cassette import RecordingTransport, save_cassette
from quartet.llm.runner import MissingApiKey, ProviderRoleRunner, build_role_runner
from quartet.telemetry.redaction import redactor_from_settings
from tests.live._types import RunnerFactory

CASSETTE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"


def recording_enabled() -> bool:
    return os.environ.get("QUARTET_RECORD_CASSETTES") == "1"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.stash[_PASSED] = report.passed


_PASSED = pytest.StashKey[bool]()


@pytest.fixture
def live_settings() -> Settings:
    try:
        return load_settings()
    except (InsecureEnvFile, InvalidSettings) as exc:
        pytest.fail(f"live tests need valid settings: {exc}", pytrace=False)


@pytest.fixture
def live_runner(request: pytest.FixtureRequest, live_settings: Settings) -> Iterator[RunnerFactory]:
    """Build a runner per provider; with QUARTET_RECORD_CASSETTES=1 each records its exchanges
    and, if the test passes, saves ``tests/fixtures/cassettes/<test>-<provider>.json``."""
    recorders: dict[Provider, RecordingTransport] = {}

    def make(provider: Provider) -> ProviderRoleRunner:
        http_client = None
        if recording_enabled():
            transport = recorders.setdefault(provider, RecordingTransport())
            if provider is Provider.ANTHROPIC:
                http_client = DefaultAsyncHttpxClient(transport=transport)
            else:
                http_client = openai.DefaultAsyncHttpx2Client(transport=transport)
        try:
            return build_role_runner(live_settings, provider, http_client=http_client)
        except MissingApiKey as exc:
            pytest.fail(str(exc), pytrace=False)

    yield make

    if recorders and request.node.stash.get(_PASSED, False):
        redactor = redactor_from_settings(live_settings)
        name = request.node.originalname
        CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
        for provider, transport in recorders.items():
            save_cassette(
                CASSETTE_DIR / f"{name}-{provider.value}.json", transport.exchanges, redactor
            )
