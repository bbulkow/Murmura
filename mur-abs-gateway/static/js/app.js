"use strict";

// State held in-memory while editing. Saving converts back to the server's
// JSON shape and POSTs the whole config in one call.
const state = {
  // [{name, associated_device_id, mappings: [{upstream, file_path}]}]
  cards: [],
  upstreamTriggers: [],   // [{name, type}]
  devices: [],            // [{id, peer_ip, ...}]
  filesByDevice: {},      // device_id -> [{name, path, ...}]
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function setSaveStatus(text, kind) {
  const el = $("#save-status");
  el.textContent = text || "";
  el.className = "save-status" + (kind ? " " + kind : "");
}

async function fetchJson(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json();
}

async function loadAll() {
  setSaveStatus("Loading...");
  try {
    const [cfg, devicesResp, triggersResp] = await Promise.all([
      fetchJson("/api/abstract-triggers"),
      fetchJson("/api/devices"),
      fetchJson("/api/upstream-triggers").catch(() => ({ trigger_names: [], triggers: [] })),
    ]);
    state.devices = devicesResp.devices || [];
    state.upstreamTriggers = triggersResp.triggers || [];
    state.cards = Object.entries(cfg.abstract_triggers || {})
      .map(([name, c]) => ({
        name,
        associated_device_id: c.associated_device_id || "",
        mappings: (c.mappings || []).map(m => ({
          upstream: m.upstream,
          file_path: m.file_path,
        })),
      }))
      .sort((a, b) => a.name.localeCompare(b.name));

    // Pre-fetch files for any associated devices we'll render.
    const deviceIds = Array.from(new Set(state.cards
      .map(c => c.associated_device_id)
      .filter(Boolean)));
    await Promise.all(deviceIds.map(id => loadFilesForDevice(id).catch(() => {})));

    renderCards();
    setSaveStatus(`Loaded ${state.cards.length} abstract trigger(s)`, "ok");
    setTimeout(() => setSaveStatus(""), 2000);
  } catch (e) {
    setSaveStatus(`Load failed: ${e.message}`, "err");
  }
}

async function loadFilesForDevice(deviceId) {
  if (!deviceId) return [];
  if (state.filesByDevice[deviceId]) return state.filesByDevice[deviceId];
  try {
    const data = await fetchJson(`/api/device/${encodeURIComponent(deviceId)}/files`);
    state.filesByDevice[deviceId] = data.files || [];
  } catch (e) {
    state.filesByDevice[deviceId] = [];
  }
  return state.filesByDevice[deviceId];
}

function renderCards() {
  const grid = $("#trigger-cards");
  const hint = $("#empty-hint");
  // Wipe everything except the empty hint placeholder.
  $$(".trigger-card", grid).forEach(c => c.remove());

  if (state.cards.length === 0) {
    hint.style.display = "";
    return;
  }
  hint.style.display = "none";

  state.cards.forEach((card, idx) => {
    const node = renderCard(card, idx);
    grid.appendChild(node);
  });
}

function renderCard(card, idx) {
  const tpl = $("#card-template").content.cloneNode(true);
  const root = tpl.querySelector(".trigger-card");
  root.dataset.idx = String(idx);

  const nameInput = $(".trigger-name", root);
  nameInput.value = card.name;
  nameInput.addEventListener("change", () => {
    card.name = nameInput.value.trim();
  });

  $(".delete-card", root).addEventListener("click", () => {
    if (!confirm(`Delete abstract trigger "${card.name}"?`)) return;
    state.cards.splice(idx, 1);
    renderCards();
  });

  const deviceSel = $(".associated-device", root);
  populateDeviceSelect(deviceSel, card.associated_device_id);
  deviceSel.addEventListener("change", async () => {
    card.associated_device_id = deviceSel.value;
    await loadFilesForDevice(card.associated_device_id).catch(() => {});
    refreshFileSelects(root, card);
  });

  const mappingsEl = $(".mappings", root);
  card.mappings.forEach((m, mi) => {
    mappingsEl.appendChild(renderMapping(card, m, mi));
  });

  $(".add-mapping", root).addEventListener("click", () => {
    card.mappings.push({ upstream: "", file_path: "" });
    mappingsEl.appendChild(renderMapping(card, card.mappings[card.mappings.length - 1], card.mappings.length - 1));
  });

  return root;
}

function renderMapping(card, mapping, mi) {
  const tpl = $("#mapping-template").content.cloneNode(true);
  const row = tpl.querySelector(".mapping-row");
  row.dataset.mi = String(mi);

  const upSel = $(".upstream-select", row);
  populateUpstreamSelect(upSel, mapping.upstream);
  upSel.addEventListener("change", () => { mapping.upstream = upSel.value; });

  const fileSel = $(".file-select", row);
  populateFileSelect(fileSel, card.associated_device_id, mapping.file_path);
  fileSel.addEventListener("change", () => { mapping.file_path = fileSel.value; });

  $(".delete-mapping", row).addEventListener("click", () => {
    const idx = card.mappings.indexOf(mapping);
    if (idx >= 0) card.mappings.splice(idx, 1);
    row.remove();
  });

  return row;
}

function populateDeviceSelect(sel, currentId) {
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "(none — pick to populate file list)";
  sel.appendChild(blank);

  const known = new Set(state.devices.map(d => d.id));
  state.devices.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = `${d.id}  (${d.peer_ip || "?"})`;
    sel.appendChild(opt);
  });
  // Preserve a previously-saved id even if device is offline.
  if (currentId && !known.has(currentId)) {
    const opt = document.createElement("option");
    opt.value = currentId;
    opt.textContent = `${currentId}  (offline)`;
    sel.appendChild(opt);
  }
  sel.value = currentId || "";
}

function populateUpstreamSelect(sel, currentValue) {
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "(select upstream trigger)";
  sel.appendChild(blank);

  const known = new Set();
  state.upstreamTriggers.forEach(t => {
    const opt = document.createElement("option");
    opt.value = t.name;
    opt.textContent = t.type ? `${t.name}  [${t.type}]` : t.name;
    sel.appendChild(opt);
    known.add(t.name);
  });
  if (currentValue && !known.has(currentValue)) {
    const opt = document.createElement("option");
    opt.value = currentValue;
    opt.textContent = `${currentValue}  (not in upstream list)`;
    sel.appendChild(opt);
  }
  sel.value = currentValue || "";
}

function populateFileSelect(sel, deviceId, currentValue) {
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = deviceId ? "(select file)" : "(no device chosen)";
  sel.appendChild(blank);

  const files = state.filesByDevice[deviceId] || [];
  const known = new Set();
  files.forEach(f => {
    const opt = document.createElement("option");
    opt.value = f.path;
    opt.textContent = f.name + (typeof f.size === "number" ? ` (${(f.size / 1024).toFixed(0)} KB)` : "");
    sel.appendChild(opt);
    known.add(f.path);
  });
  if (currentValue && !known.has(currentValue)) {
    const opt = document.createElement("option");
    opt.value = currentValue;
    opt.textContent = currentValue + " (not on selected device)";
    sel.appendChild(opt);
  }
  sel.value = currentValue || "";
}

function refreshFileSelects(cardEl, card) {
  $$(".mapping-row", cardEl).forEach((row, i) => {
    const mapping = card.mappings[i];
    if (!mapping) return;
    const fileSel = $(".file-select", row);
    populateFileSelect(fileSel, card.associated_device_id, mapping.file_path);
  });
}

async function saveAll() {
  // Validate names: non-empty, unique.
  const names = state.cards.map(c => c.name.trim()).filter(Boolean);
  if (names.length !== state.cards.length) {
    setSaveStatus("All abstract triggers need a name", "err");
    return;
  }
  if (new Set(names).size !== names.length) {
    setSaveStatus("Abstract trigger names must be unique", "err");
    return;
  }
  // Validate mappings: each must have non-empty upstream and file_path,
  // upstream names unique within the card.
  for (const c of state.cards) {
    const upstreams = c.mappings.map(m => m.upstream.trim());
    if (upstreams.some(u => !u)) {
      setSaveStatus(`'${c.name}': all mappings need an upstream trigger`, "err");
      return;
    }
    if (c.mappings.some(m => !m.file_path)) {
      setSaveStatus(`'${c.name}': all mappings need a file`, "err");
      return;
    }
    if (new Set(upstreams).size !== upstreams.length) {
      setSaveStatus(`'${c.name}': duplicate upstream trigger`, "err");
      return;
    }
  }

  const body = {
    version: 1,
    abstract_triggers: {},
  };
  for (const c of state.cards) {
    body.abstract_triggers[c.name] = {
      associated_device_id: c.associated_device_id || null,
      mappings: c.mappings.map(m => ({ upstream: m.upstream, file_path: m.file_path })),
    };
  }

  setSaveStatus("Saving...");
  try {
    await fetchJson("/api/abstract-triggers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setSaveStatus("Saved", "ok");
    setTimeout(() => setSaveStatus(""), 1500);
  } catch (e) {
    setSaveStatus(`Save failed: ${e.message}`, "err");
  }
}

function addNewCard() {
  state.cards.unshift({
    name: "",
    associated_device_id: "",
    mappings: [],
  });
  renderCards();
}

// ---------- Event log (SSE) ----------

function renderLogEntry(entry) {
  const log = $("#event-log");
  const row = document.createElement("div");
  row.className = "row-line " + classifyEntry(entry);
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = formatTs(entry.ts);
  row.appendChild(ts);

  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = entry.kind === "subscribe" ? "SUB" : "FIRE";
  row.appendChild(tag);

  const text = document.createElement("span");
  text.textContent = formatEntry(entry);
  row.appendChild(text);

  log.prepend(row);

  // Cap displayed entries — keep last ~50.
  while (log.childElementCount > 50) {
    log.lastElementChild.remove();
  }
}

function classifyEntry(entry) {
  if (entry.kind === "subscribe") return "subscribe";
  if (entry.status === "ok") return "fire-ok";
  return "fire-bad";
}

function formatEntry(entry) {
  if (entry.kind === "subscribe") {
    return `${entry.device_id} (${entry.peer_ip}) → [${(entry.triggers || []).join(", ")}]`;
  }
  // fire
  const upstream = entry.upstream || "?";
  const abstract = entry.abstract ? `→ ${entry.abstract}` : "(passthrough)";
  const file = entry.file_path ? ` file=${entry.file_path}` : "";
  const devices = (entry.devices || []).join(",") || "—";
  return `${upstream} ${abstract}${file}  devices=[${devices}]  ${entry.status}`;
}

function formatTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

function startLogStream() {
  let es;
  function open() {
    es = new EventSource("/api/log/stream");
    es.onmessage = (ev) => {
      try { renderLogEntry(JSON.parse(ev.data)); }
      catch { /* ignore */ }
    };
    es.onerror = () => {
      es.close();
      // Reconnect after a short delay.
      setTimeout(open, 2500);
    };
  }
  open();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-add-trigger").addEventListener("click", addNewCard);
  $("#btn-reload").addEventListener("click", loadAll);
  // Save on Cmd/Ctrl-S.
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      saveAll();
    }
  });
  // Auto-save when leaving an input/select after edits.
  document.addEventListener("change", (ev) => {
    if (ev.target.matches(".trigger-name, .associated-device, .upstream-select, .file-select")) {
      saveAll();
    }
  });
  loadAll().then(startLogStream);
});
