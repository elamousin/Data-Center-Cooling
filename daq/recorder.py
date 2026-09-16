"""Write runs to disk as CSV, with a JSON sidecar describing the run."""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .config import RigConfig
from .pipeline import Sample

_SLUG_STRIP = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(name: str) -> str:
    slug = _SLUG_STRIP.sub("-", name.strip()).strip("-").lower()
    return slug[:60] or "run"


@dataclass
class RunInfo:
    name: str
    filename: str
    started_at: str
    started_unix: float
    operator: str = ""
    notes: str = ""
    samples: int = 0
    ended_at: str | None = None
    duration_s: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Recorder:
    """Owns at most one open run at a time."""

    FLUSH_INTERVAL_S = 1.0

    def __init__(self, config: RigConfig) -> None:
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO | None = None
        self._writer: Any = None
        self._run: RunInfo | None = None
        self._last_flush = 0.0

    @property
    def active(self) -> bool:
        return self._run is not None

    @property
    def run(self) -> RunInfo | None:
        return self._run

    def start(self, name: str = "", operator: str = "", notes: str = "") -> RunInfo:
        if self._run is not None:
            raise RuntimeError("a run is already recording")

        now = datetime.now(timezone.utc).astimezone()
        stamp = now.strftime("%Y-%m-%d_%H%M%S")
        display_name = name.strip() or f"Run {now.strftime('%Y-%m-%d %H:%M:%S')}"
        filename = f"{stamp}_{_slugify(display_name)}.csv"

        run = RunInfo(
            name=display_name,
            filename=filename,
            started_at=now.isoformat(timespec="seconds"),
            started_unix=time.time(),
            operator=operator.strip(),
            notes=notes.strip(),
        )

        self._handle = (self.config.data_dir / filename).open(
            "w", newline="", encoding="utf-8"
        )
        self._writer = csv.writer(self._handle)
        self._writer.writerow(
            ["timestamp", "t_unix", "elapsed_s", "seq"] + [c.id for c in self.config.channels]
        )
        self._run = run
        self._last_flush = time.monotonic()
        self._write_sidecar()
        return run

    def write(self, sample: Sample) -> None:
        if self._run is None or self._writer is None:
            return

        elapsed = sample.t - self._run.started_unix
        if elapsed < 0:
            return

        stamp = datetime.fromtimestamp(sample.t).isoformat(timespec="milliseconds")
        self._writer.writerow(
            [stamp, f"{sample.t:.3f}", f"{elapsed:.3f}", sample.seq]
            + ["" if v is None else f"{v:.6g}" for v in sample.values]
        )
        self._run.samples += 1
        self._run.duration_s = elapsed

        now = time.monotonic()
        if now - self._last_flush >= self.FLUSH_INTERVAL_S:
            self._handle.flush()  # type: ignore[union-attr]
            self._last_flush = now

    def stop(self) -> RunInfo | None:
        if self._run is None:
            return None
        run = self._run
        run.ended_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
        self._handle = None
        self._writer = None
        self._run = None
        self._write_sidecar(run)
        return run

    def _write_sidecar(self, run: RunInfo | None = None) -> None:
        run = run or self._run
        if run is None:
            return
        meta = {
            "run": run.to_json(),
            "rig": {"name": self.config.name, "subtitle": self.config.subtitle},
            "fluid": {
                "name": self.config.fluid_name,
                "density_kg_m3": self.config.density_kg_m3,
                "cp_j_kgk": self.config.cp_j_kgk,
            },
            "acquisition": {"source": self.config.source, "rate_hz": self.config.rate_hz},
            "channels": [
                {
                    "id": c.id,
                    "label": c.label,
                    "unit": c.unit,
                    "group": c.group,
                    "derived": c.derived,
                    "calibration": {"type": c.calibration.type, **c.calibration.params},
                    "tare": c.tare,
                }
                for c in self.config.channels
            ],
        }
        path = self.config.data_dir / (Path(run.filename).stem + ".meta.json")
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for meta_path in sorted(self.config.data_dir.glob("*.meta.json"), reverse=True):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entry = meta.get("run", {})
            csv_path = self.config.data_dir / entry.get("filename", "")
            entry["exists"] = csv_path.exists()
            entry["sizeBytes"] = csv_path.stat().st_size if csv_path.exists() else 0
            runs.append(entry)
        return runs
