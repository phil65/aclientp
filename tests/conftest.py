"""Shared fixtures."""

import pytest

from aclientp.agent.implementations.testing import TestAgent
from aclientp.client.implementations.default_client import DefaultACPClient


@pytest.fixture
def test_client() -> DefaultACPClient:
    """A fresh in-memory client."""
    return DefaultACPClient(allow_file_operations=True, use_real_files=False)


@pytest.fixture
def test_agent() -> TestAgent:
    """A fresh test agent."""
    return TestAgent()
