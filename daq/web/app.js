/* =============================================================================
   DAQ dashboard client

   Owns a rolling in-memory copy of the stream, one uPlot per configured chart,
   the stat tiles, and the channel table. Charts share one decimated x array so
   the crosshair, the legend readout and the table all read the same instant.
   ========================================================================== */

(() => {
  "use strict";

  const BUFFER_CAP = 90000;      // samples held client side
  const TRIM_CHUNK = 5000;       // trim in chunks so we are not splicing every sample
  const MAX_PLOT_POINTS = 2500;  // decimation target, keeps redraw cost flat
  const MIN_FRAME_MS = 66;       // ~15 fps redraw ceiling
  const CHART_HEIGHT = 190;
  const STALE_AFTER_MS = 4000;

  const cursorSync = uPlot.sync("daq");

  const state = {
    config: null,
    channels: new Map(),
    order: [],
    t: [],
    series: new Map(),
    windowS: 300,
    charts: [],
    tiles: [],
    rows: new Map(),
    view: null,
    cursorIdx: null,
    lastSampleAt: 0,
    recording: null,
    recordingTimer: null,
    dirty: false,
    lastFrame: 0,
    socket: null,
    retry: 0,
  };

  const $ = (id) => document.getElementById(id);

  const STATUS_ICON = {
    ok: '<path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/>',
    warning: '<path d="M12 2 1 21h22L12 2Zm1 14h-2v2h2v-2Zm0-7h-2v5h2V9Z"/>',
    critical:
      '<path d="M8.3 2h7.4L22 8.3v7.4L15.7 22H8.3L2 15.7V8.3L8.3 2Zm3.7 4a1 1 0 0 0-1 1v6a1 1 0 0 0 2 0V7a1 1 0 0 0-1-1Zm0 10.4a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Z"/>',
    none: '<circle cx="12" cy="12" r="3.5"/>',
  };

  // ------------------------------------------------------------- formatting

  function fmt(value, decimals) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    const abs = Math.abs(value);
    if (abs !== 0 && (abs < 1e-3 || abs >= 1e6)) return value.toExponential(2);
    return value.toLocaleString(undefined, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  }

  function clockOf(unixSeconds) {
    if (!unixSeconds) return "--:--:--";
    return new Date(unixSeconds * 1000).toLocaleTimeString(undefined, { hour12: false });
  }

  function durationOf(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function statusOf(channel, value) {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return { level: "none", label: "No data" };
    }
    const lim = channel.limits || {};
    if (lim.alarmAbove !== null && lim.alarmAbove !== undefined && value >= lim.alarmAbove) {
      return { level: "critical", label: "Over" };
    }
    if (lim.alarmBelow !== null && lim.alarmBelow !== undefined && value <= lim.alarmBelow) {
      return { level: "critical", label: "Under" };
    }
    if (lim.warnAbove !== null && lim.warnAbove !== undefined && value >= lim.warnAbove) {
      return { level: "warning", label: "High" };
    }
    if (lim.warnBelow !== null && lim.warnBelow !== undefined && value <= lim.warnBelow) {
      return { level: "warning", label: "Low" };
    }
    return { level: "ok", label: "OK" };
  }

  function chip(level, label) {
    return `<span class="chip" data-status="${level}"><svg viewBox="0 0 24 24" aria-hidden="true">${STATUS_ICON[level]}</svg>${label}</span>`;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function toast(message, tone = "info") {
    const el = $("toast");
    el.textContent = message;
    el.dataset.tone = tone;
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      el.hidden = true;
    }, tone === "error" ? 6000 : 3200);
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch {
        /* response had no JSON body */
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  // ------------------------------------------------------------ data buffer

  function resetBuffers() {
    state.t = [];
    state.series = new Map(state.order.map((id) => [id, []]));
  }

  function appendSample(t, values) {
    state.t.push(t);
    for (let i = 0; i < state.order.length; i += 1) {
      const raw = values[i];
      state.series.get(state.order[i]).push(raw === null || raw === undefined ? null : raw);
    }
    if (state.t.length > BUFFER_CAP + TRIM_CHUNK) {
      state.t.splice(0, TRIM_CHUNK);
      for (const arr of state.series.values()) arr.splice(0, TRIM_CHUNK);
    }
    state.lastSampleAt = performance.now();
  }

  /** Decimated slice of the buffer for the active time window. */
  function buildView() {
    const times = state.t;
    if (times.length === 0) return { xs: [], byId: new Map(), startIndex: 0, stride: 1 };

    let start = 0;
    if (state.windowS > 0) {
      const cutoff = times[times.length - 1] - state.windowS;
      let lo = 0;
      let hi = times.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (times[mid] < cutoff) lo = mid + 1;
        else hi = mid;
      }
      start = lo;
    }

    const count = times.length - start;
    const stride = Math.max(1, Math.ceil(count / MAX_PLOT_POINTS));

    const xs = [];
    for (let i = start; i < times.length; i += stride) xs.push(times[i]);

    const byId = new Map();
    for (const id of state.order) {
      const source = state.series.get(id);
      const out = [];
      for (let i = start; i < source.length; i += stride) out.push(source[i]);
      byId.set(id, out);
    }
    return { xs, byId, startIndex: start, stride };
  }

  function valueAt(id, index) {
    const arr = state.view && state.view.byId.get(id);
    if (!arr || index === null || index === undefined || index < 0 || index >= arr.length) {
      return null;
    }
    return arr[index];
  }

  function readoutIndex() {
    if (!state.view || state.view.xs.length === 0) return null;
    return state.cursorIdx === null ? state.view.xs.length - 1 : state.cursorIdx;
  }

  // ----------------------------------------------------------------- charts

  function chartTheme() {
    return {
      ink: cssVar("--ink-2"),
      muted: cssVar("--ink-muted"),
      grid: cssVar("--grid"),
      axis: cssVar("--axis"),
      surface: cssVar("--surface"),
    };
  }

  function buildCharts() {
    const host = $("charts");
    host.innerHTML = "";
    state.charts = [];

    const dark = document.documentElement.dataset.theme === "dark";
    const theme = chartTheme();

    for (const spec of state.config.charts) {
      const colors = dark ? spec.colorsDark : spec.colorsLight;

      const el = document.createElement("div");
      el.className = "chart";
      el.innerHTML = `
        <div class="chart__head">
          <span class="chart__title">${spec.title}</span>
          <span class="chart__unit">${spec.unit}</span>
        </div>
        <div class="chart__plot"></div>
        <div class="legend"></div>`;
      host.appendChild(el);

      const plotHost = el.querySelector(".chart__plot");
      const legendHost = el.querySelector(".legend");
      const visible = new Set(spec.channels);

      const series = [
        {},
        ...spec.channels.map((id, i) => ({
          label: state.channels.get(id).label,
          stroke: colors[i],
          width: 2,
          spanGaps: false,
        })),
      ];

      const opts = {
        width: plotHost.clientWidth || 400,
        height: CHART_HEIGHT,
        padding: [10, 10, 0, 0],
        legend: { show: false },
        cursor: {
          y: false,
          sync: { key: cursorSync.key, setSeries: false },
          points: { size: 8, width: 2, fill: (u, i) => colors[i - 1] },
        },
        scales: {
          x: { time: true },
          y: { range: (u, min, max) => uPlot.rangeNum(min, max, 0.15, true) },
        },
        axes: [
          {
            stroke: theme.muted,
            font: '11px system-ui, -apple-system, "Segoe UI", sans-serif',
            grid: { stroke: theme.grid, width: 1 },
            ticks: { stroke: theme.axis, width: 1, size: 4 },
            // uPlot's default splits date and time across two rows; a live rig
            // only ever shows minutes of data, so keep it to one clock row.
            values: (u, splits) => splits.map(clockOf),
            size: 28,
          },
          {
            stroke: theme.muted,
            font: '11px system-ui, -apple-system, "Segoe UI", sans-serif',
            grid: { stroke: theme.grid, width: 1 },
            ticks: { show: false },
            size: 48,
          },
        ],
        series,
        hooks: {
          setCursor: [
            (u) => {
              const next = u.cursor.idx === null || u.cursor.idx === undefined ? null : u.cursor.idx;
              if (next !== state.cursorIdx) {
                state.cursorIdx = next;
                paintReadouts();
              }
            },
          ],
        },
      };

      const plot = new uPlot(opts, [[], ...spec.channels.map(() => [])], plotHost);
      cursorSync.sub(plot);

      const chart = { spec, plot, el, legendHost, colors, visible, items: new Map() };
      buildLegend(chart);
      state.charts.push(chart);

      new ResizeObserver(() => {
        const width = plotHost.clientWidth;
        if (width > 0) plot.setSize({ width, height: CHART_HEIGHT });
      }).observe(plotHost);
    }
  }

  function buildLegend(chart) {
    chart.legendHost.innerHTML = "";
    chart.items.clear();

    chart.spec.channels.forEach((id, i) => {
      const channel = state.channels.get(id);
      const item = document.createElement("button");
      item.type = "button";
      item.className = "legend__item";
      item.style.setProperty("--swatch", chart.colors[i]);
      item.innerHTML = `
        <span class="legend__swatch"></span>
        <span class="legend__name">${channel.label}</span>
        <span class="legend__value">--</span>`;

      item.addEventListener("click", () => {
        const on = chart.visible.has(id);
        if (on) chart.visible.delete(id);
        else chart.visible.add(id);
        item.classList.toggle("is-off", on);
        // Colours stay bound to the channel; hiding never repaints the others.
        chart.plot.setSeries(i + 1, { show: !on });
      });

      chart.legendHost.appendChild(item);
      chart.items.set(id, item.querySelector(".legend__value"));
    });
  }

  function paintCharts() {
    const xs = state.view.xs;
    for (const chart of state.charts) {
      chart.plot.setData([xs, ...chart.spec.channels.map((id) => state.view.byId.get(id) || [])]);
    }
  }

  // ------------------------------------------------------------------ tiles

  function buildTiles() {
    const host = $("tiles");
    host.innerHTML = "";
    state.tiles = [];

    for (const id of state.config.tiles) {
      const channel = state.channels.get(id);
      const el = document.createElement("article");
      el.className = "tile";
      el.innerHTML = `
        <span class="tile__label">${channel.label}</span>
        <span class="tile__value"><span class="tile__num">--</span><span class="tile__unit">${channel.unitDisplay}</span></span>
        <canvas class="tile__spark"></canvas>
        <span class="tile__flag" hidden></span>`;
      host.appendChild(el);

      state.tiles.push({
        id,
        channel,
        el,
        value: el.querySelector(".tile__value"),
        num: el.querySelector(".tile__num"),
        flag: el.querySelector(".tile__flag"),
        canvas: el.querySelector(".tile__spark"),
      });
    }
  }

  function drawSparkline(canvas, points, color) {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width === 0 || height === 0) return;

    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const clean = points.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
    if (clean.length < 2) return;

    let min = Infinity;
    let max = -Infinity;
    for (const v of clean) {
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const span = max - min || 1;
    const pad = 3;
    const usable = height - pad * 2;
    const xAt = (i) => (i / (points.length - 1)) * width;
    const yAt = (v) => pad + usable - ((v - min) / span) * usable;

    ctx.beginPath();
    let started = false;
    points.forEach((v, i) => {
      if (v === null || v === undefined || Number.isNaN(v)) {
        started = false;
        return;
      }
      if (started) ctx.lineTo(xAt(i), yAt(v));
      else {
        ctx.moveTo(xAt(i), yAt(v));
        started = true;
      }
    });

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();

    ctx.lineTo(xAt(points.length - 1), height + 2);
    ctx.lineTo(xAt(0), height + 2);
    ctx.closePath();
    ctx.globalAlpha = 0.12;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  function paintTiles(index) {
    for (const tile of state.tiles) {
      const value = valueAt(tile.id, index);
      const status = statusOf(tile.channel, value);

      tile.num.textContent = fmt(value, tile.channel.decimals);
      tile.el.dataset.status = status.level;

      if (status.level === "warning" || status.level === "critical") {
        tile.flag.hidden = false;
        tile.flag.dataset.status = status.level;
        tile.flag.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${STATUS_ICON[status.level]}</svg>${status.label}`;
      } else {
        tile.flag.hidden = true;
      }

      const points = state.view.byId.get(tile.id) || [];
      const color =
        status.level === "critical"
          ? cssVar("--critical")
          : status.level === "warning"
            ? cssVar("--warning")
            : cssVar("--accent");
      drawSparkline(tile.canvas, points, color);
    }
  }

  // ------------------------------------------------------------------ table

  function buildTable() {
    const body = $("channelTable").querySelector("tbody");
    body.innerHTML = "";
    state.rows.clear();

    // Channels inherit the colour they carry on their first chart, so the table
    // swatch and the chart line always agree.
    const dark = document.documentElement.dataset.theme === "dark";
    const colorFor = new Map();
    for (const spec of state.config.charts) {
      const colors = dark ? spec.colorsDark : spec.colorsLight;
      spec.channels.forEach((id, i) => {
        if (!colorFor.has(id)) colorFor.set(id, colors[i]);
      });
    }

    for (const channel of state.config.channels) {
      const tr = document.createElement("tr");
      const swatch = colorFor.get(channel.id);
      tr.innerHTML = `
        <td>
          <div class="ch-name">
            <span class="ch-swatch" ${swatch ? `style="--swatch:${swatch}"` : ""}></span>
            <span class="ch-label" title="${channel.label}">${channel.label}</span>
            <span class="ch-id">${channel.derived ? "=" : ""}${channel.id}</span>
          </div>
        </td>
        <td class="num"><span class="ch-value">--</span><span class="ch-unit">${channel.unitDisplay}</span></td>
        <td class="ch-status">${chip("none", "No data")}</td>
        <td>${channel.derived ? "" : `<button type="button" class="tare-btn" data-channel="${channel.id}">zero</button>`}</td>`;
      body.appendChild(tr);

      state.rows.set(channel.id, {
        channel,
        value: tr.querySelector(".ch-value"),
        status: tr.querySelector(".ch-status"),
        tare: tr.querySelector(".tare-btn"),
      });
    }

    body.addEventListener("click", onTareClick);
  }

  async function onTareClick(event) {
    const button = event.target.closest(".tare-btn");
    if (!button) return;
    const id = button.dataset.channel;
    const enabled = !button.classList.contains("is-active");
    try {
      const result = await api("/api/tare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: id, enabled }),
      });
      button.classList.toggle("is-active", enabled);
      button.textContent = enabled ? "zeroed" : "zero";
      toast(
        enabled
          ? `${id} zeroed at offset ${result.tare.toFixed(3)}`
          : `${id} offset cleared`,
      );
    } catch (error) {
      toast(`Could not zero ${id}: ${error.message}`, "error");
    }
  }

  function paintTable(index) {
    for (const [id, row] of state.rows) {
      const value = valueAt(id, index);
      const status = statusOf(row.channel, value);
      row.value.textContent = fmt(value, row.channel.decimals);
      row.status.innerHTML = chip(status.level, status.label);
    }
    const xs = state.view.xs;
    $("channelClock").textContent =
      index === null || xs.length === 0
        ? "--:--:--"
        : `${clockOf(xs[index])}${state.cursorIdx === null ? "" : " (cursor)"}`;
  }

  // -------------------------------------------------------------- rendering

  function paintReadouts() {
    if (!state.view) return;
    const index = readoutIndex();
    paintTiles(index);
    paintTable(index);
    for (const chart of state.charts) {
      for (const [id, el] of chart.items) {
        const channel = state.channels.get(id);
        el.textContent = fmt(valueAt(id, index), channel.decimals);
      }
    }
  }

  function requestRender() {
    state.dirty = true;
    if (state.frameQueued) return;
    state.frameQueued = true;
    requestAnimationFrame(frame);
  }

  function frame(now) {
    state.frameQueued = false;
    if (!state.dirty) return;
    if (now - state.lastFrame < MIN_FRAME_MS) {
      state.frameQueued = true;
      requestAnimationFrame(frame);
      return;
    }
    state.lastFrame = now;
    state.dirty = false;

    state.view = buildView();
    paintCharts();
    paintReadouts();
  }

  function markStale() {
    const stale = performance.now() - state.lastSampleAt > STALE_AFTER_MS;
    for (const tile of state.tiles) tile.value.classList.toggle("is-stale", stale);
  }

  // ------------------------------------------------------------------ state

  function applyConfig(config) {
    state.config = config;
    state.channels = new Map(config.channels.map((c) => [c.id, c]));
    state.order = config.channels.map((c) => c.id);
    resetBuffers();

    document.title = config.rig.name;
    $("rigName").textContent = config.rig.name;
    $("rigSubtitle").textContent = config.rig.subtitle;

    buildTiles();
    buildCharts();
    buildTable();
  }

  function applyState(payload) {
    const source = payload.source || {};
    $("statusDot").dataset.state = source.state || "disconnected";

    const linkLabel =
      source.kind === "simulator"
        ? "simulator"
        : source.port
          ? `${source.state} · ${source.port}`
          : source.state;
    $("vitalLink").textContent = linkLabel;
    $("vitalLink").title = source.detail || "";

    $("vitalRate").textContent = payload.rateHz ? payload.rateHz.toFixed(2) : "--";
    $("vitalSamples").textContent = (payload.received || 0).toLocaleString();

    const dropped = $("vitalDropped");
    dropped.textContent = (payload.dropped || 0).toLocaleString();
    dropped.classList.toggle("is-alert", (payload.dropped || 0) > 0);

    const select = $("sourceSelect");
    if (select.dataset.pending !== "1") {
      select.value = source.kind === "serial" ? "serial" : "simulator";
    }

    applyRecording(payload.recording);
  }

  function applyRecording(run) {
    state.recording = run;
    const bar = $("recordingBar");
    const button = $("recordBtn");

    if (!run) {
      bar.hidden = true;
      button.classList.remove("is-recording");
      $("recordLabel").textContent = "Record";
      clearInterval(state.recordingTimer);
      state.recordingTimer = null;
      return;
    }

    bar.hidden = false;
    button.classList.add("is-recording");
    $("recordLabel").textContent = "Recording";
    $("recordingName").textContent = run.name;
    $("recordingFile").textContent = run.filename;
    $("recordingSamples").textContent = (run.samples || 0).toLocaleString();

    if (!state.recordingTimer) {
      const tick = () => {
        if (!state.recording) return;
        $("recordingElapsed").textContent = durationOf(
          Date.now() / 1000 - state.recording.started_unix,
        );
      };
      tick();
      state.recordingTimer = setInterval(tick, 1000);
    }
  }

  // -------------------------------------------------------------- websocket

  function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws`);
    state.socket = socket;

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);

      if (message.type === "hello") {
        applyConfig(message.config);
        applyState(message.state);
        const history = message.history;
        for (let i = 0; i < history.t.length; i += 1) {
          appendSample(
            history.t[i],
            state.order.map((id) => history.series[id][i]),
          );
        }
        requestRender();
        loadRuns();
        return;
      }

      if (message.type === "state") {
        applyState(message);
        return;
      }

      if (message.type === "samples") {
        for (const row of message.rows) appendSample(row[0], row.slice(1));
        $("vitalRate").textContent = message.rateHz ? message.rateHz.toFixed(2) : "--";
        if (state.recording && message.recording !== null) {
          state.recording.samples = message.recording;
          $("recordingSamples").textContent = message.recording.toLocaleString();
        }
        requestRender();
      }
    });

    socket.addEventListener("close", () => {
      $("statusDot").dataset.state = "disconnected";
      $("vitalLink").textContent = "server offline";
      const delay = Math.min(1000 * 2 ** state.retry, 10000);
      state.retry += 1;
      setTimeout(connect, delay);
    });

    socket.addEventListener("open", () => {
      state.retry = 0;
    });
  }

  // ----------------------------------------------------------------- runs

  async function loadRuns() {
    try {
      const data = await api("/api/runs");
      const list = $("runsList");
      $("runsDir").textContent = data.directory;

      if (data.runs.length === 0) {
        list.innerHTML = '<li class="runs__empty">No runs recorded yet.</li>';
        return;
      }

      list.innerHTML = data.runs
        .map((run) => {
          const size = run.sizeBytes > 1e6
            ? `${(run.sizeBytes / 1e6).toFixed(1)} MB`
            : `${Math.max(1, Math.round(run.sizeBytes / 1024))} KB`;
          return `
            <li class="run">
              <span class="run__name">${escapeHtml(run.name)}</span>
              <span class="run__meta">${(run.samples || 0).toLocaleString()} samples &middot; ${durationOf(run.duration_s || 0)} &middot; ${size}</span>
              <a class="btn btn--ghost btn--small run__dl" href="/api/runs/${encodeURIComponent(run.filename)}" download>CSV</a>
            </li>`;
        })
        .join("");
    } catch (error) {
      toast(`Could not list runs: ${error.message}`, "error");
    }
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // -------------------------------------------------------------- controls

  function wireControls() {
    $("windowPicker").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-window]");
      if (!button) return;
      state.windowS = Number(button.dataset.window);
      for (const b of $("windowPicker").children) b.classList.toggle("is-active", b === button);
      requestRender();
    });

    $("recordBtn").addEventListener("click", async () => {
      if (state.recording) {
        await stopRecording();
      } else {
        $("runName").value = "";
        $("recordDialog").showModal();
      }
    });

    $("stopRecordBtn").addEventListener("click", stopRecording);
    $("cancelRecord").addEventListener("click", () => $("recordDialog").close());

    $("recordForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      $("recordDialog").close();
      try {
        const run = await api("/api/recording/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: $("runName").value,
            operator: $("runOperator").value,
            notes: $("runNotes").value,
          }),
        });
        applyRecording(run);
        toast(`Recording to ${run.filename}`);
      } catch (error) {
        toast(`Could not start recording: ${error.message}`, "error");
      }
    });

    $("sourceSelect").addEventListener("change", async (event) => {
      if (event.target.value === "serial") {
        event.target.dataset.pending = "1";
        await openSerialDialog();
      } else {
        await switchSource("simulator", null);
      }
    });

    $("cancelSerial").addEventListener("click", () => {
      $("serialDialog").close();
      $("sourceSelect").dataset.pending = "";
      $("sourceSelect").value = "simulator";
    });

    $("serialForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const chosen = $("serialDialog").querySelector("input[name=port]:checked");
      $("serialDialog").close();
      $("sourceSelect").dataset.pending = "";
      await switchSource("serial", chosen ? chosen.value : null);
    });

    $("refreshRunsBtn").addEventListener("click", loadRuns);

    $("themeBtn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem("daq-theme", next);
      } catch {
        /* private browsing; the toggle still works for this session */
      }
      buildCharts();
      buildTable();
      requestRender();
    });
  }

  async function stopRecording() {
    try {
      const run = await api("/api/recording/stop", { method: "POST" });
      applyRecording(null);
      toast(`Saved ${run.filename} (${run.samples.toLocaleString()} samples)`);
      loadRuns();
    } catch (error) {
      toast(`Could not stop recording: ${error.message}`, "error");
    }
  }

  async function switchSource(kind, port) {
    try {
      const next = await api("/api/source", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, port }),
      });
      resetBuffers();
      state.cursorIdx = null;
      applyState(next);
      requestRender();
      toast(kind === "serial" ? `Connecting to ${port || "auto-detected port"}` : "Switched to simulator");
    } catch (error) {
      toast(`Could not switch source: ${error.message}`, "error");
    }
  }

  async function openSerialDialog() {
    const host = $("portList");
    host.innerHTML = '<p class="ports__empty">Scanning ports...</p>';
    $("serialDialog").showModal();

    try {
      const data = await api("/api/ports");
      if (data.ports.length === 0) {
        host.innerHTML =
          '<p class="ports__empty">No serial ports detected. Plug the Arduino in over USB and reopen this dialog.</p>';
        return;
      }
      host.innerHTML = data.ports
        .map(
          (port, i) => `
          <label class="port">
            <input type="radio" name="port" value="${escapeHtml(port.device)}" ${i === 0 ? "checked" : ""}>
            <span>
              <span class="port__name">${escapeHtml(port.device)}</span><br>
              <span class="port__desc">${escapeHtml(port.description || "Unknown device")}</span>
            </span>
            ${port.likelyBoard ? '<span class="port__badge">likely board</span>' : ""}
          </label>`,
        )
        .join("");
    } catch (error) {
      host.innerHTML = `<p class="ports__empty">Could not list ports: ${escapeHtml(error.message)}</p>`;
    }
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    try {
      const saved = localStorage.getItem("daq-theme");
      if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
    } catch {
      /* storage unavailable; keep the default dark theme */
    }

    wireControls();
    connect();
    setInterval(markStale, 1000);
  }

  boot();
})();
