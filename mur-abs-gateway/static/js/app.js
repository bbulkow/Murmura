"use strict";

// Each card holds {name, associated_device_id, mappings: [{upstream, file_path}]}
// plus a hidden _snapshot string. _snapshot === null marks an unsaved (new)
// card; otherwise it's the JSON.stringify of the data fields at last save.
// A card is "dirty" when its current data doesn't match _snapshot.
const state = {
  cards: [],
  upstreamTriggers: [],   // [{name, type}]
  devices: [],            // [{id, peer_ip, ...}]
  filesByDevice: {},      // device_id -> [{name, path, ...}]
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function cardData(c) {
  return JSON.stringify({
    name: c.name,
    associated_device_id: c.associated_device_id,
    mappings: c.mappings,
  });
}
function isDirty(c) {
  return c._snapshot !== cardData(c);
}

async function fetchJson(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let msg = text;
    try { msg = (JSON.parse(text).error) || text; } catch {}
    throw new Error(`${resp.status}: ${msg}`);
  }
  return resp.json();
}

async function loadAll() {
  try {
    const [cfg, devicesResp, triggersResp] = await Promise.all([
      fetchJson("/api/abstract-triggers"),
      fetchJson("/api/devices"),
      fetchJson("/api/upstream-triggers").catch(() => ({ trigger_names: [], triggers: [] })),
    ]);
    state.devices = devicesResp.devices || [];
    state.upstreamTriggers = triggersResp.triggers || [];
    state.cards = Object.entries(cfg.abstract_triggers || {})
      .map(([name, c]) => {
        const card = {
          name,
          associated_device_id: c.associated_device_id || "",
          mappings: (c.mappings || []).map(m => ({
            upstream: m.upstream,
            file_path: m.file_path,
          })),
        };
        card._snapshot = cardData(card);
        return card;
      })
      .sort((a, b) => a.name.localeCompare(b.name));

    // Pre-fetch files for any associated devices so file dropdowns have options.
    const deviceIds = Array.from(new Set(state.cards
      .map(c => c.associated_device_id)
      .filter(Boolean)));
    await Promise.all(deviceIds.map(id => loadFilesForDevice(id).catch(() => {})));

    renderCards();
  } catch (e) {
    console.error("loadAll failed", e);
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
  $$(".trigger-card", grid).forEach(c => c.remove());

  if (state.cards.length === 0) {
    hint.style.display = "";
    return;
  }
  hint.style.display = "none";

  state.cards.forEach((card) => {
    const node = renderCard(card);
    grid.appendChild(node);
  });
}

function renderCard(card) {
  const tpl = $("#card-template").content.cloneNode(true);
  const root = tpl.querySelector(".trigger-card");

  const nameInput = $(".trigger-name", root);
  nameInput.value = card.name;
  nameInput.addEventListener("input", () => {
    card.name = nameInput.value.trim();
    refreshSaveButton(root, card);
  });

  const deviceSel = $(".associated-device", root);
  populateDeviceSelect(deviceSel, card.associated_device_id);
  deviceSel.addEventListener("change", async () => {
    card.associated_device_id = deviceSel.value;
    await loadFilesForDevice(card.associated_device_id).catch(() => {});
    refreshFileSelects(root, card);
    refreshSaveButton(root, card);
  });

  const mappingsEl = $(".mappings", root);
  card.mappings.forEach((m) => {
    mappingsEl.appendChild(renderMapping(root, card, m));
  });

  $(".add-mapping", root).addEventListener("click", () => {
    const m = { upstream: "", file_path: "" };
    card.mappings.push(m);
    mappingsEl.appendChild(renderMapping(root, card, m));
    refreshSaveButton(root, card);
  });

  $(".save-card", root).addEventListener("click", () => saveCard(root, card));
  $(".delete-card", root).addEventListener("click", () => deleteCard(root, card));

  refreshSaveButton(root, card);
  return root;
}

function renderMapping(cardEl, card, mapping) {
  const tpl = $("#mapping-template").content.cloneNode(true);
  const row = tpl.querySelector(".mapping-row");

  const upSel = $(".upstream-select", row);
  populateUpstreamSelect(upSel, mapping.upstream);
  upSel.addEventListener("change", () => {
    mapping.upstream = upSel.value;
    refreshSaveButton(cardEl, card);
  });

  const fileSel = $(".file-select", row);
  populateFileSelect(fileSel, card.associated_device_id, mapping.file_path);
  fileSel.addEventListener("change", () => {
    mapping.file_path = fileSel.value;
    refreshSaveButton(cardEl, card);
  });

  $(".delete-mapping", row).addEventListener("click", () => {
    const idx = card.mappings.indexOf(mapping);
    if (idx >= 0) card.mappings.splice(idx, 1);
    row.remove();
    refreshSaveButton(cardEl, card);
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

function refreshSaveButton(cardEl, card) {
  const btn = $(".save-card", cardEl);
  if (!btn) return;
  if (isDirty(card)) {
    btn.disabled = false;
    btn.classList.add("primary");
  } else {
    btn.disabled = true;
    btn.classList.remove("primary");
  }
}

function showFeedback(cardEl, text, kind) {
  const fb = $(".card-feedback", cardEl);
  fb.textContent = text || "";
  fb.className = "card-feedback" + (kind ? " " + kind : "");
}

function validateConfig() {
  const names = state.cards.map(c => c.name.trim());
  if (names.some(n => !n)) return "All abstract triggers need a name";
  if (new Set(names).size !== names.length) return "Abstract trigger names must be unique";
  for (const c of state.cards) {
    const upstreams = c.mappings.map(m => m.upstream.trim());
    if (upstreams.some(u => !u)) return `'${c.name}': all mappings need an upstream trigger`;
    if (c.mappings.some(m => !m.file_path)) return `'${c.name}': all mappings need a file`;
    if (new Set(upstreams).size !== upstreams.length) return `'${c.name}': duplicate upstream trigger`;
  }
  return null;
}

async function postConfig() {
  const body = { version: 1, abstract_triggers: {} };
  for (const c of state.cards) {
    body.abstract_triggers[c.name] = {
      associated_device_id: c.associated_device_id || null,
      mappings: c.mappings.map(m => ({ upstream: m.upstream, file_path: m.file_path })),
    };
  }
  await fetchJson("/api/abstract-triggers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function refreshAllSaveButtons() {
  const cardEls = $$(".trigger-card");
  state.cards.forEach((c, i) => {
    if (cardEls[i]) refreshSaveButton(cardEls[i], c);
  });
}

async function saveCard(cardEl, card) {
  const err = validateConfig();
  if (err) {
    showFeedback(cardEl, err, "err");
    return;
  }
  showFeedback(cardEl, "Saving...");
  try {
    await postConfig();
    state.cards.forEach(c => { c._snapshot = cardData(c); });
    refreshAllSaveButtons();
    showFeedback(cardEl, "Saved ✓", "ok");
    setTimeout(() => {
      // Only clear our own message if no newer message replaced it.
      const fb = $(".card-feedback", cardEl);
      if (fb && fb.textContent === "Saved ✓") showFeedback(cardEl, "");
    }, 2000);
  } catch (e) {
    showFeedback(cardEl, `Save failed: ${e.message}`, "err");
  }
}

async function deleteCard(cardEl, card) {
  const idx = state.cards.indexOf(card);
  if (idx < 0) return;

  // New, never-saved cards just disappear locally — no POST needed.
  if (card._snapshot === null) {
    state.cards.splice(idx, 1);
    cardEl.remove();
    if (state.cards.length === 0) $("#empty-hint").style.display = "";
    return;
  }

  // Saved cards: remove and POST the rest.
  state.cards.splice(idx, 1);
  showFeedback(cardEl, "Deleting...");
  try {
    await postConfig();
    cardEl.remove();
    if (state.cards.length === 0) $("#empty-hint").style.display = "";
  } catch (e) {
    state.cards.splice(idx, 0, card);
    showFeedback(cardEl, `Delete failed: ${e.message}`, "err");
  }
}

function addNewCard() {
  const card = {
    name: "",
    associated_device_id: "",
    mappings: [],
    _snapshot: null,   // null = unsaved/new
  };
  state.cards.unshift(card);
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
      setTimeout(open, 2500);
    };
  }
  open();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-add-trigger").addEventListener("click", addNewCard);
  $("#btn-reload").addEventListener("click", loadAll);
  loadAll().then(startLogStream);
});
