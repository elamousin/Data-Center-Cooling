"""Read framed sensor lines from an Arduino over USB serial.

Wire format (see firmware/daq_firmware/daq_firmware.ino):

    #DAQ1 {"fw":"1.0.0","rate_hz":10,"channels":["T_in","T_out",...]}
    D,<seq>,<millis>,<v1>,<v2>,...,<vN>*<xor>
    !<level> <message>

The header and the `*<xor>` checksum are both optional. The header, when
present, is checked against config.yaml so a firmware/config mismatch surfaces
as a visible warning instead of silently mislabelled data.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any, AsyncIterator

import serial
from serial.tools import list_ports

from ..config import RigConfig
from .base import RawFrame, Source

# USB vendor ids that commonly front an Arduino-compatible board.
_KNOWN_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4, 0x239A, 0x16C0}
_KNOWN_HINTS = ("arduino", "ch340", "ch910", "cp210", "ftdi", "usb serial", "wch", "usb-serial")

RECONNECT_DELAY_S = 2.0
READ_TIMEOUT_S = 1.0


def list_serial_ports() -> list[dict[str, Any]]:
    ports = []
    for info in list_ports.comports():
        ports.append(
            {
                "device": info.device,
                "description": info.description or "",
                "manufacturer": info.manufacturer or "",
                "likelyBoard": _is_likely_board(info),
            }
        )
    ports.sort(key=lambda p: (not p["likelyBoard"], p["device"]))
    return ports


def _is_likely_board(info: Any) -> bool:
    if getattr(info, "vid", None) in _KNOWN_VIDS:
        return True
    haystack = f"{info.description or ''} {info.manufacturer or ''}".lower()
    return any(hint in haystack for hint in _KNOWN_HINTS)


def _autodetect_port() -> str | None:
    candidates = [p for p in list_serial_ports() if p["likelyBoard"]]
    if candidates:
        return candidates[0]["device"]
    remaining = list_serial_ports()
    return remaining[0]["device"] if remaining else None


def _checksum(payload: str) -> int:
    value = 0
    for char in payload:
        value ^= ord(char)
    return value


class SerialSource(Source):
    kind = "serial"

    def __init__(self, config: RigConfig, port: str | None = None) -> None:
        super().__init__(config)
        self._requested_port = port or config.serial_port
        self._baud = config.serial_baud
        self._expected_width = len(config.measured)
        self._serial: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.bad_lines = 0
        self.device_info: dict[str, Any] = {}

    async def frames(self) -> AsyncIterator[RawFrame]:
        loop = asyncio.get_running_loop()

        while True:
            port = self._requested_port
            if port in ("auto", "", None):
                port = _autodetect_port()
            if port is None:
                self.status.state = "disconnected"
                self.status.detail = "No serial ports found. Plug in the Arduino."
                self.status.port = None
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue

            self.status.state = "connecting"
            self.status.detail = f"Opening {port} at {self._baud} baud"
            self.status.port = port

            try:
                self._serial = await asyncio.to_thread(
                    serial.Serial, port, self._baud, timeout=READ_TIMEOUT_S
                )
            except (serial.SerialException, OSError) as exc:
                self.status.state = "error"
                self.status.detail = f"{port}: {exc}"
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue

            # Most boards reset when the port opens; give the sketch time to boot.
            await asyncio.sleep(2.0)
            self.status.state = "connected"
            self.status.detail = f"Streaming from {port}"

            lines: asyncio.Queue[str | None] = asyncio.Queue(maxsize=4096)
            self._stop.clear()
            self._reader = threading.Thread(
                target=self._read_lines, args=(loop, lines), daemon=True
            )
            self._reader.start()

            try:
                while True:
                    line = await lines.get()
                    if line is None:  # reader thread ended
                        break
                    frame = self._parse(line)
                    if frame is not None:
                        yield frame
            finally:
                await self._close_port()

            if self.status.state == "connected":
                self.status.state = "disconnected"
                self.status.detail = f"Lost connection to {port}; retrying"
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _read_lines(self, loop: asyncio.AbstractEventLoop, out: asyncio.Queue) -> None:
        """Blocking reader; runs on its own thread and hands lines to the loop."""
        try:
            while not self._stop.is_set():
                assert self._serial is not None
                raw = self._serial.readline()
                if not raw:
                    continue  # read timeout, port still open
                try:
                    text = raw.decode("utf-8", errors="replace").strip()
                except UnicodeDecodeError:
                    continue
                if text:
                    loop.call_soon_threadsafe(self._offer, out, text)
        except (serial.SerialException, OSError, AttributeError) as exc:
            self.status.state = "error"
            self.status.detail = str(exc)
        finally:
            loop.call_soon_threadsafe(self._offer, out, None)

    @staticmethod
    def _offer(out: asyncio.Queue, item: str | None) -> None:
        try:
            out.put_nowait(item)
        except asyncio.QueueFull:
            pass  # UI is behind; dropping is better than unbounded growth

    async def _close_port(self) -> None:
        self._stop.set()
        if self._serial is not None:
            try:
                await asyncio.to_thread(self._serial.close)
            except (serial.SerialException, OSError):
                pass
            self._serial = None
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None

    async def aclose(self) -> None:
        await self._close_port()

    def _parse(self, line: str) -> RawFrame | None:
        if line.startswith("#DAQ"):
            self._handle_header(line)
            return None
        if line.startswith("!"):
            self.status.detail = line[1:].strip()
            return None
        if not line.startswith("D,"):
            return None

        payload = line
        if "*" in line:
            payload, _, given = line.rpartition("*")
            try:
                if _checksum(payload) != int(given, 16):
                    self.bad_lines += 1
                    return None
            except ValueError:
                self.bad_lines += 1
                return None

        parts = payload.split(",")
        if len(parts) < 3:
            self.bad_lines += 1
            return None

        try:
            seq = int(parts[1])
            device_ms = float(parts[2])
            values = [float(p) if p not in ("", "nan", "NaN") else float("nan") for p in parts[3:]]
        except ValueError:
            self.bad_lines += 1
            return None

        if len(values) != self._expected_width:
            self.bad_lines += 1
            self.status.detail = (
                f"Firmware sent {len(values)} values but config.yaml defines "
                f"{self._expected_width} channels"
            )
            return None

        return RawFrame(seq=seq, device_ms=device_ms, values=values)

    def _handle_header(self, line: str) -> None:
        _, _, body = line.partition(" ")
        try:
            info = json.loads(body)
        except json.JSONDecodeError:
            return
        self.device_info = info
        reported = info.get("channels")
        expected = [c.id for c in self.config.measured]
        if isinstance(reported, list) and reported != expected:
            self.status.detail = (
                "Firmware channel order does not match config.yaml: firmware sends "
                f"{', '.join(map(str, reported))}"
            )

    def describe(self) -> dict:
        info = super().describe()
        info["badLines"] = self.bad_lines
        info["device"] = self.device_info
        info["baud"] = self._baud
        return info
