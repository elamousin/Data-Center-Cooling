/* =============================================================================
   ADALM2000 bench test client

   Fields are discovered from whatever the sketch prints ("ADC: 512 Voltage:
   2.502 V" -> ADC, Voltage), so each gets a tile and a chart automatically.
   Captured set points compare the ADALM output against the Arduino reading
   and are fitted for offset, gain error and linearity.
   ========================================================================== */

(() => {
  "use strict";

  const PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
  const PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const CHART_HEIGHT = 200;
  const MAX_PLOT_POINTS = 2500;
  const RAW_LINES = 300;
  const POINTS_KEY = "adalm-points";
  const DEFAULT_LSB = 5 / 1023; // Uno: 10-bit ADC against 5 V

  const state = {
    fields: [],
    units: {},
    t: [],
    series: new Map(),
    windowS: 120,
    charts: new Map(),
    tiles: new Map(),
    points: [],
    rawPaused: false,
    dirty: false,
    retry: 0,
    link: "disconnected",
  };

  const $ = (id) => document.getElementById(id);
  const dark = () => document.documentElement.dataset.theme === "dark";
  const colorOf = (i) => (dark() ? PALETTE_DARK : PALETTE_LIGHT)[i % PALETTE_LIGHT.length];
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ------------------------------------------------------------- helpers

  function fmt(value, decimals = 3) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    return value.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }

  function decimalsFor(field) {
    const values = state.series.get(field) || [];
    const last = values[values.length - 1];
    return last !== undefined && last !== null && Number.isInteger(last) && !/volt/i.test(field) ? 0 : 3;
  }

  function clockOf(seconds) {
    return new Date(seconds * 1000).toLocaleTimeString(undefined, { hour12: false });
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function toast(message, tone = "info") {
    const el = $("toast");
    el.textContent = message;
    el.dataset.tone = tone;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, tone === "error" ? 7000 : 3200);
  }

  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || response.statusText);
    return data;
  }

  function stats(values) {
    const clean = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
    const n = clean.length;
    if (n === 0) return null;
    let sum = 0, min = Infinity, max = -Infinity;
    for (const v of clean) { sum += v; if (v < min) min = v; if (v > max) max = v; }
    const mean = sum / n;
    let sq = 0;
    for (const v of clean) sq += (v - mean) ** 2;
    return { n, mean, min, max, std: n > 1 ? Math.sqrt(sq / (n - 1)) : 0 };
  }

  /** Index of the first sample inside the last `seconds`. */
  function windowStart(seconds) {
    const times = state.t;
    if (!seconds || times.length === 0) return 0;
    const cutoff = times[times.length - 1] - seconds;
    let lo = 0, hi = times.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] < cutoff) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  const voltageField = () =>
    state.fields.find((f) => state.units[f] === "V") || state.fields.find((f) => /volt|^v$/i.test(f)) || state.fields[0];
  const adcField = () => state.fields.find((f) => /adc|raw|count|reading/i.test(f) && f !== voltageField());

  /** Volts per count, taken from the sketch's own conversion when both fields exist. */
  function lsb() {
    const v = voltageField(), a = adcField();
    if (!v || !a) return DEFAULT_LSB;
    const sv = stats(state.series.get(v).slice(-200));
    const sa = stats(state.series.get(a).slice(-200));
    if (!sv || !sa || sa.mean < 20) return DEFAULT_LSB;
    return sv.mean / sa.mean;
  }

  // --------------------------------------------------------------- data

  function reset(snapshot) {
    state.fields = [...(snapshot.fields || [])];
    state.units = { ...(snapshot.units || {}) };
    state.t = [...(snapshot.t || [])];
    state.series = new Map(state.fields.map((f) => [f, [...(snapshot.series?.[f] || [])]]));
    rebuild();
    $("raw").innerHTML = "";
    for (const [t, text] of snapshot.raw || []) appendRaw(t, text, true);
  }

  function addFields(fields) {
    let added = false;
    for (const f of fields) {
      if (!state.series.has(f)) {
        state.series.set(f, new Array(state.t.length).fill(null));
        state.fields.push(f);
        added = true;
      }
    }
    if (added) rebuild();
  }

  function ingest(message) {
    appendRaw(message.t, message.text, message.values !== null);
    if (!message.values) return;
    if (message.units) Object.assign(state.units, message.units);
    addFields(message.fields || Object.keys(message.values));
    state.t.push(message.t);
    for (const f of state.fields) state.series.get(f).push(message.values[f] ?? null);
    requestRender();
  }

  function appendRaw(t, text, parsed) {
    const el = $("raw");
    const line = document.createElement("div");
    line.innerHTML = `<span class="ts">${clockOf(t)}</span>  <span class="${parsed ? "" : "bad"}">${escapeHtml(text)}</span>`;
    el.appendChild(line);
    while (el.childElementCount > RAW_LINES) el.firstElementChild.remove();
    if (!state.rawPaused) el.scrollTop = el.scrollHeight;
  }

  // -------------------------------------------------------------- build

  function rebuild() {
    buildTiles();
    buildCharts();
    buildMeasuredSelect();
    renderPoints();
    requestRender();
  }

  function buildTiles() {
    const host = $("tiles");
    host.innerHTML = "";
    state.tiles.clear();
    if (state.fields.length === 0) {
      host.innerHTML = '<p class="bench__empty">Waiting for data. Connect to the Arduino to start.</p>';
      return;
    }
    for (const field of state.fields) {
      const el = document.createElement("article");
      el.className = "tile";
      el.innerHTML = `
        <span class="tile__label">${escapeHtml(field)}</span>
        <span class="tile__value"><span class="tile__num">--</span><span class="tile__unit">${escapeHtml(state.units[field] || "")}</span></span>
        <span class="tile__stats"></span>`;
      host.appendChild(el);
      state.tiles.set(field, {
        value: el.querySelector(".tile__value"),
        num: el.querySelector(".tile__num"),
        stats: el.querySelector(".tile__stats"),
      });
    }
  }

  function buildCharts() {
    const host = $("charts");
    for (const chart of state.charts.values()) chart.plot.destroy();
    host.innerHTML = "";
    state.charts.clear();

    const axisStyle = {
      stroke: cssVar("--ink-muted"),
      font: '11px system-ui, -apple-system, "Segoe UI", sans-serif',
      grid: { stroke: cssVar("--grid"), width: 1 },
    };

    state.fields.forEach((field, i) => {
      const el = document.createElement("div");
      el.className = "chart";
      el.innerHTML = `
        <div class="chart__head">
          <span class="chart__title">${escapeHtml(field)}</span>
          <span class="chart__unit">${escapeHtml(state.units[field] || "")}</span>
        </div>
        <div class="chart__plot"></div>`;
      host.appendChild(el);
      const plotHost = el.querySelector(".chart__plot");

      const plot = new uPlot(
        {
          width: plotHost.clientWidth || 400,
          height: CHART_HEIGHT,
          padding: [10, 10, 0, 0],
          legend: { show: false },
          cursor: { y: false, points: { size: 7, fill: colorOf(i) } },
          scales: {
            x: { time: true },
            y: { range: (u, min, max) => uPlot.rangeNum(min, max, 0.15, true) },
          },
          axes: [
            { ...axisStyle, ticks: { stroke: cssVar("--axis"), width: 1, size: 4 }, values: (u, s) => s.map(clockOf), size: 28 },
            { ...axisStyle, ticks: { show: false }, size: 52 },
          ],
          series: [{}, { label: field, stroke: colorOf(i), width: 2, points: { show: false } }],
        },
        [[], []],
        plotHost,
      );
      new ResizeObserver(() => {
        if (plotHost.clientWidth > 0) plot.setSize({ width: plotHost.clientWidth, height: CHART_HEIGHT });
      }).observe(plotHost);
      state.charts.set(field, { plot });
    });
  }

  function buildMeasuredSelect() {
    const select = $("measuredSelect");
    const previous = select.value;
    select.innerHTML = state.fields
      .map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(f)}${state.units[f] ? ` (${escapeHtml(state.units[f])})` : ""}</option>`)
      .join("");
    select.value = state.fields.includes(previous) ? previous : voltageField() || "";
  }

  // ------------------------------------------------------------- render

  function requestRender() {
    if (state.dirty) return;
    state.dirty = true;
    requestAnimationFrame(render);
  }

  function render() {
    state.dirty = false;
    const start = windowStart(state.windowS);
    const stride = Math.max(1, Math.ceil((state.t.length - start) / MAX_PLOT_POINTS));
    const xs = [];
    for (let i = start; i < state.t.length; i += stride) xs.push(state.t[i]);

    for (const field of state.fields) {
      const values = state.series.get(field);
      const windowed = values.slice(start);
      const ys = [];
      for (let i = start; i < values.length; i += stride) ys.push(values[i]);
      state.charts.get(field)?.plot.setData([xs, ys]);

      const tile = state.tiles.get(field);
      if (!tile) continue;
      const d = decimalsFor(field);
      tile.num.textContent = fmt(values[values.length - 1], d);
      const s = stats(windowed);
      tile.stats.innerHTML = s
        ? `<span>mean <b>${fmt(s.mean, d + 1)}</b></span><span>&sigma; <b>${fmt(s.std, d + 1)}</b></span><span>min <b>${fmt(s.min, d)}</b></span><span>max <b>${fmt(s.max, d)}</b></span>`
        : "";
    }

    // Rate from the last ~10 s of arrivals.
    const recent = windowStart(10);
    const span = state.t.length - recent > 1 ? state.t[state.t.length - 1] - state.t[recent] : 0;
    $("vitalRate").textContent = span > 0 ? ((state.t.length - recent - 1) / span).toFixed(2) : "--";
    $("vitalLines").textContent = state.t.length.toLocaleString();
  }

  function markStale() {
    const last = state.t[state.t.length - 1];
    const stale = !last || Date.now() / 1000 - last > 5;
    for (const tile of state.tiles.values()) tile.value.classList.toggle("is-stale", stale);
  }

  // ------------------------------------------------------------ findings

  function capture(event) {
    event.preventDefault();
    const set = parseFloat($("setInput").value);
    const field = $("measuredSelect").value;
    if (Number.isNaN(set) || !field) return;
    if (state.link !== "connected") {
      toast("Not connected - nothing to capture", "error");
      return;
    }

    const seconds = Number($("avgSelect").value);
    const start = windowStart(seconds);
    const measured = stats(state.series.get(field).slice(start));
    if (!measured || measured.n < 2) {
      toast(`Need at least 2 samples in the last ${seconds} s`, "error");
      return;
    }
    const adc = adcField();
    const adcStats = adc ? stats(state.series.get(adc).slice(start)) : null;
    const step = lsb();

    state.points.push({
      set,
      field,
      unit: state.units[field] || "",
      measured: measured.mean,
      std: measured.std,
      n: measured.n,
      adc: adcStats ? adcStats.mean : null,
      adcStd: adcStats ? adcStats.std : null,
      adcIdeal: set / step,
      lsb: step,
      at: new Date().toISOString(),
    });
    state.points.sort((a, b) => a.set - b.set);
    savePoints();
    renderPoints();
    toast(`Captured ${fmt(set, 3)} V -> ${fmt(measured.mean, 4)} (${measured.n} samples)`);
    $("setInput").select();
  }

  /** Least squares measured = gain * set + offset. */
  function fitPoints(points) {
    const n = points.length;
    if (n < 2) return null;
    const mx = points.reduce((a, p) => a + p.set, 0) / n;
    const my = points.reduce((a, p) => a + p.measured, 0) / n;
    let sxx = 0, sxy = 0, syy = 0;
    for (const p of points) {
      sxx += (p.set - mx) ** 2;
      sxy += (p.set - mx) * (p.measured - my);
      syy += (p.measured - my) ** 2;
    }
    if (sxx === 0) return null;
    const gain = sxy / sxx;
    const offset = my - gain * mx;
    const r2 = syy === 0 ? 1 : (sxy * sxy) / (sxx * syy);
    const inl = Math.max(...points.map((p) => Math.abs(p.measured - (gain * p.set + offset))));
    return { gain, offset, r2, inl };
  }

  function renderPoints() {
    const body = $("pointsTable").querySelector("tbody");
    const step = state.points.length ? state.points[state.points.length - 1].lsb : lsb();

    if (state.points.length === 0) {
      body.innerHTML = '<tr class="empty"><td colspan="8">No points yet. Set the ADALM output, then capture.</td></tr>';
    } else {
      body.innerHTML = state.points
        .map((p, i) => {
          const errMv = (p.measured - p.set) * 1000;
          const high = Math.abs(errMv) > 2 * p.lsb * 1000;
          return `<tr>
            <td class="num">${fmt(p.set, 3)}</td>
            <td class="num">${fmt(p.measured, 4)}</td>
            <td class="num ${high ? "err-high" : ""}">${errMv >= 0 ? "+" : ""}${fmt(errMv, 1)}</td>
            <td class="num">${p.adc === null ? "--" : fmt(p.adc, 1)}</td>
            <td class="num">${fmt(p.adcIdeal, 1)}</td>
            <td class="num">${fmt(p.std * 1000, 2)} m${escapeHtml(p.unit)}</td>
            <td class="num">${p.n}</td>
            <td><button type="button" class="row-x" data-index="${i}" aria-label="Remove point">&times;</button></td>
          </tr>`;
        })
        .join("");
    }

    const fit = fitPoints(state.points);
    const maxErr = state.points.length
      ? Math.max(...state.points.map((p) => Math.abs(p.measured - p.set))) * 1000
      : null;
    const cells = [];
    if (state.points.length) {
      cells.push(["Points", state.points.length, ""]);
      cells.push(["Max |error|", fmt(maxErr, 1), "mV"]);
      cells.push(["1 LSB", fmt(step * 1000, 2), "mV"]);
    }
    if (fit) {
      cells.push(["Offset", `${fit.offset >= 0 ? "+" : ""}${fmt(fit.offset * 1000, 1)}`, "mV"]);
      cells.push(["Gain error", `${fit.gain >= 1 ? "+" : ""}${fmt((fit.gain - 1) * 100, 2)}`, "%"]);
      cells.push(["Nonlinearity", fmt(fit.inl / step, 2), "LSB"]);
      cells.push(["R²", fit.r2.toFixed(5), ""]);
    }
    $("fit").innerHTML = cells
      .map(([k, v, u]) => `<div><dt>${k}</dt><dd>${v}${u ? `<small>${u}</small>` : ""}</dd></div>`)
      .join("");
  }

  function savePoints() {
    try { localStorage.setItem(POINTS_KEY, JSON.stringify(state.points)); } catch { /* storage unavailable */ }
  }

  function loadPoints() {
    try {
      const saved = JSON.parse(localStorage.getItem(POINTS_KEY) || "[]");
      if (Array.isArray(saved)) state.points = saved;
    } catch { state.points = []; }
  }

  // --------------------------------------------------------------- export

  function download(filename, rows) {
    const csv = rows.map((r) => r.map((v) => (v === null || v === undefined ? "" : String(v))).join(",")).join("\n");
    const url = URL.createObjectURL(new Blob([csv + "\n"], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: filename });
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  function exportPoints() {
    if (!state.points.length) return toast("No points to export", "error");
    download(`adalm_points_${stamp()}.csv`, [
      ["captured_at", "set_V", "measured_field", "measured_mean", "error_mV", "measured_std", "adc_mean", "adc_std", "adc_ideal", "lsb_V", "samples"],
      ...state.points.map((p) => [
        p.at, p.set, p.field, p.measured.toFixed(6), ((p.measured - p.set) * 1000).toFixed(3), p.std.toFixed(6),
        p.adc === null ? "" : p.adc.toFixed(3), p.adcStd === null ? "" : p.adcStd.toFixed(3),
        p.adcIdeal.toFixed(3), p.lsb.toFixed(8), p.n,
      ]),
    ]);
  }

  function exportStream() {
    if (!state.t.length) return toast("No data yet", "error");
    download(`adalm_session_${stamp()}.csv`, [
      ["timestamp", "t_unix", ...state.fields],
      ...state.t.map((t, i) => [new Date(t * 1000).toISOString(), t.toFixed(3), ...state.fields.map((f) => state.series.get(f)[i])]),
    ]);
  }

  // ----------------------------------------------------------- connection

  function applyLink(info) {
    state.link = info.state;
    $("statusDot").dataset.state = info.state;
    $("linkDetail").textContent = info.detail || info.state;
    $("vitalPort").textContent = info.port ? `${info.port} @ ${info.baud}` : "--";
    const unparsed = $("vitalUnparsed");
    unparsed.textContent = (info.unparsed || 0).toLocaleString();
    const live = info.state === "connected" || info.state === "connecting";
    $("connectBtn").textContent = live ? "Disconnect" : "Connect";
    $("connectBtn").classList.toggle("btn--primary", !live);
    $("notice").hidden = info.state === "connected";
    if (info.baud) $("baudSelect").value = String(info.baud);
  }

  async function toggleConnection() {
    const button = $("connectBtn");
    button.disabled = true;
    try {
      if (state.link === "connected" || state.link === "connecting") {
        applyLink(await api("/api/adalm/disconnect", {}));
      } else {
        const info = await api("/api/adalm/connect", {
          port: $("portSelect").value || null,
          baud: Number($("baudSelect").value),
        });
        applyLink(info);
        toast(`Connected to ${info.port}. The Uno resets on connect; data starts in ~2 s.`);
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function loadPorts() {
    try {
      const { ports } = await api("/api/ports");
      const select = $("portSelect");
      const previous = select.value;
      select.innerHTML =
        '<option value="">Auto</option>' +
        ports
          .map((p) => `<option value="${escapeHtml(p.device)}">${escapeHtml(p.device)} - ${escapeHtml(p.description || "unknown")}</option>`)
          .join("");
      const board = ports.find((p) => p.likelyBoard);
      select.value = previous || (board ? board.device : "");
    } catch (error) {
      toast(`Could not list ports: ${error.message}`, "error");
    }
  }

  function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws/adalm`);

    socket.addEventListener("open", () => { state.retry = 0; });
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "hello" || message.type === "reset") {
        applyLink(message);
        reset(message);
      } else if (message.type === "state") {
        applyLink(message);
      } else if (message.type === "line") {
        if (message.values === null) {
          const el = $("vitalUnparsed");
          el.textContent = (Number(el.textContent.replace(/\D/g, "")) + 1).toLocaleString();
        }
        ingest(message);
      }
    });
    socket.addEventListener("close", () => {
      applyLink({ state: "disconnected", detail: "Server offline - is python -m daq running?" });
      setTimeout(connect, Math.min(1000 * 2 ** state.retry++, 10000));
    });
  }

  // ----------------------------------------------------------------- boot

  function wire() {
    $("connectBtn").addEventListener("click", toggleConnection);
    $("portSelect").addEventListener("focus", loadPorts);
    $("captureForm").addEventListener("submit", capture);
    $("exportPointsBtn").addEventListener("click", exportPoints);
    $("exportStreamBtn").addEventListener("click", exportStream);
    $("clearPointsBtn").addEventListener("click", () => {
      if (state.points.length && !confirm(`Remove all ${state.points.length} captured points?`)) return;
      state.points = [];
      savePoints();
      renderPoints();
    });
    $("pointsTable").addEventListener("click", (event) => {
      const button = event.target.closest(".row-x");
      if (!button) return;
      state.points.splice(Number(button.dataset.index), 1);
      savePoints();
      renderPoints();
    });
    $("pauseRawBtn").addEventListener("click", (event) => {
      state.rawPaused = !state.rawPaused;
      event.target.textContent = state.rawPaused ? "Resume" : "Pause";
    });
    $("windowPicker").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-window]");
      if (!button) return;
      state.windowS = Number(button.dataset.window);
      for (const b of $("windowPicker").children) b.classList.toggle("is-active", b === button);
      requestRender();
    });
    $("themeBtn").addEventListener("click", () => {
      const next = dark() ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("daq-theme", next); } catch { /* storage unavailable */ }
      buildCharts();
      requestRender();
    });
  }

  function boot() {
    try {
      const saved = localStorage.getItem("daq-theme");
      if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
    } catch { /* keep default theme */ }
    loadPoints();
    wire();
    renderPoints();
    loadPorts();
    connect();
    setInterval(markStale, 1000);
  }

  boot();
})();
