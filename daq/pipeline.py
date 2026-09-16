"""Turn raw Arduino readings into a full, calibrated, derived sample."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RigConfig


@dataclass(slots=True)
class Sample:
    """One acquisition instant, in engineering units, for every channel."""

    t: float          # wall clock, unix seconds
    seq: int          # sequence number reported by the device
    device_ms: float  # device uptime in ms, for gap detection
    values: list[float | None]  # aligned to RigConfig.channels


class Pipeline:
    def __init__(self, config: RigConfig) -> None:
        self.config = config
        self._measured = config.measured
        self._all = config.channels
        self._flow_index = None
        if config.flow_channel_id is not None:
            ids = [c.id for c in self._measured]
            if config.flow_channel_id in ids:
                self._flow_index = ids.index(config.flow_channel_id)

    @property
    def expected_raw_width(self) -> int:
        return len(self._measured)

    def process(self, t: float, seq: int, device_ms: float, raw: list[float]) -> Sample:
        scope: dict[str, float] = {
            "rho": self.config.density_kg_m3,
            "cp": self.config.cp_j_kgk,
        }
        values: dict[str, float | None] = {}

        for index, channel in enumerate(self._measured):
            value = channel.convert(raw[index]) if index < len(raw) else None
            values[channel.id] = value
            if value is not None:
                scope[channel.id] = value

        if self._flow_index is not None and self._flow_index < len(raw):
            flow = values[self._measured[self._flow_index].id]
            if flow is not None:
                volumetric = self.config.flow_to_m3s(flow)
                if volumetric is not None:
                    scope["mdot"] = self.config.density_kg_m3 * volumetric

        for channel in self.config.derived:
            assert channel.expression is not None
            value = channel.expression.evaluate(scope)
            values[channel.id] = value
            if value is not None:
                scope[channel.id] = value

        return Sample(
            t=t,
            seq=seq,
            device_ms=device_ms,
            values=[values[c.id] for c in self._all],
        )
