"""Common shape for anything that produces raw sensor frames."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import AsyncIterator

from ..config import RigConfig


@dataclass(slots=True)
class RawFrame:
    """One line of readings as the device sent them, before calibration.

    `values` is positional and aligned to RigConfig.measured.
    """

    seq: int
    device_ms: float
    values: list[float]


@dataclass(slots=True)
class SourceStatus:
    """What the UI shows in the connection pill."""

    state: str            # connected | connecting | disconnected | error
    detail: str = ""
    port: str | None = None


class Source(abc.ABC):
    kind: str = "unknown"

    def __init__(self, config: RigConfig) -> None:
        self.config = config
        self.status = SourceStatus(state="disconnected")

    @abc.abstractmethod
    def frames(self) -> AsyncIterator[RawFrame]:
        """Yield frames until cancelled. Implementations must be restartable."""

    async def aclose(self) -> None:
        """Release hardware. Safe to call when never opened."""

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "state": self.status.state,
            "detail": self.status.detail,
            "port": self.status.port,
        }
