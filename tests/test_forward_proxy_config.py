"""Validation for resource-safety settings."""

import math

import pytest
from pydantic import ValidationError

from src.proxy.config import Settings


def test_forward_idle_timeout_default_and_override() -> None:
    assert Settings().forward_idle_timeout == 660.0
    assert Settings(forward_idle_timeout=15.5).forward_idle_timeout == 15.5
    assert Settings().forward_max_connections == 512


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf, -math.inf])
def test_forward_idle_timeout_must_be_positive_and_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(forward_idle_timeout=value)


def test_forward_max_connections_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(forward_max_connections=0)
