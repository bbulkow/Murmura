/* Mur Scene Server UI.
   Ported from haven/Triggers/scene_service.html (inline <script>, lines 426-1021).
   The scene-status functions (loadSceneStatus / renderSceneStatus /
   buildSvcPanelHeader) aggregated haven's flame service and OSC proxy and went
   away with that endpoint; the Related Services card replaces them.
   Vanilla JS, no dependencies -- this must work with no internet. */

const BASE = window.location.origin;

// --- Utilities -------------------------------------------------------------

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 350);
  }, 3200);
}

function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function nowLabel() { return `Last updated: ${new Date().toLocaleTimeString()}`; }

/** Convert "HH:MM" (24-hr) to "H:MM AM/PM" for display. */
function fmt24to12(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  const period = h < 12 ? 'AM' : 'PM';
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2, '0')} ${period}`;
}

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

/** Rewrite a loopback host to the host the page was served from.
    config.json is written for the show machine, but the UI is usually opened
    from a laptop -- where "localhost" would point at the laptop itself. */
function rewriteHost(url) {
  if (!url) return url;
  try {
    const u = new URL(url);
    if (u.hostname === 'localhost' || u.hostname === '127.0.0.1' || u.hostname === '::1') {
      u.hostname = window.location.hostname;
    }
    return u.toString();
  } catch {
    return url;
  }
}

// --- API helpers -----------------------------------------------------------

async function apiGet(path) {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function apiSend(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(BASE + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
const apiPost   = (path, body) => apiSend('POST', path, body);
const apiPut    = (path, body) => apiSend('PUT', path, body);
const apiDelete = (path)       => apiSend('DELETE', path);

// --- State -----------------------------------------------------------------

let currentScenes = [];
let currentActive = null;
let currentSchedules = [];
let sceneMeta = { count: 0, max_device_scenes: 16, over_device_limit: false };
let editingScheduleId = null;   // id of the schedule row being inline-edited

// --- Render: banner --------------------------------------------------------

function renderBanner() {
  const banner   = document.getElementById('active-banner');
  const nameEl   = document.getElementById('banner-scene-name');
  const selector = document.getElementById('banner-scene-select');

  if (currentActive) {
    banner.className = 'has-scene';
    nameEl.textContent = currentActive;
  } else {
    banner.className = 'no-scene';
    nameEl.textContent = '— none —';
  }

  const prev = selector.value;
  selector.innerHTML = '<option value="">— none (clear) —</option>';
  currentScenes.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    selector.appendChild(opt);
  });
  if (prev && currentScenes.includes(prev)) {
    selector.value = prev;
  } else if (currentActive) {
    selector.value = currentActive;
  }
}

// --- Render: scene list ----------------------------------------------------

function renderScenes() {
  const list     = document.getElementById('scene-list');
  const schedSel = document.getElementById('sched-scene-select');
  const countEl  = document.getElementById('scene-count');

  const prevSched = schedSel.value;
  schedSel.innerHTML = '<option value="">— choose —</option>';

  // Scene count against the per-device MAX_SCENES ceiling. The limit is
  // per-device and this list is fleet-wide, so going over is a warning rather
  // than an error -- but no single MUR could hold them all.
  const max = sceneMeta.max_device_scenes;
  countEl.textContent = `${sceneMeta.count} scene${sceneMeta.count === 1 ? '' : 's'} / ${max} per-device limit`;
  countEl.className = 'scene-count' + (sceneMeta.over_device_limit ? ' over-limit' : '');
  if (sceneMeta.over_device_limit) {
    countEl.textContent += ' — no MUR can hold them all (MAX_SCENES)';
  }

  if (currentScenes.length === 0) {
    list.innerHTML = '<div class="empty-state">No scenes yet. Create one above!</div>';
    document.getElementById('scenes-refresh-time').textContent = nowLabel();
    return;
  }

  list.innerHTML = '';
  currentScenes.forEach(name => {
    const isActive = name === currentActive;

    const row = document.createElement('div');
    row.className = 'scene-row' + (isActive ? ' is-active' : '');

    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.textContent = name;
    row.appendChild(nameSpan);

    if (isActive) {
      const badge = document.createElement('span');
      badge.className = 'active-badge';
      badge.textContent = 'ACTIVE';
      row.appendChild(badge);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-danger btn-sm';
    delBtn.textContent = '\u{1F5D1}';
    delBtn.title = isActive ? 'Cannot delete the active scene' : `Delete "${name}"`;
    delBtn.disabled = isActive;
    delBtn.addEventListener('click', () => deleteScene(name));
    row.appendChild(delBtn);

    list.appendChild(row);

    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    schedSel.appendChild(opt);
  });

  if (prevSched && currentScenes.includes(prevSched)) schedSel.value = prevSched;
  document.getElementById('scenes-refresh-time').textContent = nowLabel();
}

// --- Render: schedules -----------------------------------------------------

function renderSchedules() {
  // Don't clobber an in-progress inline edit on auto-refresh.
  if (editingScheduleId !== null) return;

  const list = document.getElementById('sched-list');

  if (currentSchedules.length === 0) {
    list.innerHTML = '<div class="empty-state">No schedules yet. Add one above!</div>';
    document.getElementById('sched-refresh-time').textContent = nowLabel();
    return;
  }

  list.innerHTML = '';
  [...currentSchedules]
    .sort((a, b) => a.time.localeCompare(b.time))
    .forEach(s => {
      const row = document.createElement('div');
      row.className = 'sched-row';
      row.dataset.id = s.id;

      const info = document.createElement('div');
      info.className = 'sched-info';
      info.innerHTML = `
        <div class="sched-scene">${esc(s.scene)}</div>
        <div class="sched-meta">
          ⏰ ${esc(fmt24to12(s.time))}
          ${s.last_fired ? `&nbsp;|&nbsp; Last fired: ${esc(fmtTime(s.last_fired))}` : ''}
        </div>`;

      const badge = document.createElement('span');
      badge.className = `repeat-badge ${s.repeat}`;
      badge.textContent = s.repeat === 'daily' ? '\u{1F501} Daily' : '1️⃣ Once';

      const editBtn = document.createElement('button');
      editBtn.className = 'btn btn-ghost btn-sm';
      editBtn.textContent = '✏️ Edit';
      editBtn.title = 'Edit this schedule';
      editBtn.addEventListener('click', () => startEditSchedule(s));

      const delBtn = document.createElement('button');
      delBtn.className = 'btn btn-danger btn-sm';
      delBtn.textContent = '\u{1F5D1}';
      delBtn.title = 'Delete this schedule';
      delBtn.addEventListener('click', () => deleteSchedule(s.id));

      row.appendChild(info);
      row.appendChild(badge);
      row.appendChild(editBtn);
      row.appendChild(delBtn);
      list.appendChild(row);
    });

  document.getElementById('sched-refresh-time').textContent = nowLabel();
}

// --- Inline schedule editing ----------------------------------------------

function startEditSchedule(s) {
  editingScheduleId = s.id;

  const row = document.querySelector(`.sched-row[data-id="${s.id}"]`);
  if (!row) return;

  row.className = 'sched-row editing';
  row.innerHTML = '';

  const sceneSelect = document.createElement('select');
  currentScenes.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    if (name === s.scene) opt.selected = true;
    sceneSelect.appendChild(opt);
  });

  const timeInput = document.createElement('input');
  timeInput.type = 'time';
  timeInput.value = s.time;

  const repeatSelect = document.createElement('select');
  ['daily', 'once'].forEach(val => {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = val === 'daily' ? '\u{1F501} Daily' : '1️⃣ Once';
    if (val === s.repeat) opt.selected = true;
    repeatSelect.appendChild(opt);
  });

  const fields = document.createElement('div');
  fields.className = 'edit-fields';
  fields.appendChild(sceneSelect);
  fields.appendChild(timeInput);
  fields.appendChild(repeatSelect);

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-success btn-sm';
  saveBtn.textContent = '\u{1F4BE} Save';
  saveBtn.addEventListener('click', async () => {
    const scene  = sceneSelect.value;
    const time   = timeInput.value;
    const repeat = repeatSelect.value;
    if (!time) { toast('Please enter a time.', 'error'); return; }
    try {
      await apiPut(`/api/schedules/${s.id}`, { scene, time, repeat });
      toast('Schedule updated.', 'success');
      editingScheduleId = null;
      await loadAll();
    } catch (e) { toast(e.message, 'error'); }
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-ghost btn-sm';
  cancelBtn.textContent = '✕ Cancel';
  cancelBtn.addEventListener('click', () => {
    editingScheduleId = null;
    renderSchedules();   // restore normal view from cached data
  });

  row.appendChild(fields);
  row.appendChild(saveBtn);
  row.appendChild(cancelBtn);
}

// --- Health + dispatch status ---------------------------------------------

async function loadHealth() {
  const healthEl   = document.getElementById('health-display');
  const badge      = document.getElementById('push-badge');
  const detail     = document.getElementById('dispatch-detail');

  try {
    const h = await apiGet('/health');

    healthEl.innerHTML =
      `Status: <strong style="color:#5cff8f">${esc(h.status)}</strong> &nbsp;|&nbsp; ` +
      `Scenes: <strong>${h.scenes_count}</strong> &nbsp;|&nbsp; ` +
      `Schedules: <strong>${h.schedules_count}</strong> &nbsp;|&nbsp; ` +
      `Active: <strong>${esc(h.active_scene || 'none')}</strong>` +
      (h.ephemeral ? ' &nbsp;|&nbsp; <strong style="color:#ffd8a0">EPHEMERAL</strong>' : '');

    // SceneChange dispatch state
    if (!h.trigger_server_url) {
      badge.className = 'svc-badge svc-badge-warn';
      badge.textContent = 'Disabled';
    } else if (h.last_push_ok === true) {
      badge.className = 'svc-badge svc-badge-ok';
      badge.textContent = 'OK';
    } else if (h.last_push_ok === false) {
      badge.className = 'svc-badge svc-badge-error';
      badge.textContent = 'Last push failed';
    } else {
      badge.className = 'svc-badge svc-badge-info';
      badge.textContent = 'No push yet';
    }

    let regLine;
    if (h.registration_supported === false) {
      regLine = 'not supported by this trigger server (expected with ' +
                'mock-trigger-server and mur-conductor)';
    } else if (h.trigger_server_registered) {
      regLine = 'advertised upstream';
    } else {
      regLine = 'not yet registered';
    }

    detail.innerHTML =
      `Trigger server: <strong>${esc(h.trigger_server_url || 'disabled')}</strong><br>` +
      `SceneChange registration: <strong>${esc(regLine)}</strong><br>` +
      `State file: <strong>${esc(h.scenes_file || 'none (ephemeral)')}</strong>`;

    document.getElementById('svc-refresh-time').textContent = nowLabel();
  } catch {
    healthEl.textContent = 'Could not reach service.';
    badge.className = 'svc-badge svc-badge-error';
    badge.textContent = 'Unreachable';
    detail.textContent = 'Could not reach service.';
  }
}

// --- Full data load --------------------------------------------------------

async function loadAll() {
  try {
    const data = await apiGet('/api/scenes');
    currentScenes = data.scenes || [];
    currentActive = data.active_scene || null;
    sceneMeta = {
      count: data.count || 0,
      max_device_scenes: data.max_device_scenes || 16,
      over_device_limit: !!data.over_device_limit,
    };
    renderBanner();
    renderScenes();
  } catch (e) {
    document.getElementById('scene-list').innerHTML =
      `<div class="empty-state" style="color:#dc3545">Error: ${esc(e.message)}</div>`;
  }

  try {
    const data = await apiGet('/api/schedules');
    currentSchedules = data.schedules || [];
    renderSchedules();
  } catch (e) {
    document.getElementById('sched-list').innerHTML =
      `<div class="empty-state" style="color:#dc3545">Error: ${esc(e.message)}</div>`;
  }

  loadHealth();
}

// --- Actions ---------------------------------------------------------------

async function createScene() {
  const input = document.getElementById('new-scene-name');
  const name = input.value.trim();
  if (!name) { toast('Please enter a scene name.', 'error'); return; }
  try {
    const res = await apiPost('/api/scenes', { name });
    toast(`Scene "${name}" created.`, 'success');
    if (res.warning) toast(res.warning, 'error');
    input.value = '';
    await loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteScene(name) {
  if (!confirm(`Delete scene "${name}"?`)) return;
  try {
    await apiDelete(`/api/scenes/${encodeURIComponent(name)}`);
    toast(`Scene "${name}" deleted.`, 'success');
    await loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

async function setActiveSceneFromBanner() {
  const name = document.getElementById('banner-scene-select').value || null;
  try {
    await apiPost('/api/scenes/active', { name });
    toast(name ? `Active scene set to "${name}".` : 'Active scene cleared.', 'success');
    await loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

async function addSchedule() {
  const scene  = document.getElementById('sched-scene-select').value;
  const time   = document.getElementById('sched-time').value;
  const repeat = document.getElementById('sched-repeat').value;
  if (!scene) { toast('Please select a scene.', 'error'); return; }
  if (!time)  { toast('Please enter a time.', 'error'); return; }
  try {
    await apiPost('/api/schedules', { scene, time, repeat });
    toast(`Schedule added: "${scene}" @ ${time} (${repeat}).`, 'success');
    document.getElementById('sched-time').value = '';
    await loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteSchedule(id) {
  if (!confirm('Delete this schedule?')) return;
  try {
    await apiDelete(`/api/schedules/${id}`);
    toast('Schedule deleted.', 'success');
    await loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

// --- Related services links ------------------------------------------------

function initServiceLinks() {
  document.querySelectorAll('#svc-links .svc-link').forEach(a => {
    const url = rewriteHost(a.dataset.url);
    a.href = url;
    a.title = url;
  });
}

// --- Event bindings + init -------------------------------------------------

document.getElementById('create-scene-btn').addEventListener('click', createScene);
document.getElementById('new-scene-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') createScene();
});
document.getElementById('banner-set-btn').addEventListener('click', setActiveSceneFromBanner);
document.getElementById('add-sched-btn').addEventListener('click', addSchedule);
document.getElementById('refresh-btn').addEventListener('click', loadAll);

initServiceLinks();
loadAll();
setInterval(loadAll, 30000);
