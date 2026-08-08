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
                       syncing: false, showFiles: false };
  }
  return uiState[group];
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
    el.innerHTML = `<strong>Conductor unreachable.</strong> ${esc(data.error)}
      <div class="hint">Ensembles cannot start or advance until mur-conductor is
      running. Check <code>systemctl status mur-conductor</code>.</div>`;
    return;
  }
  const g = data.gateway || {};
  const tsf = g.tsf_map || {};
  const problems = [];
  if (!g.link_connected) problems.push('no gateway link');
  if (!g.upstream_connected) problems.push('gateway does not see the conductor');
  if (!tsf.have_canonical) problems.push('no TSF reference (devices will not align)');

  el.className = problems.length ? 'ens-banner bad' : 'ens-banner';
  el.innerHTML = `
    <strong>Conductor</strong> up ${fmtMs((data.uptime_seconds || 0) * 1000)}
    &nbsp;|&nbsp; gateway link
    ${g.link_connected ? '<span class="pill ok">connected</span>'
                       : '<span class="pill bad">down</span>'}
    &nbsp;|&nbsp; devices at gateway: ${g.device_count == null ? '?' : g.device_count}
    &nbsp;|&nbsp; shared clock
    ${tsf.have_canonical ? `<span class="pill ok">ok</span>
        <span class="beat-sub">(ref ${tsf.canonical_age_seconds}s old,
        ${tsf.mur_sample_count} sample(s))</span>`
      : '<span class="pill bad">missing</span>'}
    &nbsp;|&nbsp; fanout ${g.fanout_delay_ms == null ? '?' : g.fanout_delay_ms} ms
    ${problems.length ? `<div class="hint"><strong>Problems:</strong>
        ${esc(problems.join('; '))}</div>` : ''}`;
}

function renderGroups(data) {
  const host = document.getElementById('groups');
  const groups = data.groups || [];
  if (!groups.length) {
    host.innerHTML = `<div class="ens-group"><em>No ensemble groups configured.</em>
      <div class="hint">Add a group to mur-conductor's <code>config.json</code>
      and reload it (<code>systemctl reload mur-conductor</code>).</div></div>`;
    return;
  }
  groups.forEach(group => {
    let card = document.getElementById('grp-' + group.name);
    if (!card) {
      card = document.createElement('div');
      card.id = 'grp-' + group.name;
      card.className = 'ens-group';
      host.appendChild(card);
    }
    renderGroup(card, group);
  });
}

function stateP(group) {
  if (!group.enabled) return '<span class="pill idle">disabled</span>';
  if (group.state === 'running' && group.ready) return '<span class="pill ok">playing</span>';
  if (group.state === 'waiting_readiness')
    return '<span class="pill warn">waiting for devices</span>';
  if (group.state === 'idle_no_playlist') return '<span class="pill warn">no playlist</span>';
  if (group.state === 'finished') return '<span class="pill idle">finished</span>';
  if (group.state === 'error') return '<span class="pill bad">error</span>';
  return `<span class="pill idle">${esc(group.state)}</span>`;
}

function renderGroup(card, group) {
  const s = st(group.name);
  if (s.playlist === null || !s.dirty) s.playlist = (group.playlist || []).map(e => ({ ...e }));

  const members = (group.members || []).map(m => {
    let pill;
    if (!m.present) pill = '<span class="pill bad">missing</span>';
    else if (!m.subscribed) pill = '<span class="pill warn">not subscribed</span>';
    else pill = '<span class="pill ok">ready</span>';
    const prep = m.last_prep === 'ok' ? '<span class="pill ok">ok</span>'
      : m.last_prep ? `<span class="pill bad" title="${esc(m.last_prep)}">failed</span>`
      : '<span class="beat-sub">-</span>';
    return `<tr><td>${esc(m.id)}</td><td>${pill}</td>
      <td class="beat-sub">${esc(m.ip || '-')}</td><td>${prep}</td></tr>`;
  }).join('');

  const next = group.next_downbeat_in_s;
  const nextTxt = next == null ? '-' : (next < 0 ? 'now' : next.toFixed(0) + 's');

  // Preserve the file panel and sync log across refreshes.
  const filesPanel = document.getElementById('files-' + group.name);
  const keptFiles = filesPanel ? filesPanel.innerHTML : '';

  card.innerHTML = `
    <h2>${esc(group.name)} ${stateP(group)}</h2>
    <div class="ens-sub">
      trigger <code>${esc(group.trigger_name)}</code> &middot;
      scene <code>${esc(group.scene_name)}</code> &middot;
      track ${group.track} &middot;
      ${group.loop_playlist ? 'looping' : 'play once'}
    </div>

    <div class="ens-cols">
      <div class="ens-col">
        <h3>Now playing</h3>
        <div class="beat">${group.current_file
          ? esc(group.current_file.replace('/sdcard/', ''))
          : '<span style="font-size:18px;color:#889">nothing yet</span>'}</div>
        <div class="beat-sub">
          entry ${group.current_index == null ? '-' : group.current_index + 1}
          of ${group.playlist_length} &middot;
          next downbeat in <strong>${nextTxt}</strong> &middot;
          ${group.beat_count} downbeat(s) so far
        </div>
        ${group.missing_device_ids && group.missing_device_ids.length
          ? `<div class="hint"><strong>Missing:</strong>
             ${esc(group.missing_device_ids.join(', '))} - they are silent and will
             join at the next downbeat once they reconnect.</div>` : ''}
        ${group.unsubscribed_device_ids && group.unsubscribed_device_ids.length
          ? `<div class="hint"><strong>Connected but not subscribed:</strong>
             ${esc(group.unsubscribed_device_ids.join(', '))} - their scene is
             probably misconfigured. Run <code>setup_ensemble.py --verify</code>.</div>` : ''}

        <h3 style="margin-top:16px;">Members</h3>
        <table class="ens"><thead><tr><th>Device</th><th>Status</th><th>Address</th>
          <th>Last file push</th></tr></thead><tbody>${members}</tbody></table>
      </div>

      <div class="ens-col">
        <h3>Playlist
          <span id="dirty-${esc(group.name)}" class="dirty-note">
            ${s.dirty ? 'unsaved changes' : ''}</span>
        </h3>
        <div id="pl-${esc(group.name)}"></div>
        <div style="margin-top:8px;">
          <button class="btn btn-secondary mini"
                  onclick="addEntry('${esc(group.name)}')">Add entry</button>
          <button class="btn btn-primary mini"
                  onclick="savePlaylist('${esc(group.name)}')">Save playlist</button>
          <button class="btn btn-secondary mini"
                  onclick="revertPlaylist('${esc(group.name)}')">Revert</button>
        </div>
        <p class="hint">Saved playlists take effect at the next downbeat, so the
        current entry is never cut off. Durations should match the real file
        length: the next downbeat cuts in regardless, so too long truncates and
        too short leaves silence. Use <em>Check files</em> to fill them in from
        the WAV headers.</p>
      </div>
    </div>

    <div style="margin-top:16px; border-top:1px solid #eef2f4; padding-top:12px;">
      <h3>Files on members
        <button class="btn btn-secondary mini" style="margin-left:8px;"
                onclick="loadFiles('${esc(group.name)}')">Check files</button>
        <button class="btn btn-warning mini"
                onclick="syncFiles('${esc(group.name)}')">Copy missing files</button>
      </h3>
      <div id="files-${esc(group.name)}">${keptFiles ||
        '<p class="hint">Every member must hold a file for the group to play it. '
        + 'Click <em>Check files</em> to compare them (this contacts each device).</p>'}</div>
    </div>`;

  renderPlaylist(group.name);
}

// --- playlist editing ----------------------------------------------------

function renderPlaylist(name) {
  const s = st(name);
  const host = document.getElementById('pl-' + name);
  if (!host) return;
  const options = (s.files && s.files.common) ? s.files.common : null;

  if (!s.playlist.length) {
    host.innerHTML = '<p class="hint">Empty - the group stays silent. Add an entry.</p>';
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
    return `<div class="pl-row">
      <span class="beat-sub" style="width:16px;">${i + 1}</span>
      ${picker}
      <input type="number" value="${e.duration_ms}" min="2000" step="500" title="duration ms"
             onchange="setEntry('${esc(name)}',${i},'duration_ms',this.value)">
      <input type="number" value="${e.gap_ms || 0}" min="0" step="500" title="gap after, ms"
             onchange="setEntry('${esc(name)}',${i},'gap_ms',this.value)">
      <button onclick="moveEntry('${esc(name)}',${i},-1)" title="up">&uarr;</button>
      <button onclick="moveEntry('${esc(name)}',${i},1)" title="down">&darr;</button>
      <button onclick="removeEntry('${esc(name)}',${i})" title="remove">&times;</button>
    </div>`;
  }).join('') +
  '<div class="beat-sub" style="margin-top:4px;">file &middot; duration ms &middot; gap ms</div>';
}

function markDirty(name) {
  st(name).dirty = true;
  const el = document.getElementById('dirty-' + name);
  if (el) el.textContent = 'unsaved changes';
}

function setEntry(name, i, field, value) {
  const s = st(name);
  s.playlist[i][field] = (field === 'file') ? value : parseInt(value, 10) || 0;
  markDirty(name);
}

function addEntry(name) {
  const s = st(name);
  const first = (s.files && s.files.common && s.files.common[0]) || '';
  s.playlist.push({ file: first ? '/sdcard/' + first : '/sdcard/track1.wav',
                    duration_ms: 30000, gap_ms: 0 });
  markDirty(name);
  renderPlaylist(name);
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
  s.dirty = false;
  s.playlist = null;
  poll();
}

async function savePlaylist(name) {
  const s = st(name);
  const bad = s.playlist.find(e => !e.file || e.duration_ms < 2000);
  if (bad) {
    alert('Every entry needs a file and a duration of at least 2000 ms.');
    return;
  }
  try {
    const resp = await fetch(`/api/conductor/groups/${encodeURIComponent(name)}/playlist`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist: s.playlist }) });
    const body = await resp.json();
    if (!resp.ok) { alert('Save failed: ' + (body.error || resp.status)); return; }
    s.dirty = false;
    s.playlist = null;
    poll();
  } catch (e) {
    alert('Save failed: ' + e);
  }
}

// --- file inventory and sync ---------------------------------------------

async function loadFiles(name) {
  const host = document.getElementById('files-' + name);
  host.innerHTML = '<p class="hint">Reading file lists from each device&hellip;</p>';
  const s = st(name);
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
  const members = data.members || [];
  const missingCount = (data.files || []).filter(f => f.state === 'partial').length;

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
    <table class="ens matrix"><thead><tr><th>File</th>
      ${members.map(m => `<th>${esc(m)}</th>`).join('')}</tr></thead>
      <tbody>${rows || '<tr><td colspan="9"><em>no files</em></td></tr>'}</tbody></table>
    <p class="hint">
      ${data.common.length} file(s) present on every member (these are what the
      playlist picker offers).
      ${missingCount ? `<strong>${missingCount}</strong> file(s) are missing from at
        least one member - <em>Copy missing files</em> will queue those copies.` : ''}
      Files with the same name but different sizes are shown but never
      overwritten automatically: per-device stems sharing a filename are a
      legitimate setup, so that call is yours.
    </p>
    <div id="sync-${esc(name)}"></div>`;
}

async function syncFiles(name) {
  const host = document.getElementById('sync-' + name)
            || document.getElementById('files-' + name);
  try {
    const resp = await fetch(`/api/ensemble/${encodeURIComponent(name)}/sync`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}) });
    const body = await resp.json();
    if (body.error) { host.innerHTML = `<p class="hint">${esc(body.error)}</p>`; return; }
    if (!body.queued) {
      host.innerHTML = '<p class="hint">Nothing to copy - every member already has '
        + 'the same files.</p>';
      return;
    }
    host.innerHTML = `<p class="hint">Queued ${body.queued} copy job(s),
      roughly ${body.estimated_seconds}s. Transfers run one at a time and are
      throttled so they do not disturb playback.</p><div class="sync-log"
      id="synclog-${esc(name)}"></div>`;
    pollSync(name);
  } catch (e) {
    host.innerHTML = `<p class="hint">Sync failed: ${esc(e)}</p>`;
  }
}

async function pollSync(name) {
  const log = document.getElementById('synclog-' + name);
  if (!log) return;
  try {
    const s = await (await fetch('/api/ensemble/sync-status')).json();
    const lines = [];
    if (s.current) {
      lines.push(`copying ${s.current.filename}: ${s.current.src_id} -> `
        + `${s.current.dst_id} (${fmtBytes(s.current.size)})`);
    }
    if (s.queued) lines.push(`${s.queued} job(s) waiting`);
    (s.done || []).slice(-12).reverse().forEach(d => {
      lines.push(`${d.status === 'ok' ? 'OK  ' : 'FAIL'} ${d.filename}: `
        + `${d.src_id} -> ${d.dst_id}${d.error ? ' (' + d.error + ')' : ''}`);
    });
    log.innerHTML = lines.map(esc).join('<br>')
      || '<em>no transfers yet</em>';
    if (s.running || s.current) setTimeout(() => pollSync(name), 1500);
    else lines.length && log.insertAdjacentHTML('beforeend',
      '<br><strong>done - click Check files to confirm</strong>');
  } catch (e) {
    log.textContent = 'status unavailable: ' + e;
  }
}

poll();
setInterval(poll, POLL_MS);
