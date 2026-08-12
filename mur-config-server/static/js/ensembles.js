/*
 * Ensembles page - mur-conductor group status, playlist editing, file sync.
 *
 * Status polls every 2 s (cheap: the conductor derives it from the gateway, so
 * no device is contacted). File inventory is fetched only on demand, because
 * that one does hit each device's HTTP server.
 */

const POLL_MS = 2000;

// Per-group UI state that must survive a status refresh.
const uiState = {};   // group -> {dirty, playlist, files, syncing, showFiles}

function st(group) {
  if (!uiState[group]) {
    uiState[group] = { dirty: false, playlist: null, files: null,
                       syncing: false, showFiles: false,
                       // Which inline panel is open, or null. While it is set,
                       // renderGroup leaves this card's DOM alone so the 2 s
                       // poll cannot destroy inputs the operator is typing in.
                       editing: null, readiness: null };
  }
  return uiState[group];
}

/*
 * Page-local notifications, mirroring showMessage() in device_detail.js.
 * Group CRUD has far more failure modes than a playlist save did, and a failed
 * persist needs somewhere louder than an alert() the operator dismisses.
 */
function showMessage(message, type = 'info') {
  const el = document.getElementById('messageArea');
  if (!el) { alert(message); return; }
  const palette = {
    success: ['#d7f2dd', '#14652b', '#8fd9a4'],
    error:   ['#fadbde', '#8b1520', '#f0a8b0'],
    info:    ['#e7ecef', '#33475b', '#c2ced6'],
  }[type] || ['#e7ecef', '#33475b', '#c2ced6'];
  el.textContent = message;
  el.style.display = 'block';
  el.style.background = palette[0];
  el.style.color = palette[1];
  el.style.borderColor = palette[2];
  clearTimeout(showMessage._t);
  // Errors stay up longer; a failed persist must not scroll past unnoticed.
  showMessage._t = setTimeout(() => { el.style.display = 'none'; },
                              type === 'error' ? 12000 : 5000);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtMs(ms) {
  if (ms == null) return '-';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60), s = total % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

function fmtBytes(n) {
  if (!n) return '-';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

// --- status ---------------------------------------------------------------

async function poll() {
  let data;
  try {
    data = await (await fetch('/api/conductor/status')).json();
  } catch (e) {
    document.getElementById('conductorBanner').className = 'ens-banner bad';
    document.getElementById('conductorBanner').textContent =
      'Cannot reach the config server: ' + e;
    return;
  }
  renderBanner(data);
  renderGroups(data);
}

function renderBanner(data) {
  const el = document.getElementById('conductorBanner');
  if (data.error) {
    el.className = 'ens-banner bad';
    el.innerHTML = `<strong>Conductor unreachable.</strong>
      <div class="hint">${esc(data.error)}</div>`;
    return;
  }
  const g = data.gateway || {};
  const tsf = g.tsf_map || {};
  const problems = [];
  if (!g.link_connected) problems.push('no gateway link');
  if (!g.upstream_connected) problems.push('gateway does not see the conductor');
  if (!tsf.have_canonical) problems.push('no TSF reference (devices will not align)');

  // Three pills and a count. Everything that used to be spelled out here -
  // uptime, TSF sample age, fanout delay - is diagnostic detail nobody reads at
  // a glance, so it lives in tooltips. This ran to four lines on a phone.
  el.className = problems.length ? 'ens-banner bad' : 'ens-banner';
  el.innerHTML = `
    <span title="Conductor up ${fmtMs((data.uptime_seconds || 0) * 1000)}${
      g.fanout_delay_ms == null ? '' : ' · fanout ' + g.fanout_delay_ms + ' ms'}">
      gateway ${g.link_connected && g.upstream_connected
        ? '<span class="pill ok">ok</span>' : '<span class="pill bad">down</span>'}</span>
    &nbsp;&middot;&nbsp;
    <span title="Shared WiFi-TSF clock - without it devices cannot be aligned.${
      tsf.have_canonical ? ' Reference ' + tsf.canonical_age_seconds + 's old, '
        + tsf.mur_sample_count + ' sample(s).' : ''}">
      clock ${tsf.have_canonical
        ? '<span class="pill ok">ok</span>' : '<span class="pill bad">missing</span>'}</span>
    &nbsp;&middot;&nbsp;
    <span class="beat-sub">${g.device_count == null ? '?' : g.device_count} device${
      g.device_count === 1 ? '' : 's'}</span>
    ${problems.length ? `<div class="hint">${esc(problems.join(' &middot; '))}</div>` : ''}`;
}

function renderGroups(data) {
  const host = document.getElementById('groups');
  const groups = data.groups || [];
  const live = new Set(groups.map(g => 'grp-' + g.name));

  // Prune first. Cards were previously only ever created or updated, so a
  // deleted or renamed group left its card on screen until a page reload -
  // which nobody noticed while groups could only come from config.json.
  Array.from(host.children).forEach(child => {
    if (child.id && child.id.startsWith('grp-') && !live.has(child.id)) child.remove();
  });

  const empty = document.getElementById('noGroups');
  if (empty) empty.style.display = groups.length ? 'none' : 'block';

  groups.forEach(group => {
    let card = document.getElementById('grp-' + group.name);
    if (!card) {
      card = document.createElement('div');
      card.id = 'grp-' + group.name;
      card.className = 'ens-group';
      host.appendChild(card);
    }
    // The membership editor needs every group's roster to warn about a device
    // claimed by two groups, so each card keeps a reference to the whole list.
    st(group.name).allGroups = groups;
    renderGroup(card, group);
  });
}

function stateP(group) {
  if (!group.enabled) return '<span class="pill idle">disabled</span>';
  if (group.state === 'running') {
    // Deliberately not group.ready. That flag records whether the readiness
    // GATE was satisfied at start-up and is never recalculated, so a group that
    // started on the readiness timeout reads ready:false forever - even once
    // every member has since subscribed. Derive the current truth instead.
    const adrift = (group.missing_device_ids || []).length
                 + (group.unsubscribed_device_ids || []).length;
    if (!adrift) return '<span class="pill ok">playing</span>';
    return `<span class="pill warn">playing, ${adrift} device(s) silent</span>`;
  }
  if (group.state === 'waiting_readiness')
    return '<span class="pill warn">waiting for devices</span>';
  if (group.state === 'idle_no_playlist') return '<span class="pill warn">no playlist</span>';
  if (group.state === 'finished') return '<span class="pill idle">finished</span>';
  if (group.state === 'error') return '<span class="pill bad">error</span>';
  return `<span class="pill idle">${esc(group.state)}</span>`;
}

/*
 * A card is built once and then only its volatile regions are refreshed.
 *
 * The card used to rebuild its whole innerHTML on every 2 s poll, which was
 * fine when everything in it was read-only text. Now that the header carries
 * buttons and the panel holds forms, a wholesale rebuild destroys and recreates
 * elements underneath the operator: a click can land in the gap between the old
 * button being removed and the new one arriving, and any open form would lose
 * its caret. So the shell (buttons, panel container, section scaffolding) is
 * written once, and the poll touches only the regions that actually change.
 */
function renderGroup(card, group) {
  const s = st(group.name);
  s.latest = group;
  if (card.dataset.shell !== '1') {
    renderGroupShell(card, group);
    card.dataset.shell = '1';
  }
  updateGroupLive(group);
}

function renderGroupShell(card, group) {
  const g = esc(group.name);
  card.innerHTML = `
    <h2>${g} <span id="statep-${g}"></span></h2>
    <div class="ens-sub" id="sub-${g}"></div>

    <div style="margin-bottom:10px;">
      <button class="btn btn-secondary mini" onclick="openSettings('${g}')">Settings</button>
      <button class="btn btn-secondary mini" onclick="openMembers('${g}')">Members</button>
      <button class="btn btn-secondary mini" onclick="checkReadiness('${g}')">Check setup</button>
      <button class="btn mini" id="enbtn-${g}"></button>
      <button class="btn btn-danger mini" onclick="deleteGroup('${g}')">Delete</button>
    </div>

    <!-- Never written by the poll: open forms live here. -->
    <div id="panel-${g}"></div>

    <div class="ens-cols">
      <div class="ens-col">
        <!-- State-neutral: "Now playing" was a lie for a disabled group, and
             this heading is in the shell so it cannot change per state. -->
        <h3>Playback</h3>
        <div id="live-${g}"></div>
      </div>

      <div class="ens-col">
        <h3>Playlist
          <span id="dirty-${g}" class="dirty-note"></span>
        </h3>
        <div id="pl-${g}"></div>
        <div style="margin-top:8px;">
          <button class="btn btn-secondary mini" onclick="addEntry('${g}')">Add entry</button>
          <button class="btn btn-secondary mini" onclick="fitDurations('${g}')"
                  title="Re-read every entry's length from its WAV header">Fit to file length</button>
          <!-- Save/Revert are disabled unless there is something to save, so the
               button itself says whether the playlist is settled. -->
          <button class="btn btn-primary mini" id="plsave-${g}"
                  onclick="savePlaylist('${g}')" disabled>Save playlist</button>
          <button class="btn btn-secondary mini" id="plrevert-${g}"
                  onclick="revertPlaylist('${g}')" disabled>Revert</button>
        </div>
      </div>
    </div>

    <div style="margin-top:16px; border-top:1px solid #eef2f4; padding-top:12px;">
      <!-- "Copy missing files" was removed here, deliberately. It sent the
           upload with chunked framing, which the firmware cannot read, so every
           copy created an empty file at the destination and reported success.
           The framing is fixed and POST .../sync still works, but a transfer
           that holds a device for minutes does not belong behind a one-click
           button on a 2 s-polling status page: it has no progress, no cancel,
           and its lock hold makes the dashboard report the fleet offline.
           Until it has a page of its own, use device-manager/file_manager.py. -->
      <h3>Files on members
        <button class="btn btn-secondary mini" style="margin-left:8px;"
                onclick="loadFiles('${g}')">Check files</button>
      </h3>
      <!-- data-resting marks placeholder text that the poll may refresh; once
           real results land here it must never be overwritten. -->
      <div id="files-${g}"><p class="hint" data-resting="1"></p></div>
    </div>`;
}

function updateGroupLive(group) {
  const s = st(group.name);
  const g = group.name;
  const byId = (el) => document.getElementById(el);

  const statep = byId('statep-' + g);
  if (statep) statep.innerHTML = stateP(group);

  const sub = byId('sub-' + g);
  if (sub) {
    sub.innerHTML = `trigger <code>${esc(group.trigger_name)}</code> &middot;
      scene <code>${esc(group.scene_name)}</code> &middot;
      track ${group.track} &middot;
      ${group.loop_playlist ? 'looping' : 'play once'}`;
  }

  // Label and colour change with state, but the element itself must persist so
  // a click already in flight still lands on it.
  const enbtn = byId('enbtn-' + g);
  if (enbtn) {
    const label = group.enabled ? 'Disable' : 'Enable';
    if (enbtn.textContent !== label) {
      enbtn.textContent = label;
      enbtn.className = 'btn mini ' + (group.enabled ? 'btn-warning' : 'btn-success');
      enbtn.onclick = () => setEnabled(g, !group.enabled);
    } else if (!enbtn.onclick) {
      enbtn.className = 'btn mini ' + (group.enabled ? 'btn-warning' : 'btn-success');
      enbtn.onclick = () => setEnabled(g, !group.enabled);
    }
  }

  // Setup column comes from the last on-demand readiness check, not the poll -
  // that one contacts every device and must never run on a timer.
  const readiness = {};
  if (s.readiness) (s.readiness.members || []).forEach(r => { readiness[r.id] = r; });

  const members = (group.members || []).map(m => {
    let pill;
    if (!m.present) pill = '<span class="pill bad">missing</span>';
    else if (!m.subscribed) pill = '<span class="pill warn">not subscribed</span>';
    else pill = '<span class="pill ok">ready</span>';
    const prep = m.last_prep === 'ok' ? '<span class="pill ok">ok</span>'
      : m.last_prep ? `<span class="pill bad" title="${esc(m.last_prep)}">failed</span>`
      : '<span class="beat-sub">-</span>';
    const r = readiness[m.id];
    let setup = '<span class="beat-sub">not checked</span>';
    if (r) {
      const errs = (r.problems || []).filter(p => p.severity === 'error').length;
      setup = errs ? `<span class="pill bad">${errs} problem${errs > 1 ? 's' : ''}</span>`
                   : '<span class="pill ok">ok</span>';
    }
    return `<tr><td>${esc(m.id)}</td><td>${pill}</td>
      <td class="beat-sub">${esc(m.ip || '-')}</td><td>${prep}</td><td>${setup}</td></tr>`;
  }).join('');

  const next = group.next_downbeat_in_s;
  // m:ss, not raw seconds - "215s" is not a number anyone reads at a glance.
  const nextTxt = next == null ? '-' : (next < 0 ? 'now' : fmtMs(next * 1000));
  // Enabling a group whose members are not subscribed parks it here for the
  // whole readiness timeout; say so rather than showing a bare "waiting".
  const waiting = group.state === 'waiting_readiness'
    ? `<div class="hint">Waiting for members &middot; starts anyway after
       ${group.readiness_timeout_s}s</div>` : '';

  const live = byId('live-' + g);
  if (live) {
    // The filename appears ONLY when the group is actually running. A disabled
    // group headlining "<song>.wav" reads as "this is playing now" and is acted
    // on as such - the state pill in the heading is too easy to miss.
    const playing = group.enabled && group.state === 'running';
    let headline;
    if (!group.enabled)                          headline = 'Disabled';
    else if (group.state === 'idle_no_playlist')  headline = 'No playlist';
    else if (group.state === 'waiting_readiness') headline = 'Waiting for devices';
    else if (group.state === 'finished')          headline = 'Finished';
    else if (playing && group.current_file)
      headline = null;   // render the filename below
    else                                          headline = 'Idle';

    const idx = group.current_index == null ? '-' : group.current_index + 1;
    live.innerHTML = `
      <div class="beat">${headline === null
        ? esc(group.current_file.replace('/sdcard/', ''))
        : `<span class="beat-idle">${headline}</span>`}</div>
      ${playing
        ? `<div class="beat-meta"><strong>${idx}</strong> of
             ${group.playlist_length}
             &nbsp;&middot;&nbsp; next in <strong>${nextTxt}</strong>
             &nbsp;&middot;&nbsp; ${group.beat_count}
             downbeat${group.beat_count === 1 ? '' : 's'}</div>`
        : `<div class="beat-meta muted">${group.playlist_length}
             in playlist</div>`}
      ${waiting}
      ${group.missing_device_ids && group.missing_device_ids.length
        ? `<div class="hint"><strong>Missing:</strong>
           ${esc(group.missing_device_ids.join(', '))} &middot; silent until they
           reconnect</div>` : ''}
      ${group.unsubscribed_device_ids && group.unsubscribed_device_ids.length
        ? `<div class="hint"><strong>Not subscribed:</strong>
           ${esc(group.unsubscribed_device_ids.join(', '))} &middot; see
           <em>Check setup</em></div>` : ''}

      <h3 style="margin-top:16px;">Members</h3>
      <div class="table-scroll">
      <table class="ens"><thead><tr><th>Device</th><th>Status</th><th>Address</th>
        <th>Last file push</th><th>Setup</th></tr></thead>
        <tbody>${members || '<tr><td colspan="5"><em>No members yet - click '
          + '<strong>Members</strong> to add devices.</em></td></tr>'}</tbody></table>
      </div>`;
  }

  syncPlaylistControls(g);

  // Keep the files panel's placeholder honest as membership changes, but only
  // while it is still a placeholder - a loaded comparison must survive the poll.
  const filesHost = byId('files-' + g);
  const resting = filesHost && filesHost.firstElementChild
               && filesHost.firstElementChild.dataset.resting === '1'
               ? filesHost.firstElementChild : null;
  if (resting) {
    resting.innerHTML = (group.members || []).length
      ? 'Every member must hold a file for the group to play it. Click '
        + '<em>Check files</em> to compare them (this contacts each device).'
      : '<strong>This group has no members yet.</strong> File comparison and the '
        + 'playlist picker both work across the group\'s devices, so add members '
        + 'first - click <em>Members</em> above.';
  }

  // Only reload the model from the server while the operator is not mid-edit.
  if (s.playlist === null || !s.dirty) {
    s.playlist = (group.playlist || []).map(e => ({ ...e }));
    renderPlaylist(group.name);
  }
}

// --- group CRUD ----------------------------------------------------------

const NAME_RE = /^[A-Za-z0-9_-]+$/;
const RESERVED_TRIGGER = 'SceneChange';

/*
 * Names are restricted to [A-Za-z0-9_-] on both sides. Server-side it keeps
 * config.json loadable; client-side it also stops an apostrophe in a name from
 * breaking every onclick="fn('<name>')" in the card, since the HTML parser
 * decodes &#39; back to ' before the JS is parsed.
 */
function validName(value, label) {
  if (!value) return `${label} is required.`;
  if (!NAME_RE.test(value)) {
    return `${label} may only contain letters, digits, hyphen and underscore.`;
  }
  return null;
}

function validTrigger(value) {
  const bad = validName(value, 'Trigger name');
  if (bad) return bad;
  if (value === RESERVED_TRIGGER) {
    return `'${RESERVED_TRIGGER}' is reserved for the scene service and is `
         + 'special-cased by the gateway. Pick another name.';
  }
  return null;
}

function toggleCreate(show) {
  document.getElementById('createPanel').style.display = show ? 'block' : 'none';
  document.getElementById('newGroupBtn').style.display = show ? 'none' : 'inline-block';
  if (show) {
    loadConductorTriggers();
    document.getElementById('ngName').focus();
  }
}

async function loadConductorTriggers() {
  // Only names of groups that already exist, so this is a convenience, not
  // validation - a brand new group's trigger cannot be in the list yet.
  try {
    const data = await (await fetch('/api/conductor/triggers')).json();
    const names = (data.triggers || []).map(t => t.name || t).filter(Boolean);
    document.getElementById('conductorTriggers').innerHTML =
      names.map(n => `<option value="${esc(n)}"></option>`).join('');
  } catch (e) { /* datalist stays empty; never block the form */ }
}

async function createGroup() {
  const body = {
    action: 'create',
    name: document.getElementById('ngName').value.trim(),
    trigger_name: document.getElementById('ngTrigger').value.trim(),
    scene_name: document.getElementById('ngScene').value.trim(),
    track: parseInt(document.getElementById('ngTrack').value, 10) || 0,
    prep_lead_ms: parseInt(document.getElementById('ngPrep').value, 10) || 0,
    readiness_timeout_s: parseFloat(document.getElementById('ngReadiness').value) || 0,
    loop_playlist: document.getElementById('ngLoop').checked,
    enabled: document.getElementById('ngEnabled').checked,
    expected_device_ids: [],
  };
  const bad = validName(body.name, 'Group name')
           || validTrigger(body.trigger_name)
           || validName(body.scene_name, 'Scene name');
  if (bad) { showMessage(bad, 'error'); return; }

  try {
    const resp = await fetch('/api/conductor/groups',
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    const data = await resp.json();
    if (!resp.ok) { showMessage('Create failed: ' + (data.error || resp.status), 'error'); return; }
    if (reportPersist(data)) {
      showMessage(`Group '${body.name}' created. Next: add members, then Check setup.`,
                  'success');
    }
    if (data.warnings && data.warnings.length) {
      showMessage(data.warnings.join(' | '), 'info');
    }
    toggleCreate(false);
    ['ngName', 'ngTrigger', 'ngScene'].forEach(id => {
      document.getElementById(id).value = '';
    });
    poll();
  } catch (e) {
    showMessage('Create failed: ' + e, 'error');
  }
}

async function deleteGroup(name) {
  const typed = prompt(`Delete ensemble group "${name}"?\n\n`
    + 'Member devices keep their ensemble scene - this only removes the group.\n'
    + 'Type the group name to confirm:');
  if (typed === null) return;
  if (typed !== name) { showMessage('Name did not match; nothing deleted.', 'info'); return; }
  try {
    const resp = await fetch('/api/conductor/groups',
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', name }) });
    const data = await resp.json();
    if (!resp.ok) { showMessage('Delete failed: ' + (data.error || resp.status), 'error'); return; }
    if (reportPersist(data)) {
      // A prep already in flight is not cancelled and can land for a few more
      // seconds, overwriting file_path on a device being reconfigured.
      showMessage(`Group '${name}' deleted. Give it a few seconds before `
        + 'reconfiguring those devices - a file push may still be in flight.', 'success');
    }
    delete uiState[name];
    poll();
  } catch (e) {
    showMessage('Delete failed: ' + e, 'error');
  }
}

/* Shared writer for every group-settings change. */
async function patchGroup(name, body, { confirmRestart = false } = {}) {
  try {
    const resp = await fetch(`/api/conductor/groups/${encodeURIComponent(name)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    const data = await resp.json();
    if (!resp.ok) {
      showMessage('Save failed: ' + (data.error || resp.status), 'error');
      return null;
    }
    if (reportPersist(data)) {
      const changed = (data.changed || []).join(', ') || 'nothing';
      showMessage(data.restarted
        ? `Saved (${changed}). The group restarted: the current entry was cut off `
          + 'and the playlist starts again at entry 1.'
        : `Saved (${changed}). ${data.note || ''}`,
        'success');
    }
    if (data.warnings && data.warnings.length) showMessage(data.warnings.join(' | '), 'info');
    if (data.renamed_from && data.renamed_from !== data.group) {
      uiState[data.group] = uiState[data.renamed_from] || {};
      delete uiState[data.renamed_from];
    }
    return data;
  } catch (e) {
    showMessage('Save failed: ' + e, 'error');
    return null;
  }
}

async function setEnabled(name, enable) {
  if (enable && !confirm(`Enable "${name}"?\n\n`
      + 'It will start sending downbeats. Devices that are not set up yet stay '
      + 'silent, so this is safe - but check their setup first if you expect sound.')) {
    return;
  }
  if (!enable && !confirm(`Disable "${name}"?\n\n`
      + 'Devices finish the file they are playing and then fall silent.')) {
    return;
  }
  const s = st(name);
  s.editing = null;
  await patchGroup(name, { enabled: enable });
  poll();
}

// --- group settings panel ------------------------------------------------

function closePanel(name) {
  const s = st(name);
  s.editing = null;
  // Configure outcomes are scoped to this panel session; keeping them would show
  // "last configure attempt" long after it stopped being relevant.
  s.configureResult = null;
  const host = document.getElementById('panel-' + name);
  if (host) host.innerHTML = '';
  poll();
}

function openSettings(name) {
  const s = st(name);
  const g = s.latest;
  if (!g) return;
  s.editing = 'settings';
  loadConductorTriggers();
  const host = document.getElementById('panel-' + name);
  const n = esc(name);
  const flag = '<span class="restart-flag" title="Changing this restarts the group: '
             + 'the current entry is cut off and the playlist starts again at entry 1. '
             + 'Members also need their device setup updated to match.">'
             + '&#9888;</span>';
  host.innerHTML = `
    <div class="ens-panel">
      <h4>Settings</h4>
      <div class="ens-form-row">
        <label>Group name</label>
        <input type="text" id="set-name-${n}" value="${esc(g.name)}">
      </div>
      <div class="ens-form-row">
        <label>Trigger name ${flag}</label>
        <input type="text" id="set-trigger-${n}" value="${esc(g.trigger_name)}"
               list="conductorTriggers">
      </div>
      <div class="ens-form-row">
        <label>Scene name ${flag}</label>
        <input type="text" id="set-scene-${n}" value="${esc(g.scene_name)}">
      </div>
      <div class="ens-form-row">
        <label>Track ${flag}</label>
        <input type="number" id="set-track-${n}" value="${g.track}" min="0" max="2">
      </div>
      <div class="ens-form-row">
        <label>Prep lead (ms)</label>
        <input type="number" id="set-prep-${n}" value="${g.prep_lead_ms}" min="0" step="500">
      </div>
      <div class="ens-form-row">
        <label>Readiness timeout (s)</label>
        <input type="number" id="set-readiness-${n}" value="${g.readiness_timeout_s}"
               min="0" step="10">
      </div>
      <div class="ens-form-row">
        <label>Loop the playlist</label>
        <input type="checkbox" id="set-loop-${n}" ${g.loop_playlist ? 'checked' : ''}>
      </div>
      <div style="margin-top:10px;">
        <button class="btn btn-primary mini" onclick="saveSettings('${n}')">Save</button>
        <button class="btn btn-secondary mini" onclick="closePanel('${n}')">Cancel</button>
      </div>
    </div>`;
}

async function saveSettings(name) {
  const n = name;
  const body = {
    name: document.getElementById(`set-name-${n}`).value.trim(),
    trigger_name: document.getElementById(`set-trigger-${n}`).value.trim(),
    scene_name: document.getElementById(`set-scene-${n}`).value.trim(),
    track: parseInt(document.getElementById(`set-track-${n}`).value, 10) || 0,
    prep_lead_ms: parseInt(document.getElementById(`set-prep-${n}`).value, 10) || 0,
    readiness_timeout_s: parseFloat(document.getElementById(`set-readiness-${n}`).value) || 0,
    loop_playlist: document.getElementById(`set-loop-${n}`).checked,
  };
  const bad = validName(body.name, 'Group name')
           || validTrigger(body.trigger_name)
           || validName(body.scene_name, 'Scene name');
  if (bad) { showMessage(bad, 'error'); return; }

  const g = st(name).latest || {};
  const willRestart = body.trigger_name !== g.trigger_name
                   || body.scene_name !== g.scene_name
                   || body.track !== g.track;
  if (willRestart && g.beat_count && !confirm(
      'Changing the trigger name, scene name or track restarts the group.\n\n'
      + 'The entry playing now is cut off and the playlist starts again at entry 1. '
      + 'Members also need their device setup updated to match.\n\nContinue?')) {
    return;
  }
  st(name).editing = null;
  const data = await patchGroup(name, body);
  const finalName = (data && data.group) || name;
  const host = document.getElementById('panel-' + name);
  if (host) host.innerHTML = '';
  // A rename retires this card; the poll's pruning pass removes it.
  if (finalName !== name) delete uiState[name];
  poll();
}

// --- membership editor ---------------------------------------------------

async function openMembers(name) {
  const s = st(name);
  const g = s.latest;
  if (!g) return;
  s.editing = 'members';
  const host = document.getElementById('panel-' + name);
  host.innerHTML = '<div class="ens-panel"><h4>Members</h4>'
                 + '<p class="hint">Loading known devices&hellip;</p></div>';

  // Two sources: this server's registry (what has been scanned) and the
  // conductor (what is actually at the gateway). Neither is a superset.
  let known = [];
  try {
    const data = await (await fetch('/api/devices')).json();
    known = (data.devices || []).map(d => ({ id: d.id, ip: d.ip, status: d.status }));
  } catch (e) { /* fall back to conductor-only knowledge below */ }

  const current = (g.members || []).map(m => m.id);
  const byId = {};
  known.forEach(d => { byId[d.id] = d; });
  const memberInfo = {};
  (g.members || []).forEach(m => { memberInfo[m.id] = m; });

  // Union, so a member this server has never scanned is still listed - and so
  // still removable. Without it, such a member would be stuck in the group.
  const ids = Array.from(new Set([...known.map(d => d.id), ...current])).sort();

  // Which other groups claim each device: a track carries exactly one trigger
  // name, so two groups on the same device+track cannot both be heard.
  const elsewhere = {};
  ((s.allGroups) || []).forEach(other => {
    if (other.name === name) return;
    (other.members || []).forEach(m => {
      (elsewhere[m.id] = elsewhere[m.id] || []).push(other.name);
    });
  });

  const rows = ids.map(id => {
    const isMember = current.includes(id);
    const reg = byId[id];
    const mem = memberInfo[id];
    const ip = (mem && mem.ip) || (reg && reg.ip) || '';
    let pill;
    if (mem && mem.present) pill = '<span class="pill ok">at gateway</span>';
    else if (reg) pill = `<span class="pill ${reg.status === 'online' ? 'warn' : 'idle'}">`
                       + `${esc(reg.status || 'unknown')}</span>`;
    else pill = '<span class="pill idle">unknown to this server</span>';
    const clash = elsewhere[id] && elsewhere[id].length
      ? `<div class="check-why" title="A track carries one trigger name, so if both
         groups use the same track on this device, one group's downbeats are
         ignored.">Also in ${esc(elsewhere[id].join(', '))}</div>` : '';
    return `<tr><td><input type="checkbox" class="mem-${esc(name)}" value="${esc(id)}"
        ${isMember ? 'checked' : ''}></td>
      <td>${esc(id)}${clash}</td>
      <td class="beat-sub">${esc(ip || '-')}</td>
      <td>${pill}</td></tr>`;
  }).join('');

  const n = esc(name);
  host.innerHTML = `
    <div class="ens-panel">
      <h4>Members</h4>
      <div class="table-scroll">
      <table class="ens"><thead><tr><th></th><th>Device</th><th>Address</th>
        <th>State</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4"><em>No devices known. Run a network '
          + 'scan from the dashboard first.</em></td></tr>'}</tbody></table>
      </div>
      <p class="hint">Applies immediately. Playback is not interrupted.</p>
      <div style="margin-top:8px;">
        <button class="btn btn-primary mini" onclick="saveMembers('${n}')">Save members</button>
        <button class="btn btn-secondary mini" onclick="closePanel('${n}')">Cancel</button>
      </div>
    </div>`;
}

async function saveMembers(name) {
  const boxes = document.querySelectorAll('.mem-' + CSS.escape(name));
  const ids = Array.from(boxes).filter(b => b.checked).map(b => b.value);
  st(name).editing = null;
  await patchGroup(name, { expected_device_ids: ids });
  const host = document.getElementById('panel-' + name);
  if (host) host.innerHTML = '';
  poll();
}

// --- readiness checklist -------------------------------------------------

async function checkReadiness(name) {
  const s = st(name);
  s.editing = 'checklist';
  const host = document.getElementById('panel-' + name);
  host.innerHTML = '<div class="ens-panel"><h4>Device setup</h4>'
                 + '<p class="hint">Reading scenes from each member&hellip;</p></div>';
  try {
    const data = await (await fetch(
      `/api/ensemble/${encodeURIComponent(name)}/readiness`)).json();
    if (data.error) {
      host.innerHTML = `<div class="ens-panel"><h4>Device setup</h4>
        <p class="hint">${esc(data.error)}</p>
        <button class="btn btn-secondary mini" onclick="closePanel('${esc(name)}')">Close</button>
        </div>`;
      return;
    }
    s.readiness = data;
    renderReadiness(name, data);
    // Refresh the Setup column now rather than waiting out a poll tick.
    if (s.latest) updateGroupLive(s.latest);
  } catch (e) {
    host.innerHTML = `<div class="ens-panel"><h4>Device setup</h4>
      <p class="hint">Check failed: ${esc(e)}</p></div>`;
  }
}

function renderReadiness(name, data) {
  const host = document.getElementById('panel-' + name);
  if (!host) return;
  const n = esc(name);

  const members = (data.members || []).map(m => {
    // message + fix only. `why` is a tooltip: it is the paragraph you want the
    // first time and never again, and three lines per problem does not fit a
    // phone when a member has five of them.
    const problems = (m.problems || []).map(p => `
      <div class="check-row sev-${esc(p.severity)}" title="${esc(p.why)}">
        <div class="check-dot"></div>
        <div class="check-body">
          <div>${esc(p.message)}</div>
          <div class="check-fix">${esc(p.fix)}</div>
        </div>
      </div>`).join('');
    const errs = (m.problems || []).filter(p => p.severity === 'error').length;

    /*
     * Two chips, because these are two independent facts and each was being
     * used as the other's explanation. Whether the device holds a connection to
     * the gateway says nothing about whether it answers HTTP, and the word
     * "offline" for the first of those was simply wrong - it is the state of a
     * device sitting on the network serving its own config page perfectly well.
     * That is also the state in which the operator most needs to reach it, so
     * the device page link is offered here too.
     */
    const linkChip = m.at_gateway
      ? '<span class="pill ok" title="The gateway holds a connection from this device, so downbeats can reach it.">at gateway</span>'
      : '<span class="pill bad" title="The gateway has no connection from this device, so it cannot hear a downbeat. This says nothing about whether the device is running.">not at gateway</span>';
    const HTTP_CHIP = {
      ok:         ['ok',   'responding',     'This server reached the device over HTTP.'],
      busy:       ['warn', 'busy',           'Another request to this device was in flight. Transient.'],
      no_answer:  ['bad',  'no HTTP answer', 'We have an address for this device but it did not answer.'],
      no_address: ['bad',  'no address',     'Neither the gateway nor this server has an address for this device.'],
    };
    const [hCls, hText, hWhy] = HTTP_CHIP[m.http_state] || HTTP_CHIP.no_answer;
    const httpChip = `<span class="pill ${hCls}" title="${esc(hWhy)}">${hText}</span>`;
    const countChip = m.reachable && errs
      ? `<span class="pill bad">${errs} to fix</span>`
      : m.reachable && !errs ? '<span class="pill ok">ready</span>' : '';

    let action;
    if (m.http_state === 'no_address') {
      // The one case where trying is not possible rather than merely unlikely.
      action = `<button class="btn btn-secondary mini" disabled
                 title="There is no address to send a request to. Run a network scan from the dashboard.">Try to configure</button>`;
    } else if (!m.reachable) {
      // Enabled on purpose. This check is a snapshot and the device may have come
      // back since; refusing to try would hide that. If it really is down, the
      // attempt reports why - which is far better than a dead button.
      action = `<button class="btn btn-secondary mini"
                 onclick="configureMember('${esc(name)}','${esc(m.id)}',false)"
                 title="This device did not answer the last check. Pressing this tries anyway and reports what happens.">Try to configure</button>`;
    } else if (errs) {
      action = `<button class="btn btn-primary mini"
                 onclick="configureMember('${esc(name)}','${esc(m.id)}',false)"
                 title="Apply only the steps this device is missing, then save to SD">Configure this device</button>`;
    } else {
      action = `<button class="btn btn-secondary mini"
                 onclick="configureMember('${esc(name)}','${esc(m.id)}',true)"
                 title="Nothing looks wrong. Re-apply every setup step anyway and save to SD.">Reconfigure</button>`;
    }
    const link = m.device_page
      ? `<a class="btn btn-secondary mini" href="${esc(m.device_page)}"
           target="_blank" rel="noopener">Device page &rarr;</a>`
      : '';
    const noPage = m.device_page
      ? '' : '<span class="beat-sub">not scanned - no device page</span>';

    /*
     * Outcome of the last configure attempt for this member, rendered inline.
     * Configure is a five-step sequence and any step can fail on its own, so a
     * transient toast is not enough - "3 of 5 applied, activate failed because X"
     * has to stay on screen next to the device it happened to.
     */
    const res = (st(name).configureResult || {})[m.id];
    let outcome = '';
    if (res) {
      const okSteps = (res.applied || []).map(a =>
        `<div class="check-fix">applied: ${esc(a)}</div>`).join('');
      const badSteps = (res.failed || []).map(f =>
        `<div class="check-row sev-error"><div class="check-dot"></div><div class="check-body">
           <div><strong>${esc(f.step)}</strong> failed</div>
           <div class="check-why">${esc(f.error)}</div></div></div>`).join('');
      const nothing = !(res.applied || []).length && !(res.failed || []).length
        ? '<div class="check-why">Nothing needed changing.</div>' : '';
      outcome = `<div style="margin:6px 0 0 0;padding:6px 8px;border-left:3px solid ${
        (res.failed || []).length ? '#dc3545' : '#2e7d32'};background:#fbfcfd;">
        <div class="beat-sub">last configure attempt</div>
        ${okSteps}${badSteps}${nothing}</div>`;
    }

    return `
      <div style="margin-bottom:14px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <strong>${esc(m.id)}</strong>
          <span class="beat-sub" title="${m.ip_source === 'registry'
            ? 'From this server’s own network scan - the gateway has no connection from this device.'
            : 'The peer address of this device’s gateway connection.'}"
            >${esc(m.ip || 'no address')}</span>
          ${linkChip}${httpChip}${countChip}${action}${link}${noPage}
        </div>
        ${problems || '<div class="member-ok">Nothing to fix.</div>'}
        ${outcome}
      </div>`;
  }).join('');

  const anyFixable = (data.members || []).some(
    m => m.reachable && (m.problems || []).some(p => p.severity === 'error'));

  host.innerHTML = `
    <div class="ens-panel">
      <h4>Device setup</h4>
      <p class="hint"><strong>${data.ready_count}/${data.member_count} ready</strong>
        &middot; wants <code>${esc(data.expected.scene_name)}</code> track
        ${data.expected.track} &middot; <code>${esc(data.expected.trigger_name)}</code>
        OneShot</p>
      ${members || '<p class="hint">No members to check.</p>'}
      <p class="hint" title="Subscriptions are published by the device and the
        conductor polls every 10s, so the Status column trails a fix by a few
        seconds. The device is already fixed - don't undo it.">Status can lag
        10s</p>
      <div style="margin-top:8px;">
        ${anyFixable ? `<button class="btn btn-warning mini"
                 onclick="configureMember('${n}', null, false)"
                 title="Configure every member that needs it">Configure all</button>` : ''}
        <button class="btn btn-secondary mini" onclick="checkReadiness('${n}')">Check again</button>
        <button class="btn btn-secondary mini" onclick="closePanel('${n}')">Close</button>
      </div>
    </div>`;
}

/*
 * Apply the missing setup steps to one member, or to every member that needs
 * them when deviceId is null. The server re-checks each device first and applies
 * only what is wrong, so this is safe to press twice.
 */
async function configureMember(name, deviceId, force) {
  const who = deviceId ? `device ${deviceId}` : 'every member that needs it';
  const what = force
    ? 'Re-applies every setup step even though nothing looks wrong: track config, '
      + 'boot default, activate, save to SD.'
    : 'Creates the scene if missing, points the track at this group\'s trigger in '
      + 'OneShot mode, makes the scene active and the boot default, and saves to '
      + 'SD. Only the steps a device is actually missing are applied.';
  if (!confirm(`${force ? 'Reconfigure' : 'Configure'} ${who} for "${name}"?\n\n${what}`)) {
    return;
  }
  showMessage(`${force ? 'Reconfiguring' : 'Configuring'} ${who}...`, 'info');
  try {
    const resp = await fetch(`/api/ensemble/${encodeURIComponent(name)}/configure`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign(
          deviceId ? { device_id: deviceId } : {}, force ? { force: true } : {})) });
    const data = await resp.json();
    if (data.error) { showMessage('Configure failed: ' + data.error, 'error'); return; }

    // Keep the per-member outcome so renderReadiness can show it inline; a toast
    // alone loses which step failed on which device.
    const s = st(name);
    s.configureResult = s.configureResult || {};
    (data.members || []).forEach(m => { s.configureResult[m.id] = m; });

    const failed = (data.members || []).filter(m => m.failed.length || !m.ok);
    if (!failed.length) {
      showMessage(`Configured ${data.configured}/${data.total}. `
        + 'Status above can lag ~10s.', 'success');
    } else {
      const detail = failed.map(m => {
        const why = m.failed.length
          ? m.failed.map(f => `${f.step}: ${f.error}`).join('; ')
          : (m.remaining || []).map(p => p.code).join(', ');
        return `${m.id} - ${why || 'still not ready'}`;
      }).join(' | ');
      showMessage(`Configured ${data.configured}/${data.total}. Problems: ${detail}`
        + ' - details are on each device below.', 'error');
    }
    await checkReadiness(name);   // re-render from the device, not from hope
  } catch (e) {
    showMessage('Configure failed: ' + e, 'error');
  }
}

// --- playlist editing ----------------------------------------------------

function renderPlaylist(name) {
  const s = st(name);
  const host = document.getElementById('pl-' + name);
  if (!host) return;
  const options = (s.files && s.files.common) ? s.files.common : null;

  // The picker offers the filename intersection across members, so with no
  // members there is nothing to offer. Say that instead of silently degrading
  // to a free-text box that looks like the picker just failed to load.
  const noMembers = memberCount(name) === 0;
  const pickerNote = noMembers
    ? '<p class="hint">No members - type paths by hand.</p>'
    : (options ? '' : '<p class="hint">Tap <em>Check files</em> for a file picker.</p>');

  if (!s.playlist.length) {
    host.innerHTML = `<p class="hint">Empty - the group stays silent. Add an entry.</p>`
                   + pickerNote;
    return;
  }
  host.innerHTML = s.playlist.map((e, i) => {
    const short = String(e.file || '').replace('/sdcard/', '');
    const picker = options
      ? `<select onchange="setEntry('${esc(name)}',${i},'file',this.value)"
                 style="flex:1 1 auto;min-width:90px;padding:4px;font-size:13px;">
           ${options.map(f => `<option value="/sdcard/${esc(f)}"
             ${'/sdcard/' + f === e.file ? 'selected' : ''}>${esc(f)}</option>`).join('')}
           ${options.includes(short) ? ''
             : `<option value="${esc(e.file)}" selected>${esc(short)} (not on all)</option>`}
         </select>`
      : `<input type="text" value="${esc(e.file)}"
                onchange="setEntry('${esc(name)}',${i},'file',this.value)">`;
    // Compare the entered duration against the file's real length. Silently
    // obeying a wrong number is how an entry gets truncated or leaves dead air,
    // and that is only discoverable by listening.
    const real = knownDuration(name, e.file);
    let flag = '';
    if (e.volume === 0) {
      flag = '<span class="pill bad" title="This entry plays silently">muted</span>';
    } else if (!e.duration_ms) {
      flag = '<span class="pill bad">duration not set</span>';
    } else if (real) {
      const delta = e.duration_ms - real;
      if (delta > 1000) {
        flag = `<span class="pill warn" title="file is ${fmtMs(real)}">`
             + `${fmtMs(delta)} of silence after this file</span>`;
      } else if (delta < -1000) {
        flag = `<span class="pill bad" title="file is ${fmtMs(real)}">`
             + `cuts off ${fmtMs(-delta)}</span>`;
      } else {
        flag = `<span class="pill ok" title="matches the file header">${fmtMs(real)}</span>`;
      }
    }
    return `<div class="pl-row">
      <span class="beat-sub" style="width:16px;">${i + 1}</span>
      ${picker}
      <input type="number" value="${e.duration_ms}" min="0" step="500" title="duration ms"
             onchange="setEntry('${esc(name)}',${i},'duration_ms',this.value)">
      <input type="number" value="${e.gap_ms || 0}" min="0" step="500" title="gap after, ms"
             onchange="setEntry('${esc(name)}',${i},'gap_ms',this.value)">
      <input type="number" class="pl-vol" value="${e.volume ?? 100}" min="0" max="100"
             step="5" title="volume %"
             onchange="setEntry('${esc(name)}',${i},'volume',this.value)">
      <button onclick="moveEntry('${esc(name)}',${i},-1)" title="up">&uarr;</button>
      <button onclick="moveEntry('${esc(name)}',${i},1)" title="down">&darr;</button>
      <button onclick="removeEntry('${esc(name)}',${i})" title="remove">&times;</button>
      ${flag}
    </div>`;
  }).join('') +
  '<div class="beat-sub" style="margin-top:4px;">file &middot; duration ms &middot; gap ms '
  + '&middot; vol %</div>'
  + pickerNote;
}

function markDirty(name) {
  st(name).dirty = true;
  syncPlaylistControls(name);
}

/* Keep the dirty note and the Save/Revert enablement in one place, so there is
 * exactly one answer to "does this playlist have unsaved changes". */
function syncPlaylistControls(name) {
  const dirty = !!st(name).dirty;
  const note = document.getElementById('dirty-' + name);
  if (note) note.textContent = dirty ? 'unsaved changes' : '';
  const save = document.getElementById('plsave-' + name);
  const revert = document.getElementById('plrevert-' + name);
  if (save) {
    save.disabled = !dirty;
    save.title = dirty ? 'Applies at the next downbeat' : 'No changes to save';
  }
  if (revert) {
    revert.disabled = !dirty;
    revert.title = dirty ? 'Discard these edits' : 'No changes to discard';
  }
}

/*
 * Durations come from the WAV header, never from the operator.
 *
 * The next downbeat lands at duration_ms + gap_ms whatever the audio is doing,
 * so a wrong number truncates the entry or leaves silence - and nobody can be
 * expected to know a file's length in milliseconds. Reading the header costs a
 * 4 KB range read (~200 ms), which is cheap enough to do on every file pick.
 *
 * There is deliberately no "0 means play to the end": the conductor schedules
 * the next downbeat on an absolute deadline computed in advance, and no device
 * reports playback completion back to it. That fixed grid is what keeps the
 * fleet aligned, so the length has to be known up front.
 */
function knownDuration(name, file) {
  const short = String(file || '').replace('/sdcard/', '');
  const s = st(name);
  if (s.durations && s.durations[short] != null) return s.durations[short];
  // loadFiles() already probes anything in the playlist; reuse that.
  const fromInventory = s.files && s.files.durations && s.files.durations[short];
  if (fromInventory && fromInventory.duration_ms) return fromInventory.duration_ms;
  return null;
}

async function probeDuration(name, file) {
  const short = String(file || '').replace('/sdcard/', '');
  if (!short) return null;
  const cached = knownDuration(name, short);
  if (cached != null) return cached;

  const s = st(name);
  // Pass the size if a loaded inventory has it - saves the server an /api/files call.
  let size = 0;
  const entry = s.files && (s.files.files || []).find(f => f.name === short);
  if (entry) size = entry.size || 0;

  try {
    const url = `/api/ensemble/${encodeURIComponent(name)}/probe`
              + `?file=${encodeURIComponent(short)}${size ? '&size=' + size : ''}`;
    const data = await (await fetch(url)).json();
    if (data.error || !data.duration_ms) return null;
    s.durations = s.durations || {};
    s.durations[short] = data.duration_ms;
    return data.duration_ms;
  } catch (e) {
    return null;
  }
}

async function setEntry(name, i, field, value) {
  const s = st(name);
  if (field === 'volume') {
    // Its own branch because the generic `parseInt(...) || 0` below would turn a
    // typo into 0 = MUTE, silently. Clamp, and fall back to 100 rather than 0.
    let v = parseInt(value, 10);
    if (!Number.isFinite(v)) v = 100;
    s.playlist[i].volume = Math.max(0, Math.min(100, v));
  } else {
    s.playlist[i][field] = (field === 'file') ? value : parseInt(value, 10) || 0;
  }
  markDirty(name);
  // Re-render so a clamped value snaps back in the box, instead of the input
  // showing 150 while state holds 100.
  if (field === 'volume') renderPlaylist(name);

  // Picking a file sets its duration from the header - the entire point being
  // that the operator never types a millisecond count.
  if (field === 'file') {
    const ms = await probeDuration(name, value);
    if (ms) {
      s.playlist[i].duration_ms = ms;
      showMessage(`Duration set to ${fmtMs(ms)} from the file's header.`, 'info');
    } else {
      showMessage('Could not read that file\'s length - set the duration by hand, '
        + 'or use "Fit to file length" once the file is on every member.', 'error');
    }
    renderPlaylist(name);
  }
}

async function addEntry(name) {
  const s = st(name);
  const first = (s.files && s.files.common && s.files.common[0]) || '';
  // Still allowed with no members - authoring the playlist before the devices
  // exist is legitimate - but say why there is a text box instead of a picker,
  // rather than leaving it looking broken.
  if (memberCount(name) === 0) {
    showMessage('This group has no members yet, so filenames cannot be offered or '
      + 'checked. Add members (Members button) to pick from the files they all have.',
      'info');
  }
  const file = first ? '/sdcard/' + first : '';
  // No 30000 default. A canned duration is wrong for essentially every real
  // file (these are 3-4 minute tracks) and would silently truncate them, so
  // read the real length instead and flag it when that is not possible.
  const ms = file ? await probeDuration(name, file) : null;
  s.playlist.push({ file, duration_ms: ms || 0, gap_ms: 0, volume: 100 });
  markDirty(name);
  renderPlaylist(name);
  if (file && !ms) {
    showMessage('Added an entry, but could not read that file\'s length - set the '
      + 'duration before saving.', 'error');
  }
}

/*
 * Re-read every entry's length from its file. Fixes a playlist typed by hand,
 * and repairs one after the files on the SD cards change.
 */
async function fitDurations(name) {
  const s = st(name);
  if (!s.playlist.length) { showMessage('Playlist is empty.', 'info'); return; }
  if (memberCount(name) === 0) {
    showMessage('No members yet, so there are no files to measure. Add members first.',
                'error');
    return;
  }
  showMessage('Reading file headers…', 'info');
  let fixed = 0;
  const failed = [];
  for (let i = 0; i < s.playlist.length; i++) {
    const e = s.playlist[i];
    if (!e.file) { failed.push(`entry ${i + 1} (no file)`); continue; }
    const ms = await probeDuration(name, e.file);
    if (!ms) { failed.push(String(e.file).replace('/sdcard/', '')); continue; }
    if (e.duration_ms !== ms) { e.duration_ms = ms; fixed++; }
  }
  if (fixed) markDirty(name);
  renderPlaylist(name);
  const parts = [];
  if (fixed) parts.push(`${fixed} duration(s) corrected`);
  if (!fixed && !failed.length) parts.push('all durations already match their files');
  if (failed.length) parts.push(`could not read: ${failed.join(', ')}`);
  showMessage(parts.join('; ') + (fixed ? ' - remember to Save playlist.' : ''),
              failed.length ? 'error' : 'success');
}

function removeEntry(name, i) {
  st(name).playlist.splice(i, 1);
  markDirty(name);
  renderPlaylist(name);
}

function moveEntry(name, i, delta) {
  const pl = st(name).playlist;
  const j = i + delta;
  if (j < 0 || j >= pl.length) return;
  [pl[i], pl[j]] = [pl[j], pl[i]];
  markDirty(name);
  renderPlaylist(name);
}

function revertPlaylist(name) {
  const s = st(name);
  if (!s.dirty) return;
  s.dirty = false;
  s.playlist = null;
  syncPlaylistControls(name);
  showMessage('Edits discarded - back to the saved playlist.', 'info');
  poll();
}

async function savePlaylist(name) {
  const s = st(name);
  const bad = s.playlist.find(e => !e.file || e.duration_ms < 2000
                                || !(e.volume >= 0 && e.volume <= 100));
  if (bad) {
    showMessage('Every entry needs a file and a duration of at least 2000 ms. '
      + 'Use "Fit to file length" to read them from the files.', 'error');
    return;
  }
  try {
    const resp = await fetch(`/api/conductor/groups/${encodeURIComponent(name)}/playlist`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist: s.playlist }) });
    const body = await resp.json();
    if (!resp.ok) {
      showMessage('Save failed: ' + (body.error || resp.status), 'error');
      return;
    }
    if (reportPersist(body)) {
      const n = body.playlist_length;
      showMessage(`Playlist saved: ${n} entr${n === 1 ? 'y' : 'ies'}`
        + ' - takes effect at the next downbeat.', 'success');
    }
    s.dirty = false;
    s.playlist = null;
    syncPlaylistControls(name);
    poll();
  } catch (e) {
    showMessage('Save failed: ' + e, 'error');
  }
}

/*
 * The conductor applies a change in memory even when it cannot write
 * config.json (on Windows os.replace throws if an editor holds the file open).
 * That used to be a log line nobody read: the change worked, then vanished on
 * the next restart. Say so loudly instead.
 */
function reportPersist(body) {
  if (body && body.persisted === false) {
    showMessage('Applied, but NOT saved to config.json: '
      + (body.persist_error || 'unknown error')
      + ' - this change will be lost when the conductor restarts.', 'error');
    return false;
  }
  return true;
}

// --- file inventory and sync ---------------------------------------------

/*
 * Everything in this section is about files *on members*, so with no members
 * there is nothing to say about files. Reporting "no files" there answers a
 * question nobody asked and reads as "your devices are empty" - which sends you
 * looking at SD cards instead of at the membership list.
 */
function memberCount(name) {
  const g = st(name).latest;
  return g && g.members ? g.members.length : 0;
}

function noMembersNotice(name, what) {
  return `<p class="hint"><strong>No members.</strong> Add devices with
    <em>Members</em> above.</p>`;
}

async function loadFiles(name) {
  const host = document.getElementById('files-' + name);
  const s = st(name);

  // Short-circuit before the request: with no members the endpoint would fan
  // out to nothing and hand back an empty matrix indistinguishable from
  // "members that genuinely hold no files".
  if (memberCount(name) === 0) {
    s.files = null;
    host.innerHTML = noMembersNotice(name, 'Checking files');
    renderPlaylist(name);
    return;
  }

  host.innerHTML = '<p class="hint">Reading file lists from each device&hellip;</p>';
  try {
    // Probe durations for the files currently in the playlist so the editor can
    // offer real numbers instead of guesses.
    const inPlaylist = (s.playlist || [])
      .map(e => String(e.file || '').replace('/sdcard/', '')).filter(Boolean);
    const q = inPlaylist.length ? '?probe=' + encodeURIComponent(inPlaylist.join(',')) : '';
    const data = await (await fetch(
      `/api/ensemble/${encodeURIComponent(name)}/files${q}`)).json();
    if (data.error) { host.innerHTML = `<p class="hint">${esc(data.error)}</p>`; return; }
    s.files = data;
    renderFiles(name, data);
    renderPlaylist(name);
  } catch (e) {
    host.innerHTML = `<p class="hint">Failed: ${esc(e)}</p>`;
  }
}

function renderFiles(name, data) {
  const host = document.getElementById('files-' + name);
  const members = data.members || [];        // reachable members only
  const unreachable = data.unreachable || [];
  const missingCount = (data.files || []).filter(f => f.state === 'partial').length;

  // Three different situations used to render as the same "no files" row.
  if (!members.length && !unreachable.length) {
    host.innerHTML = noMembersNotice(name, 'This comparison');
    return;
  }
  if (!members.length) {
    host.innerHTML = `<p class="hint"><strong>No member could be reached</strong>, so
      there is nothing to compare - this does not mean they hold no files.</p>
      <p class="hint">${unreachable.map(u =>
        esc(u.id) + ' - ' + esc(u.reason)).join('<br>')}</p>`;
    return;
  }

  const rows = (data.files || []).map(f => {
    const cells = members.map(m => {
      const size = f.sizes[m];
      return `<td class="st-${f.state}">${size == null
        ? '<span class="pill bad">absent</span>' : fmtBytes(size)}</td>`;
    }).join('');
    const dur = (data.durations || {})[f.name];
    return `<tr><td><code>${esc(f.name)}</code>${dur
      ? ` <span class="beat-sub">${fmtMs(dur.duration_ms)}
          (${dur.sample_rate}Hz/${dur.channels}ch)</span>` : ''}</td>${cells}</tr>`;
  }).join('');

  host.innerHTML = `
    ${data.unreachable && data.unreachable.length
      ? `<p class="hint"><strong>Not compared:</strong> ${data.unreachable.map(u =>
          esc(u.id) + ' (' + esc(u.reason) + ')').join(', ')}</p>` : ''}
    <div class="table-scroll">
    <table class="ens matrix"><thead><tr><th>File</th>
      ${members.map(m => `<th>${esc(m)}</th>`).join('')}</tr></thead>
      <tbody>${rows || `<tr><td colspan="${members.length + 1}"><em>These
        member(s) have no files on their SD cards.</em></td></tr>`}</tbody></table>
    </div>
    <p class="hint">
      <span title="Only files present on every member can be played by the group,
        so these are what the playlist picker offers.">${data.common.length} on all
        members</span>${missingCount
        ? ` &middot; <strong title="Some members are missing these. Push them from
              a machine holding the WAVs with device-manager/file_manager.py -
              faster than device-to-device and it reports progress."
            >${missingCount} incomplete</strong>` : ''}
      <span title="Same name, different size. Never overwritten automatically:
        per-device stems sharing a filename are a legitimate setup."></span>
    </p>`;
}

/*
 * syncFiles() and pollSync() lived here. They drove POST .../sync and polled
 * /api/ensemble/sync-status into a log element inside the group card. Both are
 * gone with the button; the server endpoints remain and are the starting point
 * for a transfers page, which is where this belongs.
 */

poll();
setInterval(poll, POLL_MS);
