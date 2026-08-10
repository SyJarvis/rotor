from dataclasses import dataclass
from typing import Any


class RotorMCPError(Exception):
    """Base class for errors safe to expose as MCP tool failures."""


class ConfigurationError(RotorMCPError):
    pass


class InvalidToolInput(RotorMCPError):
    pass


class RotorBackendUnavailable(RotorMCPError):
    pass


class InvalidControlAPIResponse(RotorMCPError):
    pass


@dataclass(slots=True)
class ControlAPIError(RotorMCPError):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"Rotor Control API error ({self.code}): {self.message}"
