"""Load and validate config.yaml into the typed objects the rest of the app uses."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .expr import Expression, ExpressionError

# Categorical palette, validated for colour-vision deficiency on adjacent pairs.
# Slots are assigned per chart in declaration order and never recycled.
PALETTE_LIGHT = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
    "#e87ba4", "#008300", "#4a3aa7", "#e34948",
]
PALETTE_DARK = [
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
]
MAX_SERIES_PER_CHART = len(PALETTE_LIGHT)

UNIT_DISPLAY = {
    "degC": "°C",
    "degF": "°F",
    "ohm": "Ω",
    "m3/s": "m³/s",
}

# Volumetric flow -> m^3/s, for the mass-flow term in derived expressions.
FLOW_TO_M3S = {
    "L/min": 1.0 / 60000.0,
    "l/min": 1.0 / 60000.0,
    "LPM": 1.0 / 60000.0,
    "m3/s": 1.0,
    "GPM": 6.30902e-5,
    "gpm": 6.30902e-5,
}


class ConfigError(ValueError):
    """Raised when config.yaml is structurally invalid."""


@dataclass
class Calibration:
    """Converts a raw value from the Arduino into engineering units."""

    type: str = "linear"
    params: dict[str, float] = field(default_factory=dict)

    def apply(self, raw: float) -> float | None:
        p = self.params
        try:
            if self.type == "linear":
                return raw * p.get("gain", 1.0) + p.get("offset", 0.0)

            if self.type == "map":
                span_in = p["in_max"] - p["in_min"]
                if span_in == 0:
                    return None
                frac = (raw - p["in_min"]) / span_in
                return p["out_min"] + frac * (p["out_max"] - p["out_min"])

            if self.type == "steinhart":
                adc_max = p.get("adc_max", 1023.0)
                if raw <= 0 or raw >= adc_max:
                    return None
                # Thermistor as the lower leg of the divider by default.
                if p.get("thermistor_to_ground", True):
                    resistance = p["r_series"] * raw / (adc_max - raw)
                else:
                    resistance = p["r_series"] * (adc_max - raw) / raw
                if resistance <= 0:
                    return None
                ln_r = math.log(resistance)
                inv_t = p["a"] + p["b"] * ln_r + p["c"] * ln_r**3
                if inv_t == 0:
                    return None
                return 1.0 / inv_t - 273.15

            if self.type == "kfactor":
                k = p["k_pulses_per_litre"]
                if k == 0:
                    return None
                return raw * 60.0 / k  # pulses/s -> L/min
        except (KeyError, ZeroDivisionError, ValueError, OverflowError):
            return None

        raise ConfigError(f"unknown calibration type {self.type!r}")

    @classmethod
    def parse(cls, raw: Any, channel_id: str) -> "Calibration":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError(f"channel {channel_id}: calibration must be a mapping")
        data = dict(raw)
        cal_type = data.pop("type", "linear")
        required = {
            "linear": (),
            "map": ("in_min", "in_max", "out_min", "out_max"),
            "steinhart": ("r_series", "a", "b", "c"),
            "kfactor": ("k_pulses_per_litre",),
        }
        if cal_type not in required:
            raise ConfigError(
                f"channel {channel_id}: unknown calibration type {cal_type!r}; "
                f"expected one of {', '.join(sorted(required))}"
            )
        missing = [k for k in required[cal_type] if k not in data]
        if missing:
            raise ConfigError(
                f"channel {channel_id}: calibration {cal_type} needs {', '.join(missing)}"
            )
        return cls(type=cal_type, params={k: float(v) for k, v in data.items()})


@dataclass
class Channel:
    id: str
    label: str
    unit: str
    group: str = "general"
    derived: bool = False
    calibration: Calibration = field(default_factory=Calibration)
    expression: Expression | None = None
    warn_above: float | None = None
    warn_below: float | None = None
    alarm_above: float | None = None
    alarm_below: float | None = None
    decimals: int = 2
    sim: dict[str, float] = field(default_factory=dict)
    tare: float = 0.0

    @property
    def unit_display(self) -> str:
        return UNIT_DISPLAY.get(self.unit, self.unit)

    def convert(self, raw: float) -> float | None:
        value = self.calibration.apply(raw)
        if value is None:
            return None
        return value - self.tare

    def status(self, value: float | None) -> str:
        """One of ok / warning / critical / none, for the UI's status colours."""
        if value is None:
            return "none"
        if self.alarm_above is not None and value >= self.alarm_above:
            return "critical"
        if self.alarm_below is not None and value <= self.alarm_below:
            return "critical"
        if self.warn_above is not None and value >= self.warn_above:
            return "warning"
        if self.warn_below is not None and value <= self.warn_below:
            return "warning"
        return "ok"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "unit": self.unit,
            "unitDisplay": self.unit_display,
            "group": self.group,
            "derived": self.derived,
            "decimals": self.decimals,
            "tare": self.tare,
            "limits": {
                "warnAbove": self.warn_above,
                "warnBelow": self.warn_below,
                "alarmAbove": self.alarm_above,
                "alarmBelow": self.alarm_below,
            },
        }


@dataclass
class ChartSpec:
    title: str
    unit: str
    channel_ids: list[str]
    colors_light: list[str]
    colors_dark: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "unit": UNIT_DISPLAY.get(self.unit, self.unit),
            "channels": self.channel_ids,
            "colorsLight": self.colors_light,
            "colorsDark": self.colors_dark,
        }


@dataclass
class RigConfig:
    path: Path
    name: str
    subtitle: str
    fluid_name: str
    density_kg_m3: float
    cp_j_kgk: float
    flow_channel_id: str | None
    source: str
    rate_hz: float
    serial_port: str
    serial_baud: int
    channels: list[Channel]
    tiles: list[str]
    charts: list[ChartSpec]
    data_dir: Path
    buffer_samples: int

    @property
    def measured(self) -> list[Channel]:
        return [c for c in self.channels if not c.derived]

    @property
    def derived(self) -> list[Channel]:
        return [c for c in self.channels if c.derived]

    def by_id(self, channel_id: str) -> Channel | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None

    def flow_to_m3s(self, value: float) -> float | None:
        """Convert the designated flow channel's reading to m^3/s."""
        if self.flow_channel_id is None:
            return None
        channel = self.by_id(self.flow_channel_id)
        if channel is None:
            return None
        factor = FLOW_TO_M3S.get(channel.unit)
        if factor is None:
            return None
        return value * factor

    def to_json(self) -> dict[str, Any]:
        return {
            "rig": {"name": self.name, "subtitle": self.subtitle},
            "fluid": {
                "name": self.fluid_name,
                "density": self.density_kg_m3,
                "cp": self.cp_j_kgk,
                "flowChannel": self.flow_channel_id,
            },
            "acquisition": {"source": self.source, "rateHz": self.rate_hz},
            "channels": [c.to_json() for c in self.channels],
            "tiles": self.tiles,
            "charts": [c.to_json() for c in self.charts],
        }


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc


def _decimals_for(unit: str, group: str) -> int:
    if unit in {"W", "Pa", "RPM"}:
        return 0
    if group == "performance":
        return 3
    return 2


def _parse_channel(raw: Any, index: int, derived: bool) -> Channel:
    section = "derived" if derived else "channels"
    if not isinstance(raw, dict):
        raise ConfigError(f"{section}[{index}] must be a mapping")
    if "id" not in raw:
        raise ConfigError(f"{section}[{index}] is missing an 'id'")

    channel_id = str(raw["id"])
    if not channel_id.isidentifier():
        raise ConfigError(
            f"channel id {channel_id!r} must be a valid identifier "
            "(letters, digits, underscore; not starting with a digit) "
            "so it can be used in derived expressions"
        )

    unit = str(raw.get("unit", ""))
    group = str(raw.get("group", "general"))

    expression = None
    if derived:
        if "expr" not in raw:
            raise ConfigError(f"derived channel {channel_id} is missing 'expr'")
        try:
            expression = Expression(str(raw["expr"]))
        except ExpressionError as exc:
            raise ConfigError(f"derived channel {channel_id}: {exc}") from exc

    limits = {}
    for key in ("warn_above", "warn_below", "alarm_above", "alarm_below"):
        if raw.get(key) is not None:
            limits[key] = _as_float(raw[key], f"channel {channel_id}.{key}")

    sim = {}
    if isinstance(raw.get("sim"), dict):
        sim = {k: _as_float(v, f"channel {channel_id}.sim.{k}") for k, v in raw["sim"].items()}

    return Channel(
        id=channel_id,
        label=str(raw.get("label", channel_id)),
        unit=unit,
        group=group,
        derived=derived,
        calibration=Calibration.parse(raw.get("calibration"), channel_id),
        expression=expression,
        decimals=int(raw.get("decimals", _decimals_for(unit, group))),
        sim=sim,
        **limits,
    )


def _build_charts(raw_charts: Any, known: set[str]) -> list[ChartSpec]:
    if not raw_charts:
        return []
    if not isinstance(raw_charts, list):
        raise ConfigError("dashboard.charts must be a list")

    charts: list[ChartSpec] = []
    for index, raw in enumerate(raw_charts):
        if not isinstance(raw, dict):
            raise ConfigError(f"dashboard.charts[{index}] must be a mapping")
        ids = [str(c) for c in raw.get("channels", [])]
        unknown = [c for c in ids if c not in known]
        if unknown:
            raise ConfigError(
                f"dashboard.charts[{index}] references undefined channels: "
                f"{', '.join(unknown)}"
            )
        if len(ids) > MAX_SERIES_PER_CHART:
            raise ConfigError(
                f"dashboard.charts[{index}] has {len(ids)} series; the validated "
                f"palette holds {MAX_SERIES_PER_CHART}. Split it into two charts "
                "rather than reusing colours."
            )
        charts.append(
            ChartSpec(
                title=str(raw.get("title", f"Chart {index + 1}")),
                unit=str(raw.get("unit", "")),
                channel_ids=ids,
                colors_light=PALETTE_LIGHT[: len(ids)],
                colors_dark=PALETTE_DARK[: len(ids)],
            )
        )
    return charts


def _validate_derived_order(channels: Iterable[Channel], extra_names: set[str]) -> None:
    """Each derived expression may only reference names defined before it."""
    available = set(extra_names)
    for channel in channels:
        if not channel.derived:
            available.add(channel.id)
            continue
        assert channel.expression is not None
        unknown = channel.expression.names - available
        if unknown:
            raise ConfigError(
                f"derived channel {channel.id} references {', '.join(sorted(unknown))}, "
                "which is not a measured channel, a built-in (rho, cp, mdot), or a "
                "derived channel defined above it. Derived channels are evaluated "
                "top to bottom."
            )
        available.add(channel.id)


def load_config(path: str | Path) -> RigConfig:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a top-level mapping")

    rig = raw.get("rig") or {}
    fluid = raw.get("fluid") or {}
    acquisition = raw.get("acquisition") or {}
    serial_cfg = acquisition.get("serial") or {}
    dashboard = raw.get("dashboard") or {}
    storage = raw.get("storage") or {}

    raw_channels = raw.get("channels") or []
    if not raw_channels:
        raise ConfigError("config.yaml defines no channels")

    channels = [_parse_channel(c, i, derived=False) for i, c in enumerate(raw_channels)]
    channels += [
        _parse_channel(c, i, derived=True) for i, c in enumerate(raw.get("derived") or [])
    ]

    seen: set[str] = set()
    for channel in channels:
        if channel.id in seen:
            raise ConfigError(f"duplicate channel id {channel.id!r}")
        seen.add(channel.id)

    _validate_derived_order(channels, {"rho", "cp", "mdot"})

    flow_channel_id = fluid.get("flow_channel")
    if flow_channel_id is None:
        flow_channel_id = next(
            (c.id for c in channels if not c.derived and c.unit in FLOW_TO_M3S), None
        )
    elif flow_channel_id not in seen:
        raise ConfigError(f"fluid.flow_channel {flow_channel_id!r} is not a defined channel")

    tiles = [str(t) for t in dashboard.get("tiles", [])]
    unknown_tiles = [t for t in tiles if t not in seen]
    if unknown_tiles:
        raise ConfigError(
            f"dashboard.tiles references undefined channels: {', '.join(unknown_tiles)}"
        )

    source = str(acquisition.get("source", "simulator")).lower()
    if source not in {"simulator", "serial"}:
        raise ConfigError(f"acquisition.source must be 'simulator' or 'serial', got {source!r}")

    return RigConfig(
        path=path,
        name=str(rig.get("name", "DAQ")),
        subtitle=str(rig.get("subtitle", "")),
        fluid_name=str(fluid.get("name", "Water")),
        density_kg_m3=_as_float(fluid.get("density_kg_m3", 997.0), "fluid.density_kg_m3"),
        cp_j_kgk=_as_float(fluid.get("cp_j_kgk", 4180.0), "fluid.cp_j_kgk"),
        flow_channel_id=flow_channel_id,
        source=source,
        rate_hz=_as_float(acquisition.get("rate_hz", 10.0), "acquisition.rate_hz"),
        serial_port=str(serial_cfg.get("port", "auto")),
        serial_baud=int(serial_cfg.get("baud", 115200)),
        channels=channels,
        tiles=tiles,
        charts=_build_charts(dashboard.get("charts"), seen),
        data_dir=(path.parent / str(storage.get("directory", "data"))).resolve(),
        buffer_samples=int(storage.get("buffer_samples", 72000)),
    )
