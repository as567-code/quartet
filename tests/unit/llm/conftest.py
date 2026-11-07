import pytest

from quartet.llm.base import RoleConfig
from tests.unit.llm._records import cfg


@pytest.fixture
def role_config() -> RoleConfig:
    return cfg()
