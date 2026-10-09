(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const NS = "http://www.w3.org/2000/svg";
  const REFRESH_MS = 2000;
  const SLOW_REFRESH_MS = 15000;
  const STALE_AFTER_MS = 10000;
  const VIEWER_ID = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let leaving = false;
  const RANGE_LABELS = { "1h": "Last hour", "24h": "Last 24 hours", "7d": "Last 7 days" };
  const PAGE_LABELS = { overview: "Overview", performance: "Performance", resources: "Resources", activity: "Activity" };

  const state = {
    page: "overview",
    range: "1h",
    status: null,
    history: [],
    events: [],
    statusError: null,
    historyError: null,
    eventsError: null,
    statusReceivedAt: null,
    historyReceivedAt: null,
    eventsReceivedAt: null,
    statusInFlight: false,
    historyInFlight: false,
    eventsInFlight: false,
    manualRefreshInFlight: false
  };

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function text(value, fallback = "—") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function number(value, digits = 0) {
    if (!finite(value)) return "—";
    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits
    }).format(value);
  }

  function integer(value) {
    return number(value, 0);
  }

  function compactNumber(value) {
    if (!finite(value)) return "—";
    return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }

  function ratioPercent(value) {
    if (!finite(value)) return null;
    return Math.abs(value) <= 1 ? value * 100 : value;
  }

  function percent(value, digits = 0) {
    const converted = ratioPercent(value);
    return finite(converted) ? `${number(converted, digits)}%` : "—";
  }

  function rawPercent(value, digits = 0) {
    return finite(value) ? `${number(value, digits)}%` : "—";
  }

  function rate(value, digits = 1) {
    return finite(value) ? number(value, digits) : "—";
  }

  function bytes(value, digits = 1) {
    if (!finite(value)) return "—";
    const absolute = Math.abs(value);
    if (absolute === 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let scaled = value;
    let index = 0;
    while (Math.abs(scaled) >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    const shownDigits = index === 0 ? 0 : digits;
    return `${number(scaled, shownDigits)} ${units[index]}`;
  }

  function dataRate(value) {
    if (!finite(value)) return "—";
    const absolute = Math.abs(value);
    if (absolute < 1024) return `${number(value, 0)} B/s`;
    if (absolute < 1024 * 1024) return `${number(value / 1024, 1)} KiB/s`;
    if (absolute < 1024 * 1024 * 1024) return `${number(value / (1024 * 1024), 1)} MiB/s`;
    return `${number(value / (1024 * 1024 * 1024), 2)} GiB/s`;
  }

  function gib(value) {
    return finite(value) ? `${number(value, 1)} GiB` : "—";
  }

  function timestampMillis(value) {
    if (value instanceof Date) return value.getTime();
    if (finite(value)) return value < 100000000000 ? value * 1000 : value;
    if (typeof value === "string" && value.trim()) {
      const parsed = Date.parse(value);
      return Number.isNaN(parsed) ? null : parsed;
    }
    return null;
  }

  function ageSeconds(value, now = Date.now()) {
    const stamp = timestampMillis(value);
    if (!finite(stamp)) return null;
    return Math.max(0, (now - stamp) / 1000);
  }

  function relativeTime(value) {
    const age = ageSeconds(value);
    if (age === null) return "—";
    if (age < 5) return "just now";
    if (age < 60) return `${Math.floor(age)}s ago`;
    if (age < 3600) return `${Math.floor(age / 60)}m ago`;
    if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
    return `${Math.floor(age / 86400)}d ago`;
  }

  function clockTime(value) {
    const stamp = timestampMillis(value);
    if (!finite(stamp)) return "—";
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(stamp);
  }

  function cleanMessage(value, fallback = "No detail available") {
    const result = text(value, fallback).replace(/[\r\n]+/g, " ").trim();
    return result.length > 160 ? `${result.slice(0, 157)}…` : result;
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = text(value);
  }

  function setProgress(id, value) {
    const node = document.getElementById(id);
    if (!node) return;
    if (!finite(value)) {
      node.style.width = "0%";
      node.removeAttribute("aria-valuenow");
      node.parentElement?.classList.add("is-empty");
      return;
    }
    const clamped = Math.max(0, Math.min(100, value));
    node.style.width = `${clamped}%`;
    node.setAttribute("aria-valuenow", String(Math.round(clamped)));
    node.parentElement?.classList.remove("is-empty");
  }

  function host() {
    return state.status?.host || {};
  }

  function model() {
    return state.status?.model || {};
  }

  function source(name) {
    return state.status?.sources?.[name] || null;
  }

  function modelName(id) {
    if (!id) return "Qwen 3.8 Flash Next";
    return String(id).replace(/[-_]+/g, " ").replace(/^qwen/i, "Qwen").replace(/\bflash\b/i, "Flash").replace(/\bnext\b/i, "Next").replace(/\s+/g, " ").trim();
  }

  function contextLabel(value) {
    if (!finite(value)) return "—";
    return `${integer(value)} tokens`;
  }

  function uptime(value) {
    if (!finite(value)) return "—";
    let remaining = Math.max(0, Math.floor(value));
    const days = Math.floor(remaining / 86400);
    remaining %= 86400;
    const hours = Math.floor(remaining / 3600);
    remaining %= 3600;
    const minutes = Math.floor(remaining / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  }

  function loadLabel(value) {
    if (Array.isArray(value)) return value.filter(finite).slice(0, 3).map((item) => number(item, 2)).join(" / ") || "—";
    if (typeof value === "string") return value;
    return finite(value) ? number(value, 2) : "—";
  }

  async function requestJSON(path) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(path, {
        cache: "no-store",
        headers: { Accept: "application/json", ...(path === "/api/status" ? { "X-StrixDash-Viewer": VIEWER_ID } : {}) },
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      return await response.json();
    } finally {
      window.clearTimeout(timer);
    }
  }

  function errorMessage(error) {
    if (error?.name === "AbortError") return "The node did not respond in time.";
    return cleanMessage(error?.message, "The node could not be reached.");
  }

  async function pollStatus() {
    if (state.statusInFlight || leaving) return;
    state.statusInFlight = true;
    try {
      const payload = await requestJSON("/api/status");
      state.status = payload && typeof payload === "object" ? payload : null;
      state.statusError = state.status ? null : "The node returned no status data.";
      state.statusReceivedAt = Date.now();
    } catch (error) {
      state.statusError = errorMessage(error);
    } finally {
      state.statusInFlight = false;
      render();
    }
  }

  async function pollHistory() {
    if (state.historyInFlight) return;
    state.historyInFlight = true;
    try {
      const payload = await requestJSON(`/api/history?range=${encodeURIComponent(state.range)}`);
      state.history = Array.isArray(payload?.points) ? payload.points.filter((point) => point && typeof point === "object") : [];
      state.historyError = null;
      state.historyReceivedAt = Date.now();
    } catch (error) {
      state.historyError = errorMessage(error);
    } finally {
      state.historyInFlight = false;
      render();
    }
  }

  async function pollEvents() {
    if (state.eventsInFlight) return;
    state.eventsInFlight = true;
    try {
      const payload = await requestJSON("/api/events");
      state.events = Array.isArray(payload?.events) ? payload.events.filter((event) => event && typeof event === "object") : [];
      state.eventsError = null;
      state.eventsReceivedAt = Date.now();
    } catch (error) {
      state.eventsError = errorMessage(error);
    } finally {
      state.eventsInFlight = false;
      render();
    }
  }

  function sourceStatus(sourceData) {
    if (!sourceData) return { state: "unknown", label: "—", detail: "Waiting for data" };
    if (sourceData.paused) return { state: "unknown", label: "Paused", detail: "Waiting for the next dashboard sample" };
    const lastOkAge = ageSeconds(sourceData.last_ok);
    if (sourceData.ok === false) return { state: "error", label: "Error", detail: `${cleanMessage(sourceData.error, "Metrics unavailable")}${lastOkAge === null ? " · no last good sample" : ` · last good ${relativeTime(sourceData.last_ok)}`}` };
    if (sourceData.ok === true && (lastOkAge === null || lastOkAge <= STALE_AFTER_MS / 1000 * 2)) return { state: "ok", label: "Healthy", detail: lastOkAge === null ? "Responding" : `Last good sample ${relativeTime(sourceData.last_ok)}` };
    if (lastOkAge !== null) return { state: "stale", label: "Stale", detail: `Last good sample ${relativeTime(sourceData.last_ok)}` };
    return { state: "unknown", label: "Unknown", detail: "Waiting for a health sample" };
  }

  function connectionState() {
    if (!state.status && !state.statusError) return "connecting";
    if (state.statusError) return "offline";
    const modelData = model();
    const modelSource = source("model");
    const hardwareSource = source("hardware");
    const timestamp = state.status?.timestamp || state.statusReceivedAt;
    const stale = ageSeconds(timestamp) !== null && ageSeconds(timestamp) > STALE_AFTER_MS / 1000;
    if (modelData.online === false) return "offline";
    if (stale || modelSource?.ok === false || hardwareSource?.ok === false) return "degraded";
    return "live";
  }

  function renderConnection() {
    const connection = connectionState();
    const status = $("#connection-status");
    const label = $("#connection-label");
    const dot = $("#sidebar-status-dot");
    if (status) status.dataset.state = connection;
    if (label) label.textContent = connection === "live" ? "Live" : connection === "degraded" ? "Degraded" : connection === "offline" ? "Offline" : "Connecting";
    if (dot) dot.dataset.state = connection;
    const lastUpdate = state.status?.timestamp || state.statusReceivedAt;
    setText("last-update", state.statusError ? "Unavailable" : relativeTime(lastUpdate));
  }

  function renderBanner() {
    const banner = $("#stale-banner");
    const title = $("#stale-title");
    const detail = $("#stale-detail");
    if (!banner || !title || !detail) return;
    let message = null;
    let heading = "Data needs attention";
    let error = false;
    if (state.statusError) {
      heading = "Node disconnected";
      message = state.statusError;
      error = true;
    } else if (state.status?.model?.online === false) {
      heading = "Model service offline";
      message = "Host metrics may remain available while the inference service is stopped.";
      error = true;
    } else {
      const timestamp = state.status?.timestamp || state.statusReceivedAt;
      const sampleAge = ageSeconds(timestamp);
      const badSources = ["model", "hardware"].filter((name) => source(name)?.ok === false);
      if (sampleAge !== null && sampleAge > STALE_AFTER_MS) {
        heading = "Status sample is stale";
        message = `The last node sample was ${relativeTime(timestamp)}. Values remain visible until it responds.`;
      } else if (badSources.length) {
        heading = "Some metrics are stale";
        message = `${badSources.join(" and ")} data source${badSources.length > 1 ? "s" : ""} reported an error.`;
      }
    }
    banner.classList.toggle("is-hidden", !message);
    banner.classList.toggle("is-error", error);
    if (message) {
      title.textContent = heading;
      detail.textContent = message;
    }
  }

  function renderModel() {
    const current = model();
    setText("model-title", modelName(current.id));
    const subtitle = [];
    if (current.version !== null && current.version !== undefined) subtitle.push(`v${current.version}`);
    if (finite(current.context)) subtitle.push(contextLabel(current.context));
    setText("model-subtitle", subtitle.length ? `Inference service · ${subtitle.join(" · ")}` : "Inference service · waiting for a sample");

    const status = $("#model-state");
    const label = $("#model-state-label");
    const detail = $("#model-state-detail");
    if (!status || !label || !detail) return;
    const connection = connectionState();
    if (current.sampling_paused && !state.statusError) {
      status.dataset.state = "unknown";
      label.textContent = "Resuming monitoring";
      detail.textContent = "Waiting for the next model sample";
    } else if (!state.status && !state.statusError) {
      status.dataset.state = "unknown";
      label.textContent = "Checking service";
      detail.textContent = "No model sample yet";
    } else if (current.online === false || connection === "offline") {
      status.dataset.state = "offline";
      label.textContent = "Offline";
      detail.textContent = state.statusError ? "Node is unreachable" : "Inference service is not online";
    } else if (connection === "degraded") {
      status.dataset.state = "degraded";
      label.textContent = "Online · degraded";
      detail.textContent = finite(current.active) ? `${integer(current.active)} active · source needs attention` : "Source needs attention";
    } else {
      status.dataset.state = "online";
      label.textContent = "Online";
      detail.textContent = finite(current.active) ? current.active === 0 ? "Idle · ready for requests" : `${integer(current.active)} active request${current.active === 1 ? "" : "s"}` : "Ready for requests";
    }
  }

  function renderKpis() {
    const current = model();
    setText("output-rate", rate(current.output_tps));
    setText("output-rate-meta", finite(current.output_tps) ? `Rolling wall-clock · ${finite(current.output_window_seconds) && current.output_window_seconds > 0 ? number(current.output_window_seconds, 0) : 60}s window` : "Waiting for data");
    setText("active-requests", integer(current.active));
    setText("active-requests-unit", finite(current.active) && current.active === 0 ? "idle" : "active");
    setText("queue-meta", finite(current.queued) ? `Queue ${integer(current.queued)}${current.active === 0 && current.queued === 0 ? " · idle" : ""}` : "Queue —");
    setText("kv-occupancy", ratioPercent(current.kv_ratio) === null ? "—" : number(ratioPercent(current.kv_ratio), 1));
    const kvUsed = finite(current.kv_used) ? integer(current.kv_used) : "—";
    const kvPool = finite(current.kv_positions) ? integer(current.kv_positions) : "—";
    setText("kv-meta", finite(current.kv_used) || finite(current.kv_positions) ? `${kvUsed} reserved / ${kvPool} pool` : "KV pool —");
    setText("cache-efficiency", ratioPercent(current.cache_hit_ratio) === null ? "—" : number(ratioPercent(current.cache_hit_ratio), 1));
    setText("cache-meta", `Cache bytes ${bytes(current.cache_bytes)}`);
    setText("chart-current-rate", rate(current.output_tps));
    setText("chart-range-label", RANGE_LABELS[state.range]);
    setText("completed-requests", integer(current.completed));
    setText("queued-requests", integer(current.queued));
    setText("prompt-tokens", compactNumber(current.prompt_tokens));
    setText("output-tokens", compactNumber(current.output_tokens));
    setText("context-window", contextLabel(current.context));
    setText("kv-positions", integer(current.kv_positions));

    const gpu = host().gpu || {};
    setText("gpu-busy", rawPercent(gpu.busy_percent));
    setProgress("gpu-busy-bar", gpu.busy_percent);
    setText("gpu-temp", finite(gpu.temp_c) ? `${number(gpu.temp_c, 1)} °C` : "—");
    setText("gpu-power", finite(gpu.power_w) ? `${number(gpu.power_w, 1)} W` : "—");
    setText("gpu-clock", finite(gpu.clock_mhz) ? `${integer(gpu.clock_mhz)} MHz` : "—");
  }

  function renderAllocation() {
    const allocation = model().allocations || {};
    const weights = finite(allocation.weights_gib) ? allocation.weights_gib : null;
    const kv = finite(allocation.kv_gib) ? allocation.kv_gib : null;
    const working = finite(allocation.working_gib) ? allocation.working_gib : null;
    const values = [weights, kv, working];
    const total = values.every((item) => item === null) ? null : values.reduce((sum, item) => sum + (item || 0), 0);
    setText("allocation-total", total === null ? "—" : `${number(total, 1)} GiB total`);
    setText("allocation-weights", gib(weights));
    setText("allocation-kv", gib(kv));
    setText("allocation-working", gib(working));
    setText("allocation-source", text(model().allocations_source));
    const bar = $("#allocation-bar");
    if (bar) {
      [["weights", weights], ["kv", kv], ["working", working]].forEach(([name, value]) => {
        const segment = bar.querySelector(`[data-segment="${name}"]`);
        if (segment) segment.style.width = total && finite(value) ? `${(value / total) * 100}%` : "0%";
      });
    }
  }

  function valuesFor(key) {
    return state.history.map((point) => finite(point[key]) ? point[key] : null);
  }

  function setSparkline(id, values) {
    const svg = document.getElementById(id);
    if (!svg) return;
    const line = svg.querySelector("[data-spark-line]");
    const area = svg.querySelector("[data-spark-area]");
    const valid = values.filter(finite);
    if (!valid.length) {
      if (line) line.setAttribute("d", "");
      if (area) area.setAttribute("d", "");
      svg.classList.add("is-empty");
      return;
    }
    svg.classList.remove("is-empty");
    const min = Math.min(...valid);
    const max = Math.max(...valid);
    const span = max - min || Math.max(Math.abs(max) * .15, 1);
    const low = min - span * .1;
    const high = max + span * .1;
    const points = valid.map((value, index) => {
      const x = valid.length === 1 ? 60 : 2 + index / (valid.length - 1) * 116;
      const y = 31 - ((value - low) / (high - low)) * 27;
      return [x, Math.max(2, Math.min(34, y))];
    });
    const linePath = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
    const areaPath = `${linePath} L ${points[points.length - 1][0].toFixed(2)} 35 L ${points[0][0].toFixed(2)} 35 Z`;
    if (line) line.setAttribute("d", linePath);
    if (area) area.setAttribute("d", areaPath);
  }

  function svgElement(name, attributes = {}) {
    const node = document.createElementNS(NS, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function makeSeriesPath(values, width, height, padding, min, max) {
    let path = "";
    let open = false;
    const denominator = Math.max(1, values.length - 1);
    values.forEach((value, index) => {
      if (!finite(value)) {
        open = false;
        return;
      }
      const x = padding.left + index / denominator * (width - padding.left - padding.right);
      const y = padding.top + (1 - (value - min) / (max - min || 1)) * (height - padding.top - padding.bottom);
      path += `${open ? " L" : "M"}${x.toFixed(2)} ${Math.max(padding.top, Math.min(height - padding.bottom, y)).toFixed(2)}`;
      open = true;
    });
    return path;
  }

  function drawChart(id, series, emptyId) {
    const svg = document.getElementById(id);
    if (!svg) return;
    const viewBox = (svg.getAttribute("viewBox") || "0 0 720 250").split(/\s+/).map(Number);
    const width = viewBox[2] || 720;
    const height = viewBox[3] || 250;
    const padding = { top: 14, right: 12, bottom: 30, left: 43 };
    const grid = svg.querySelector("[data-chart-grid]");
    const seriesGroup = svg.querySelector("[data-chart-series]");
    const allValues = series.flatMap((item) => item.values).filter(finite);
    if (grid) {
      while (grid.firstChild) grid.removeChild(grid.firstChild);
      for (let index = 1; index <= 4; index += 1) {
        const y = padding.top + index / 5 * (height - padding.top - padding.bottom);
        grid.appendChild(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: y, y2: y }));
      }
    }
    if (seriesGroup) {
      while (seriesGroup.firstChild) seriesGroup.removeChild(seriesGroup.firstChild);
    }
    const hasData = allValues.length > 0;
    const minValue = hasData ? Math.min(...allValues) : 0;
    const maxValue = hasData ? Math.max(...allValues) : 1;
    const span = maxValue - minValue || Math.max(Math.abs(maxValue) * .15, 1);
    const min = Math.max(0, minValue - span * .12);
    const max = maxValue + span * .12;
    if (grid && hasData) {
      [min, (min + max) / 2, max].forEach((value) => {
        const y = padding.top + (1 - (value - min) / (max - min)) * (height - padding.top - padding.bottom);
        const label = svgElement("text", { x: padding.left - 7, y: y + 3, "text-anchor": "end", fill: "currentColor", "font-size": 10, opacity: .7 });
        label.textContent = number(value, value < 10 ? 1 : 0);
        grid.appendChild(label);
      });
      const times = state.history.map((point) => point.timestamp).filter(finite);
      [0, .5, 1].forEach((fraction) => {
        if (!times.length) return;
        const stamp = times[0] + fraction * (times[times.length - 1] - times[0]);
        const label = svgElement("text", { x: padding.left + fraction * (width - padding.left - padding.right), y: height - 6, "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle", fill: "currentColor", "font-size": 10, opacity: .7 });
        label.textContent = new Date(stamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        grid.appendChild(label);
      });
    }
    if (seriesGroup && hasData) {
      series.forEach((item) => {
        const path = svgElement("path", { "data-series": item.key, d: makeSeriesPath(item.values, width, height, padding, min, max) });
        seriesGroup.appendChild(path);
      });
    }
    const empty = document.getElementById(emptyId);
    if (empty) empty.classList.toggle("is-hidden", hasData);
  }

  function renderCharts() {
    const output = valuesFor("output_tps");
    setSparkline("spark-output", output);
    setSparkline("spark-active", valuesFor("active"));
    setSparkline("spark-kv", valuesFor("kv_ratio").map(ratioPercent));
    setSparkline("spark-cache", []);
    drawChart("overview-chart", [{ key: "output", values: output }], "overview-chart-empty");
    drawChart("performance-chart", [{ key: "output", values: output }], "performance-chart-empty");
    setText("chart-points", `${state.history.length} sample${state.history.length === 1 ? "" : "s"}`);
    setText("perf-chart-points", `${state.history.length} sample${state.history.length === 1 ? "" : "s"}`);
    setText("perf-chart-source", state.historyError ? "Unavailable" : RANGE_LABELS[state.range]);
  }

  function renderPerformance() {
    const current = model();
    const decode = current.decode_tps;
    const prefill = current.prefill_tps;
    const cacheHit = ratioPercent(current.cache_hit_ratio);
    const draft = ratioPercent(current.draft_accept_ratio);
    setText("perf-output-rate", rate(current.output_tps));
    setText("perf-decode-rate", rate(decode));
    setText("perf-prefill-rate", rate(prefill));
    setText("perf-draft-accept", draft === null ? "—" : number(draft, 1));
    setText("perf-decode-age", current.decode_measured_at ? `Measured ${relativeTime(current.decode_measured_at)}` : current.last_generation_at ? `Measured ${relativeTime(current.last_generation_at)}` : "No generation measured");
    setText("perf-cache-hit", cacheHit === null ? "Prompt reuse —" : `Prompt reuse ${number(cacheHit, 1)}%`);
    setText("perf-decode-detail", finite(decode) ? `${rate(decode)} t/s` : "—");
    setText("perf-prefill-detail", finite(prefill) ? `${rate(prefill)} t/s` : "—");
    setText("perf-cache-detail", cacheHit === null ? "—" : `${number(cacheHit, 1)}%`);
    setText("perf-draft-detail", draft === null ? "—" : `${number(draft, 1)}%`);
    setText("perf-cache-bytes", bytes(current.cache_bytes));
  }

  function renderResources() {
    const currentHost = host();
    const currentGpu = currentHost.gpu || {};
    const memory = currentHost.memory || {};
    const disk = currentHost.disk || {};
    const memoryUsedPercent = finite(memory.used) && finite(memory.total) && memory.total > 0 ? memory.used / memory.total * 100 : null;
    const diskUsedPercent = finite(disk.used) && finite(disk.total) && disk.total > 0 ? disk.used / disk.total * 100 : null;
    const gttUsedPercent = finite(currentGpu.gtt_used) && finite(currentGpu.gtt_total) && currentGpu.gtt_total > 0 ? currentGpu.gtt_used / currentGpu.gtt_total * 100 : null;
    const vramUsedPercent = finite(currentGpu.vram_used) && finite(currentGpu.vram_total) && currentGpu.vram_total > 0 ? currentGpu.vram_used / currentGpu.vram_total * 100 : null;
    setText("resource-cpu", rawPercent(currentHost.cpu_percent));
    setProgress("resource-cpu-bar", currentHost.cpu_percent);
    setText("resource-load", loadLabel(currentHost.load));
    setText("resource-uptime", uptime(currentHost.uptime_seconds));
    setText("resource-gpu", rawPercent(currentGpu.busy_percent));
    setProgress("resource-gpu-bar", currentGpu.busy_percent);
    setText("resource-temp", finite(currentGpu.temp_c) ? `${number(currentGpu.temp_c, 1)} °C` : "—");
    setText("resource-clock", finite(currentGpu.clock_mhz) ? `${integer(currentGpu.clock_mhz)} MHz` : "—");
    setText("resource-memory", memoryUsedPercent === null ? "—" : `${number(memoryUsedPercent, 1)}%`);
    setProgress("resource-memory-bar", memoryUsedPercent);
    setText("resource-memory-used", bytes(memory.used));
    setText("resource-memory-available", bytes(memory.available));
    setText("resource-disk", diskUsedPercent === null ? "—" : `${number(diskUsedPercent, 1)}%`);
    setProgress("resource-disk-bar", diskUsedPercent);
    setText("resource-disk-used", bytes(disk.used));
    setText("resource-disk-free", bytes(disk.free));
    setText("resource-gtt", finite(currentGpu.gtt_total) ? `${bytes(currentGpu.gtt_total)} total` : "—");
    setText("resource-gtt-used", finite(currentGpu.gtt_used) ? `${bytes(currentGpu.gtt_used)} / ${bytes(currentGpu.gtt_total)}` : "—");
    setProgress("resource-gtt-bar", gttUsedPercent);
    setText("resource-vram-used", finite(currentGpu.vram_used) ? `${bytes(currentGpu.vram_used)} / ${bytes(currentGpu.vram_total)}` : "—");
    setProgress("resource-vram-bar", vramUsedPercent);
    setText("network-rx", dataRate(currentHost.network?.rx_bps));
    setText("network-tx", dataRate(currentHost.network?.tx_bps));
  }

  function sortedEvents() {
    return [...state.events].sort((left, right) => {
      const leftTime = timestampMillis(left.timestamp) || 0;
      const rightTime = timestampMillis(right.timestamp) || 0;
      return rightTime - leftTime;
    });
  }

  function renderEventList(id, limit = null) {
    const container = document.getElementById(id);
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    const events = sortedEvents().slice(0, limit || undefined);
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "event-empty";
      empty.textContent = state.eventsError ? "Event stream unavailable." : "No service events in this window.";
      container.appendChild(empty);
      return;
    }
    events.forEach((event) => {
      const row = document.createElement("div");
      const level = text(event.level, "info").toLowerCase();
      row.className = "event-row";
      row.dataset.level = level;
      const dot = document.createElement("span");
      dot.className = "event-level";
      const message = document.createElement("span");
      message.className = "event-message";
      message.textContent = cleanMessage(event.message, "Service event");
      const time = document.createElement("time");
      time.className = "event-time";
      time.textContent = relativeTime(event.timestamp);
      if (timestampMillis(event.timestamp)) time.dateTime = new Date(timestampMillis(event.timestamp)).toISOString();
      row.append(dot, message, time);
      container.appendChild(row);
    });
  }

  function renderEvents() {
    renderEventList("overview-events", 4);
    renderEventList("activity-events");
    const count = state.events.length;
    setText("activity-event-count", `${count} event${count === 1 ? "" : "s"}`);
  }

  function renderSources() {
    [["model", "model-source-dot", "model-source-detail", "model-source-state"], ["hardware", "hardware-source-dot", "hardware-source-detail", "hardware-source-state"]].forEach(([name, dotId, detailId, stateId]) => {
      const result = sourceStatus(source(name));
      const dot = document.getElementById(dotId);
      if (dot) dot.dataset.state = result.state;
      setText(detailId, result.detail);
      setText(stateId, result.label);
    });
  }

  function renderPage() {
    $$(".page").forEach((page) => {
      const active = page.dataset.page === state.page;
      page.classList.toggle("is-active", active);
      page.hidden = !active;
    });
    $$("[data-page-target]").forEach((button) => {
      const active = button.dataset.pageTarget === state.page;
      button.classList.toggle("is-active", active);
      if (button.classList.contains("nav-item")) {
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
    });
    setText("page-heading", PAGE_LABELS[state.page] || "Overview");
  }

  function render() {
    renderConnection();
    renderBanner();
    renderModel();
    renderKpis();
    renderAllocation();
    renderCharts();
    renderPerformance();
    renderResources();
    renderEvents();
    renderSources();
    renderPage();
  }

  function setPage(page) {
    if (!PAGE_LABELS[page]) return;
    state.page = page;
    renderPage();
  }

  function setRange(range) {
    if (!RANGE_LABELS[range] || state.range === range) return;
    state.range = range;
    $$("[data-range]").forEach((button) => button.classList.toggle("is-active", button.dataset.range === range));
    void pollHistory();
    render();
  }

  function setTheme(theme) {
    const normalized = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = normalized;
    const toggle = $("#theme-toggle");
    if (toggle) toggle.setAttribute("aria-pressed", String(normalized === "light"));
    setText("theme-label", normalized === "light" ? "Dark theme" : "Light theme");
    try { window.localStorage.setItem("strixDash-theme", normalized); } catch (_) { /* storage can be unavailable */ }
  }

  async function manualRefresh() {
    if (state.manualRefreshInFlight) return;
    state.manualRefreshInFlight = true;
    const button = $("#refresh-button");
    button?.classList.add("is-spinning");
    try {
      await Promise.allSettled([pollStatus(), pollHistory(), pollEvents()]);
    } finally {
      state.manualRefreshInFlight = false;
      button?.classList.remove("is-spinning");
    }
  }

  function init() {
    setText("node-endpoint", window.location.host);
    let savedTheme = "dark";
    try { savedTheme = window.localStorage.getItem("strixDash-theme") || "dark"; } catch (_) { /* storage can be unavailable */ }
    setTheme(savedTheme);
    $$('[data-page-target]').forEach((button) => button.addEventListener("click", () => setPage(button.dataset.pageTarget)));
    $$('[data-range]').forEach((button) => button.addEventListener("click", () => setRange(button.dataset.range)));
    $("#theme-toggle")?.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
    $("#refresh-button")?.addEventListener("click", () => void manualRefresh());
    render();
    void pollStatus();
    void pollHistory();
    void pollEvents();
    window.addEventListener("pagehide", () => {
      leaving = true;
      navigator.sendBeacon("/api/viewer/leave", VIEWER_ID);
    });
    window.addEventListener("pageshow", () => {
      leaving = false;
      void pollStatus();
    });
    window.setInterval(() => void pollStatus(), REFRESH_MS);
    window.setInterval(() => { void pollHistory(); void pollEvents(); }, SLOW_REFRESH_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
