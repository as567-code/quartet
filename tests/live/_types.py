"""Shared type aliases for the live tests."""

from collections.abc import Callable

from quartet.llm.base import Provider
from quartet.llm.runner import ProviderRoleRunner

RunnerFactory = Callable[[Provider], ProviderRoleRunner]
