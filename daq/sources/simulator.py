"""A synthetic rig, so the whole platform runs before the hardware exists.

This is not noise around a constant. It integrates a lumped model of a
single-phase liquid loop -- heater load, pump flow, chiller inlet, and the
thermal and hydraulic responses they drive -- so the derived channels
(heat removed, thermal resistance, heat balance) carry real physics and the
dashboard can be judged on realistic data.

Channels whose ids the model does not recognise fall back to a random walk
around their `sim.base`, so an arbitrary config still produces a live UI.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import AsyncIterator

from ..config import RigConfig
from .base import RawFrame, Source

# Channel ids the lumped model drives directly.
MODEL_CHANNELS = {"T_in", "T_out", "T_case", "T_amb", "P_in", "P_out", "Q_flow", "W_heater"}

NOMINAL_FLOW_LPM = 1.9
# Cold-plate convective resistance at nominal flow; scales as flow^-0.8.
BASE_CONV_RESISTANCE_KW = 0.035
# dP = K * flow^1.8, K set so a nominal 1.9 L/min gives ~32 kPa across the plate.
HYDRAULIC_K = 10.17
FLOW_EXPONENT = 1.8


class _LoopModel:
    """Lumped thermal-hydraulic state of the test loop."""

    def __init__(self, density: float, cp: float) -> None:
        self.density = density
        self.cp = cp

        self.load_target = 420.0
        self.w_heat = 420.0
        self.pump_target = NOMINAL_FLOW_LPM
        self.q_flow = NOMINAL_FLOW_LPM
        self.t_amb = 22.0
        self.t_in = 25.0
        self.t_out = 28.2
        self.t_case = 43.0
        self.p_out = 148.0

        self._next_load_step = 45.0
        self._elapsed = 0.0

    def step(self, dt: float) -> None:
        self._elapsed += dt

        # The rig operator changes the simulated die load every so often.
        if self._elapsed >= self._next_load_step:
            self.load_target = random.uniform(220.0, 620.0)
            self._next_load_step = self._elapsed + random.uniform(40.0, 90.0)

        self.w_heat += _approach(self.w_heat, self.load_target, dt, tau=6.0)
        self.pump_target += _walk(dt, scale=0.02, tau=60.0)
        self.pump_target = _clamp(self.pump_target, 1.2, 2.6)
        self.q_flow += _approach(self.q_flow, self.pump_target, dt, tau=2.0)

        self.t_amb += _approach(self.t_amb, 22.0, dt, tau=300.0) + _walk(dt, 0.05, 120.0)

        # Chiller holds the inlet near setpoint but lags behind load swings.
        inlet_setpoint = 25.0 + 0.004 * (self.w_heat - 420.0)
        self.t_in += _approach(self.t_in, inlet_setpoint, dt, tau=25.0) + _walk(dt, 0.02, 90.0)

        mass_flow = self.density * self.q_flow / 60000.0  # L/min -> kg/s
        heat_capacity_rate = max(mass_flow * self.cp, 1e-6)  # W/K

        t_out_steady = self.t_in + self.w_heat / heat_capacity_rate
        self.t_out += _approach(self.t_out, t_out_steady, dt, tau=8.0)

        flow_ratio = max(self.q_flow, 0.05) / NOMINAL_FLOW_LPM
        conv_resistance = BASE_CONV_RESISTANCE_KW * flow_ratio**-0.8
        t_case_steady = self.t_out + self.w_heat * conv_resistance
        self.t_case += _approach(self.t_case, t_case_steady, dt, tau=5.0)

        self.p_out += _approach(self.p_out, 148.0, dt, tau=120.0) + _walk(dt, 0.6, 45.0)

    @property
    def delta_p(self) -> float:
        return HYDRAULIC_K * max(self.q_flow, 0.0) ** FLOW_EXPONENT

    def true_value(self, channel_id: str) -> float | None:
        return {
            "T_in": self.t_in,
            "T_out": self.t_out,
            "T_case": self.t_case,
            "T_amb": self.t_amb,
            "Q_flow": self.q_flow,
            "W_heater": self.w_heat,
            "P_out": self.p_out,
            "P_in": self.p_out + self.delta_p,
        }.get(channel_id)


def _approach(current: float, target: float, dt: float, tau: float) -> float:
    """First-order lag increment toward `target` with time constant `tau`."""
    return (target - current) * (1.0 - math.exp(-dt / tau))


def _walk(dt: float, scale: float, tau: float) -> float:
    return random.gauss(0.0, scale) * math.sqrt(dt / tau)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class SimulatorSource(Source):
    kind = "simulator"

    def __init__(self, config: RigConfig) -> None:
        super().__init__(config)
        self._model = _LoopModel(config.density_kg_m3, config.cp_j_kgk)
        self._measured = config.measured
        self._fallback = {c.id: 0.0 for c in self._measured if c.id not in MODEL_CHANNELS}

    async def frames(self) -> AsyncIterator[RawFrame]:
        period = 1.0 / max(self.config.rate_hz, 0.1)
        self.status.state = "connected"
        self.status.detail = "Synthetic rig - no hardware attached"
        self.status.port = None

        seq = 0
        started = time.perf_counter()
        next_tick = started

        while True:
            next_tick += period
            now = time.perf_counter()
            delay = next_tick - now
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Behind schedule. Drop the missed ticks rather than emitting a
                # burst of samples spaced microseconds apart, which would both
                # corrupt the rate estimate and put bogus points in the run file.
                if delay < -period:
                    next_tick = now
                await asyncio.sleep(0)

            self._model.step(period)
            seq += 1
            yield RawFrame(
                seq=seq,
                device_ms=(time.perf_counter() - started) * 1000.0,
                values=[self._read(channel) for channel in self._measured],
            )

    def _read(self, channel) -> float:
        noise = channel.sim.get("noise", 0.0)
        truth = self._model.true_value(channel.id)

        if truth is None:
            base = channel.sim.get("base", 0.0)
            drift = channel.sim.get("drift", 0.0)
            state = self._fallback.get(channel.id, 0.0) * 0.999 + random.gauss(0.0, 0.03)
            self._fallback[channel.id] = _clamp(state, -3.0, 3.0)
            truth = base + drift * self._fallback[channel.id]

        return truth + (random.gauss(0.0, noise) if noise else 0.0)
