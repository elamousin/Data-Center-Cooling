"""Bench test bridge: stream a free-form Arduino sketch to the /adalm page.

The main dashboard only understands the framed `D,<seq>,...` protocol and the
channels in config.yaml. Bench sketches - like the ADALM2000 ADC check - just
print human-readable lines to the Serial Monitor, e.g.

    ADC: 512   Voltage: 2.502 V

This module reads those lines as-is and pulls out every `label: number [unit]`
pair, so the sketch needs no changes. A line of bare comma/space separated
numbers is accepted too and named ch1..chN.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from collections import deque
from typing import Any

import serial

from .sources.serial_source import list_serial_ports

READ_TIMEOUT_S = 1.0
HISTORY_LINES = 20000
RAW_LOG_LINES = 200
SUBSCRIBER_QUEUE_LIMIT = 256

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# label : number [unit]; the unit is skipped when it is really the next label.
_PAIR = re.compile(
    rf"([A-Za-z_][\w ]*?)\s*[:=]\s*({_NUMBER})(?:\s*([A-Za-z%°µ/]+)(?!\w)(?!\s*[:=]))?"
)
_BARE = re.compile(rf"^\s*{_NUMBER}(?:\s*[,;\t ]\s*{_NUMBER})*\s*$")


def parse_line(line: str) -> tuple[dict[str, float], dict[str, str]]:
    """Return ({field: value}, {field: unit}) for one printed line."""
    values: dict[str, float] = {}
    units: dict[str, str] = {}
    for label, number, unit in _PAIR.findall(line):
        key = label.strip()
        values[key] = float(number)
        if unit:
            units[key] = unit
    if not values and _BARE.match(line):
        for i, number in enumerate(re.findall(_NUMBER, line), start=1):
            values[f"ch{i}"] = float(number)
    return values, units


class BenchStream:
    """One serial port, opened on demand, fanned out to websocket subscribers."""

    def __init__(self) -> None:
        self.port: str | None = None
        self.baud = 9600
        self.state = "disconnected"
        self.detail = "Not connected"
        self.units: dict[str, str] = {}
        self.fields: list[str] = []
        self.history: deque[tuple[float, dict[str, float]]] = deque(maxlen=HISTORY_LINES)
        self.raw: deque[tuple[float, str]] = deque(maxlen=RAW_LOG_LINES)
        self.unparsed = 0

        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

    # ----------------------------------------------------------- connection

    async def connect(self, port: str | None, baud: int) -> None:
        await self.disconnect()
        if not port:
            boards = [p for p in list_serial_ports() if p["likelyBoard"]]
            if not boards:
                raise ValueError("No Arduino-like port found. Pick one explicitly.")
            port = boards[0]["device"]

        self.port, self.baud = port, baud
        self.state, self.detail = "connecting", f"Opening {port} at {baud} baud"
        self._publish({"type": "state", **self.describe()})
        try:
            self._serial = await asyncio.to_thread(
                serial.Serial, port, baud, timeout=READ_TIMEOUT_S
            )
        except (serial.SerialException, OSError) as exc:
            self.state = "error"
            self.detail = f"{port}: {exc}"
            if "denied" in str(exc).lower() or "PermissionError" in str(exc):
                self.detail += (
                    " - another program has the port open. Close the Arduino IDE "
                    "Serial Monitor / Plotter and try again."
                )
            self._publish({"type": "state", **self.describe()})
            raise ValueError(self.detail) from exc

        self.history.clear()
        self.raw.clear()
        self.fields, self.units, self.unparsed = [], {}, 0
        self.state, self.detail = "connected", f"Streaming from {port}"
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_lines, daemon=True)
        self._thread.start()
        self._publish({"type": "reset", **self.describe()})

    async def disconnect(self) -> None:
        self._stop.set()
        if self._serial is not None:
            with contextlib.suppress(serial.SerialException, OSError):
                await asyncio.to_thread(self._serial.close)
            self._serial = None
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2.0)
            self._thread = None
        if self.state != "error":
            self.state, self.detail = "disconnected", "Not connected"
        self._publish({"type": "state", **self.describe()})

    def _read_lines(self) -> None:
        """Blocking reader thread; hands each decoded line to the event loop."""
        try:
            while not self._stop.is_set():
                assert self._serial is not None
                raw = self._serial.readline()
                if raw:
                    text = raw.decode("utf-8", errors="replace").strip()
                    if text and self._loop is not None:
                        self._loop.call_soon_threadsafe(self._ingest, time.time(), text)
        except (serial.SerialException, OSError, AttributeError, TypeError) as exc:
            if not self._stop.is_set() and self._loop is not None:
                self._loop.call_soon_threadsafe(self._lost, str(exc))

    def _lost(self, reason: str) -> None:
        self.state, self.detail = "error", f"Lost {self.port}: {reason}"
        self._publish({"type": "state", **self.describe()})

    def _ingest(self, t: float, text: str) -> None:
        self.raw.append((t, text))
        values, units = parse_line(text)
        if not values:
            self.unparsed += 1
            self._publish({"type": "line", "t": t, "text": text, "values": None})
            return
        self.units.update(units)
        new_fields = [k for k in values if k not in self.fields]
        self.fields.extend(new_fields)
        self.history.append((t, values))
        self._publish(
            {
                "type": "line",
                "t": round(t, 3),
                "text": text,
                "values": values,
                "fields": self.fields if new_fields else None,
                "units": self.units if (new_fields or units) else None,
            }
        )

    # ---------------------------------------------------------- subscribers

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
                self._subscribers.discard(q)

    # ------------------------------------------------------------ readouts

    def describe(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "port": self.port,
            "baud": self.baud,
            "fields": self.fields,
            "units": self.units,
            "lines": len(self.history),
            "unparsed": self.unparsed,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.describe(),
            "t": [round(t, 3) for t, _ in self.history],
            "series": {f: [v.get(f) for _, v in self.history] for f in self.fields},
            "raw": [[round(t, 3), text] for t, text in self.raw],
        }
