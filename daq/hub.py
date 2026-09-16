"""Central runtime: owns the source, the history buffer, and the subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

from .config import RigConfig
from .pipeline import Pipeline, Sample
from .recorder import Recorder
from .sources import SerialSource, SimulatorSource, Source

log = logging.getLogger("daq.hub")

# Live samples are batched into one websocket message per flush so a 100 Hz
# rig does not push 100 messages/second at every browser tab.
BROADCAST_INTERVAL_S = 0.05
SUBSCRIBER_QUEUE_LIMIT = 256
RATE_WINDOW_S = 3.0


class Hub:
    def __init__(self, config: RigConfig) -> None:
        self.config = config
        self.pipeline = Pipeline(config)
        self.recorder = Recorder(config)

        depth = max(config.buffer_samples, 100)
        self._times: deque[float] = deque(maxlen=depth)
        self._series: dict[str, deque[float | None]] = {
            channel.id: deque(maxlen=depth) for channel in config.channels
        }

        self._subscribers: set[asyncio.Queue] = set()
        self._pending: list[Sample] = []
        self._source: Source | None = None
        self._reader_task: asyncio.Task | None = None
        self._flusher_task: asyncio.Task | None = None

        self._latest: Sample | None = None
        self._last_seq: int | None = None
        self._recent_t: deque[float] = deque(maxlen=1024)
        self._rate_hz = 0.0
        self._dropped = 0
        self._received = 0
        self._started_at = time.time()

    # ---------------------------------------------------------------- source

    @property
    def source_kind(self) -> str:
        return self._source.kind if self._source else "none"

    async def start(self, kind: str | None = None, port: str | None = None) -> None:
        await self.stop()

        kind = (kind or self.config.source).lower()
        if kind == "serial":
            self._source = SerialSource(self.config, port=port)
        elif kind == "simulator":
            self._source = SimulatorSource(self.config)
        else:
            raise ValueError(f"unknown source {kind!r}")

        self._last_seq = None
        self._recent_t.clear()
        self._rate_hz = 0.0
        self._dropped = 0
        self._received = 0

        self._reader_task = asyncio.create_task(self._read_loop(), name="daq-reader")
        self._flusher_task = asyncio.create_task(self._flush_loop(), name="daq-flusher")
        await self._broadcast_state()

    async def stop(self) -> None:
        for task in (self._reader_task, self._flusher_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._flusher_task = None

        if self._source is not None:
            await self._source.aclose()
            self._source = None

    async def _read_loop(self) -> None:
        assert self._source is not None
        try:
            async for frame in self._source.frames():
                now = time.time()
                sample = self.pipeline.process(now, frame.seq, frame.device_ms, frame.values)
                self._ingest(sample)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("acquisition loop failed")
            if self._source is not None:
                self._source.status.state = "error"
                self._source.status.detail = "Acquisition loop failed; see server log"
            await self._broadcast_state()

    def _ingest(self, sample: Sample) -> None:
        self._received += 1

        if self._last_seq is not None and sample.seq > self._last_seq + 1:
            self._dropped += sample.seq - self._last_seq - 1
        self._last_seq = sample.seq

        # Rate over a sliding window, not 1/dt: scheduler jitter makes the
        # instantaneous figure swing by an order of magnitude.
        self._recent_t.append(sample.t)
        while len(self._recent_t) > 2 and sample.t - self._recent_t[0] > RATE_WINDOW_S:
            self._recent_t.popleft()
        if len(self._recent_t) >= 2:
            span = self._recent_t[-1] - self._recent_t[0]
            self._rate_hz = (len(self._recent_t) - 1) / span if span > 0 else 0.0

        self._times.append(sample.t)
        for channel, value in zip(self.config.channels, sample.values):
            self._series[channel.id].append(value)

        self._latest = sample
        self._pending.append(sample)
        self.recorder.write(sample)

    # ----------------------------------------------------------- subscribers

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # A stalled tab must not stall acquisition; it resyncs on reconnect.
                self._subscribers.discard(q)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL_S)
            if not self._pending:
                continue
            batch, self._pending = self._pending, []
            self._publish(
                {
                    "type": "samples",
                    "rows": [[round(s.t, 3), *s.values] for s in batch],
                    "rateHz": round(self._rate_hz, 2),
                    "recording": self.recorder.run.samples if self.recorder.active else None,
                }
            )

    async def _broadcast_state(self) -> None:
        self._publish({"type": "state", **self.state()})

    # ------------------------------------------------------------- readouts

    def state(self) -> dict[str, Any]:
        source = self._source.describe() if self._source else {"kind": "none", "state": "disconnected", "detail": "Not started", "port": None}
        run = self.recorder.run
        return {
            "source": source,
            "rateHz": round(self._rate_hz, 2),
            "expectedRateHz": self.config.rate_hz,
            "received": self._received,
            "dropped": self._dropped,
            "uptimeS": round(time.time() - self._started_at, 1),
            "recording": run.to_json() if run else None,
            "latest": self.latest(),
        }

    def latest(self) -> dict[str, Any] | None:
        if self._latest is None:
            return None
        return {
            "t": round(self._latest.t, 3),
            "values": {
                channel.id: value
                for channel, value in zip(self.config.channels, self._latest.values)
            },
        }

    def history(self, window_s: float | None = None, max_points: int = 4000) -> dict[str, Any]:
        times = list(self._times)
        if not times:
            return {"t": [], "series": {c.id: [] for c in self.config.channels}}

        start_index = 0
        if window_s:
            cutoff = times[-1] - window_s
            lo, hi = 0, len(times)
            while lo < hi:  # times is ascending
                mid = (lo + hi) // 2
                if times[mid] < cutoff:
                    lo = mid + 1
                else:
                    hi = mid
            start_index = lo

        count = len(times) - start_index
        stride = max(1, count // max_points) if max_points else 1

        sliced = times[start_index::stride]
        series = {
            channel_id: list(values)[start_index::stride]
            for channel_id, values in self._series.items()
        }
        return {
            "t": [round(t, 3) for t in sliced],
            "series": series,
            "stride": stride,
            "totalSamples": len(times),
        }

    # -------------------------------------------------------------- actions

    async def set_tare(self, channel_id: str, enabled: bool) -> float:
        channel = self.config.by_id(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        if channel.derived:
            raise ValueError("derived channels cannot be tared; tare their inputs")

        if not enabled:
            channel.tare = 0.0
        else:
            current = self._series[channel_id][-1] if self._series[channel_id] else None
            if current is None:
                raise ValueError("no reading yet on this channel")
            channel.tare += current
        await self._broadcast_state()
        return channel.tare

    async def start_recording(self, name: str, operator: str, notes: str) -> dict[str, Any]:
        run = self.recorder.start(name=name, operator=operator, notes=notes)
        await self._broadcast_state()
        return run.to_json()

    async def stop_recording(self) -> dict[str, Any] | None:
        run = self.recorder.stop()
        await self._broadcast_state()
        return run.to_json() if run else None
