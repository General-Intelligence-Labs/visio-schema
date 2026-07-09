"use strict";
// visio-display --serve launcher UI. Talks to the local aiohttp app: a live device
// list over SSE (/api/devices), and connect/disconnect/status/viewer JSON endpoints.

const state = { devices: [], connectedId: null, urls: null, pollTimer: null,
                stateTimer: null, status: { state: "idle" }, hevcOk: null,
                decodeOverride: null };

// Whether to ask the launcher to decode H.265 → JPEG on this PC. Auto = on when the browser
// can't decode HEVC (see checkHevc); the toggle lets the operator override either way (e.g.
// Foxglove Desktop can decode where this page's browser can't, or vice-versa).
function effectiveDecode() {
  return state.decodeOverride !== null ? state.decodeOverride : state.hevcOk === false;
}

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
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = tr(el.dataset.i18nPh);
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
  const { ok, data } = await postJSON("/api/connect", { id, decode: effectiveDecode() });
  if (!ok) { alert(data.error || tr("connectFailed")); return; }
  state.connectedId = data.connected_id;
  state.urls = data;
  render();
  applyStatus(data);
  startPolling();
  resetConfigMessages();
  loadConfigState();      // populate the current-state header + pre-fill the forms
  scanWifi(true);         // pre-scan host Wi-Fi so the dropdown has networks on first open
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
  // Video-compatibility UI. The Ego streams H.265; Foxglove decodes via the browser's
  // WebCodecs, which on Chrome/Edge has no software HEVC fallback. The toggle reflects the
  // effective decode preference; when the launcher is decoding on this PC (server-confirmed
  // via s.decoding) show a calm info note; only when a device is connected, the browser
  // can't do HEVC, and decode is off do we surface the red install warning (→ a red ✕).
  $("decode-toggle").checked = effectiveDecode();
  $("decode-note").hidden = !s.decoding;
  const showWarn = connected && !s.decoding && state.hevcOk === false;
  if (showWarn) renderHevcWarn();     // OS-tailored native-decode guidance (content is computed)
  $("hevc-warn").hidden = !showWarn;
  // The config panel needs a live bus connection to send commands, so hide it once the
  // link errors out (the device's reader thread is gone; commands would just fail).
  $("config").hidden = !connected || s.state === "error";
  // "error" is terminal until the next connect — stop the 1 Hz poll.
  if (s.state === "error") stopPolling();
}

async function refreshStatus() {
  const r = await fetch("/api/status");
  if (r.ok) applyStatus(await r.json());
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refreshStatus, 1000);   // stream liveness (passive)
  state.stateTimer = setInterval(pollState, 4000);      // DeviceState header (pull-only)
}
function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  if (state.stateTimer) { clearInterval(state.stateTimer); state.stateTimer = null; }
}

// ---- HEVC / H.265 decode capability --------------------------------------- //
// Foxglove renders the device's H.265 video through the browser's WebCodecs VideoDecoder.
// Ask the browser whether it can decode HEVC at all; Chrome/Edge on Windows only can with
// a hardware decoder + the OS "HEVC Video Extensions". A definitive "no" reveals the banner
// (see applyStatus). Anything uncertain (no WebCodecs, probe throws) leaves hevcOk null so
// we don't cry wolf, but WebCodecs-less browsers can't decode HEVC anyway → treat as false.
async function checkHevc() {
  if (typeof VideoDecoder === "undefined" || !VideoDecoder.isConfigSupported) {
    state.hevcOk = false;
    applyStatus(state.status);
    return;
  }
  // HEVC Main profile at a couple of representative levels, both common codec-string forms.
  const codecs = ["hev1.1.6.L120.90", "hvc1.1.6.L120.90", "hev1.1.6.L93.90"];
  let ok = false;
  for (const codec of codecs) {
    try {
      const r = await VideoDecoder.isConfigSupported({ codec });
      if (r && r.supported) { ok = true; break; }
    } catch (e) { /* unrecognized config → try the next form */ }
  }
  state.hevcOk = ok;
  applyStatus(state.status);
}

// Best-effort OS family, used ONLY to pick which native-decode tip to show in the warning
// (the decode-capability check itself is the cross-platform WebCodecs probe above, so auto
// software-decode already works on every OS). Linux/macOS get their own guidance, not Windows'.
function osFamily() {
  const uad = navigator.userAgentData;
  const p = ((uad && uad.platform) || navigator.platform || navigator.userAgent || "").toLowerCase();
  if (p.includes("win")) return "Win";
  if (p.includes("mac")) return "Mac";
  if (p.includes("linux") || p.includes("x11") || p.includes("android")) return "Linux";
  return "Other";
}

function renderHevcWarn() {
  $("hevc-warn").innerHTML = tr("hevcWarnLead") + " " + tr("hevcWarn" + osFamily());
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

// Flipping the compatibility toggle overrides the auto decision; if a device is live, reconnect
// so the new mode takes effect (connect() tears down the old reader first, so this is clean).
$("decode-toggle").onchange = () => {
  state.decodeOverride = $("decode-toggle").checked;
  if (state.connectedId) connect(state.connectedId);
};

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

// ---- device config (right, below status) --------------------------------- //
// Every config action sends a Command over the connected device's bridge and shows the
// device's own ok/error in the section's message line. Setters echo a fresh DeviceState,
// which we fold back into the current-state header + form values.
function setMsg(el, cls, key) { el.className = "cfg-msg " + cls; el.textContent = key ? tr(key) : ""; }
function setErr(el, text) { el.className = "cfg-msg err"; el.textContent = text; }  // raw device text

async function cfgPost(url, body, msgEl, okKey) {
  if (msgEl) setMsg(msgEl, "busy", "cfgBusy");
  let ok, data;
  try {
    ({ ok, data } = await postJSON(url, body));
  } catch (e) {
    // the launcher process/connection dropped mid-request — don't leave "Working…" stuck
    if (msgEl) setErr(msgEl, tr("cfgConnErr"));
    return { ok: false, data: {} };
  }
  const good = ok && data.ok;
  if (msgEl) {
    if (good) setMsg(msgEl, "ok", okKey || "cfgDone");
    else setErr(msgEl, data.error || tr("cfgConnErr"));
  }
  if (good && data.state) renderStateHeader(data.state);   // header only — don't clobber edits
  return { ok: good, data };
}

function resetConfigMessages() {
  document.querySelectorAll(".cfg-msg").forEach((el) => setMsg(el, "", null));
}

// The read-only "Current settings" header. Safe to call on every poll — it never touches
// the editable fields, so a background refresh can't clobber what the operator is typing.
function renderStateHeader(st) {
  if (!st) return;
  // DeviceState.WifiState enum: 0 DISABLED, 1 STA (connected), 2 AP_FALLBACK.
  const wifi = st.wifi_state === 1
      ? tr("cfgWifiConnected") + (st.wifi_ssid ? " · " + st.wifi_ssid : "")
      : st.wifi_state === 2 ? tr("cfgWifiAp") : tr("cfgWifiOff");
  const disk = st.disk_no_sdcard ? tr("cfgStNoCard")
      : (st.disk_free_pct != null && st.disk_free_pct >= 0) ? st.disk_free_pct + "%" : "—";
  const rows = [
    [tr("cfgStWifi"), wifi],
    [tr("cfgStIp"), st.wifi_ip || "—"],
    [tr("cfgStDisk"), disk],
    [tr("cfgStBitrate"), st.video_bitrate_kbps ? (st.video_bitrate_kbps / 1000) + " Mbps" : "—"],
    [tr("cfgStRecording"), st.recording_session_name || "—"],
  ];
  const dl = $("cfg-state-dl");
  dl.textContent = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
}

// Pre-fill the EDITABLE fields — only on connect and right after a save, never on the poll.
function prefillForms(st) {
  if (!st) return;
  if (st.video_bitrate_kbps) $("cfg-bitrate-kbps").value = String(st.video_bitrate_kbps);
  $("cfg-meta-task").value = st.recording_meta_task || "";
  $("cfg-meta-location").value = st.recording_meta_location || "";
  $("cfg-meta-capturer").value = st.recording_meta_capturer || "";
  $("cfg-meta-message").value = st.recording_meta_message || "";
}

function fillState(st) { renderStateHeader(st); prefillForms(st); }

async function loadConfigState() {
  const { data } = await postJSON("/api/config/state", {});
  if (data && data.state) fillState(data.state);
}

// DeviceState is pull-only (no periodic stream), so poll GetState to keep the header live —
// header only, so the form fields stay editable. Guarded so slow replies don't stack.
let _stateInFlight = false;
async function pollState() {
  if (_stateInFlight) return;
  _stateInFlight = true;
  try {
    const { data } = await postJSON("/api/config/state", {});
    if (data && data.state) renderStateHeader(data.state);
  } catch (e) { /* transient — the next tick retries */ }
  finally { _stateInFlight = false; }
}

// Wi-Fi networks are scanned on THIS computer (host-side), server sorts them strongest
// first. There's no manual scan button: the list refreshes automatically when the operator
// opens the dropdown (debounced) and once on connect so the first open already has data.
let _wifiScanning = false;
let _wifiScanAt = 0;
async function scanWifi(force) {
  if (_wifiScanning) return;
  if (!force && Date.now() - _wifiScanAt < 2000) return;   // already fresh
  _wifiScanning = true;
  const msg = $("cfg-wifi-msg");
  const sel = $("cfg-wifi-ssid");
  setMsg(msg, "busy", "cfgScanning");
  try {
    const { ok, data } = await postJSON("/api/config/wifi/scan", {});
    const keep = sel.value;                     // preserve any current pick across a refresh
    sel.textContent = "";
    const first = document.createElement("option");
    first.value = ""; first.textContent = tr("cfgPickNet"); sel.append(first);
    const nets = (ok && data.ok && data.scan) ? data.scan : [];
    if (!nets.length) { setErr(msg, (data && data.error) || tr("cfgNoNets")); return; }
    for (const n of nets) {
      const o = document.createElement("option");
      o.value = n.ssid;
      o.textContent = `${n.ssid} — ${n.security || "OPEN"} · ${n.signal}%`;
      sel.append(o);
    }
    if (keep) sel.value = keep;
    setMsg(msg, "", null);
  } catch (e) {
    setErr(msg, tr("cfgConnErr"));
  } finally {
    _wifiScanning = false;
    _wifiScanAt = Date.now();
  }
}
// auto-scan when the dropdown is opened (mousedown precedes the native popup; focus covers
// keyboard) — the list may show the previous scan this open and the fresh one on the next.
$("cfg-wifi-ssid").addEventListener("mousedown", () => scanWifi(false));
$("cfg-wifi-ssid").addEventListener("focus", () => scanWifi(false));

$("cfg-wifi-ssid").onchange = () => {
  if ($("cfg-wifi-ssid").value) $("cfg-wifi-ssid-manual").value = $("cfg-wifi-ssid").value;
};

$("cfg-wifi-connect").onclick = () => {
  const ssid = ($("cfg-wifi-ssid-manual").value || $("cfg-wifi-ssid").value).trim();
  if (!ssid) { setMsg($("cfg-wifi-msg"), "err", "cfgSsid"); return; }
  cfgPost("/api/config/wifi", { ssid, passphrase: $("cfg-wifi-pass").value }, $("cfg-wifi-msg"));
};

$("cfg-time").onclick = () => cfgPost("/api/config/time", {}, $("cfg-quick-msg"));
$("cfg-identify").onclick = () => cfgPost("/api/config/identify", {}, $("cfg-quick-msg"));
$("cfg-bitrate-set").onclick = () =>
  cfgPost("/api/config/bitrate", { bitrate_kbps: Number($("cfg-bitrate-kbps").value) },
          $("cfg-bitrate-msg"), "cfgSaved");
$("cfg-meta-save").onclick = () =>
  cfgPost("/api/config/meta", {
    task: $("cfg-meta-task").value, location: $("cfg-meta-location").value,
    capturer: $("cfg-meta-capturer").value, message: $("cfg-meta-message").value,
  }, $("cfg-meta-msg"), "cfgSaved");

$("cfg-format-go").onclick = () => {
  if ($("cfg-format-confirm").value.trim().toUpperCase() !== "FORMAT") {
    setMsg($("cfg-format-msg"), "err", "cfgConfirmFormat");
    return;
  }
  cfgPost("/api/config/format", {}, $("cfg-format-msg")).then((r) => {
    if (r.ok) $("cfg-format-confirm").value = "";
  });
};

applyI18n();
subscribeDevices();
refreshStatus();
checkHevc();
