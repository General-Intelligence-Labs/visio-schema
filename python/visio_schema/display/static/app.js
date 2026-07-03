"use strict";
// visio-display --serve launcher UI. Talks to the local aiohttp app: a live device
// list over SSE (/api/devices), and connect/disconnect/status/viewer JSON endpoints.

const state = { devices: [], connectedId: null, urls: null, pollTimer: null,
                status: { state: "idle" } };

const $ = (id) => document.getElementById(id);

// ---- i18n (translations are inline in index.html as window.I18N) ---------- //
function detectLang() {
  const saved = localStorage.getItem("visio-lang");
  if (saved === "en" || saved === "zh") return saved;
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}
let lang = detectLang();

function tr(key) {
  const L = window.I18N || {};
  return (L[lang] && L[lang][key]) || (L.en && L.en[key]) || key;
}

function applyI18n() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = tr(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => {
    el.innerHTML = tr(el.dataset.i18nHtml);
  });
  $("btn-lang").textContent = lang === "zh" ? "EN" : "中文";
  render();                      // empty-state strings
  applyStatus(state.status);     // state-label strings
}

// ---- device list (left) --------------------------------------------------- //
function deviceRow(d) {
  const row = document.createElement("div");
  row.className = "device" + (d.id === state.connectedId ? " active" : "");
  const meta = d.transport === "usb" ? (d.device || "")
             : d.host ? `${d.host}:${d.port}` : "";
  row.innerHTML =
    `<span class="dot"></span>` +
    `<span class="info"><span class="label"></span>` +
    `<span class="meta"></span></span>` +
    `<span class="badge">${d.transport}</span>`;
  row.querySelector(".label").textContent = d.label || d.id;
  row.querySelector(".meta").textContent = meta;
  row.onclick = () => connect(d.id);
  return row;
}

function render() {
  for (const tp of ["usb", "sta", "ap"]) {
    const grp = $("grp-" + tp);
    grp.textContent = "";
    const items = state.devices.filter((d) => d.transport === tp);
    if (!items.length) {
      const e = document.createElement("div");
      e.className = "empty";
      e.textContent = tp === "ap" ? tr("emptyAp") : tp === "usb" ? tr("emptyUsb") : tr("emptySta");
      grp.appendChild(e);
    } else {
      items.forEach((d) => grp.appendChild(deviceRow(d)));
    }
  }
}

function subscribeDevices() {
  const es = new EventSource("/api/devices");
  es.onmessage = (ev) => { state.devices = JSON.parse(ev.data); render(); };
  // EventSource auto-reconnects on error; nothing to do.
}

// ---- connect / viewer (right) --------------------------------------------- //
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data };
}

async function connect(id) {
  const { ok, data } = await postJSON("/api/connect", { id });
  if (!ok) { alert(data.error || tr("connectFailed")); return; }
  state.connectedId = data.connected_id;
  state.urls = data;
  render();
  applyStatus(data);
  startPolling();
}

async function disconnect() {
  const { data } = await postJSON("/api/disconnect");
  state.connectedId = null;
  state.urls = null;
  stopPolling();
  render();
  applyStatus(data);
}

function applyStatus(s) {
  state.status = s;
  const box = $("status").querySelector(".state");
  box.className = "state s-" + (s.state || "idle");
  const key = { idle: "stateIdle", connecting: "stateConnecting",
                streaming: "stateStreaming", error: "stateError" }[s.state];
  $("state-text").textContent = key ? tr(key) : (s.state || "");
  $("st-label").textContent = s.label || "—";
  $("st-transport").textContent = s.transport || "—";
  $("st-messages").textContent = s.messages != null ? s.messages : "—";
  $("st-topics").textContent = s.topics && s.topics.length ? s.topics.length : "—";
  $("st-ws").textContent = s.ws_url || "—";
  $("st-error").textContent = s.error || "";
  const connected = !!s.connected_id;
  $("btn-desktop").disabled = !connected;
  $("btn-browser").disabled = !connected || !(state.urls && state.urls.browser_url);
  $("btn-disconnect").disabled = !connected;
  // "error" is terminal until the next connect — stop the 1 Hz poll.
  if (s.state === "error") stopPolling();
}

async function refreshStatus() {
  const r = await fetch("/api/status");
  if (r.ok) applyStatus(await r.json());
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refreshStatus, 1000);
}
function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

// ---- wiring --------------------------------------------------------------- //
$("btn-lang").onclick = () => {
  lang = lang === "zh" ? "en" : "zh";
  localStorage.setItem("visio-lang", lang);
  applyI18n();
};

$("btn-desktop").onclick = async () => {
  const { data } = await postJSON("/api/open-viewer");
  if (data) state.urls = { ...state.urls, ...data };
};
$("btn-browser").onclick = () => {
  if (state.urls && state.urls.browser_url) {
    // Named target reuses one tab across device switches.
    window.open(state.urls.browser_url, "foxglove");
  }
};
$("btn-disconnect").onclick = disconnect;

$("btn-quit").onclick = async () => {
  await postJSON("/api/shutdown");
  document.body.innerHTML =
    '<p style="padding:2rem;font:15px sans-serif;color:#9aa2b1">' + tr("stopped") + "</p>";
};

$("manual-form").onsubmit = async (e) => {
  e.preventDefault();
  $("manual-err").textContent = "";
  const host = $("host").value.trim();
  const port = $("port").value.trim();
  if (!host) return;
  const { ok, data } = await postJSON("/api/manual", { host, port: port || undefined });
  if (!ok) { $("manual-err").textContent = data.error || tr("unreachable"); return; }
  $("host").value = ""; $("port").value = "";
  // The device now shows up via the SSE list; the operator clicks it to connect.
};

applyI18n();
subscribeDevices();
refreshStatus();
