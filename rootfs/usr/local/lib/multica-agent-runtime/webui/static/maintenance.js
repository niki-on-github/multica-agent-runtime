/* Maintenance: Docker prune, Nix GC, old workspace data, automatic cleanup schedule, history */
'use strict';

(() => {
  const state = { data: null, timer: null, dirty: false, lastFinished: null };
  const STEP_LABEL = { workspaces: 'Workspaces', docker: 'Docker', nix: 'Nix', trash: 'Trash' };

  /* --- settings form <-> object ------------------------------------------------------------ */

  function getPath(obj, path) {
    return path.split('.').reduce((acc, key) => (acc == null ? undefined : acc[key]), obj);
  }

  function setPath(obj, path, value) {
    const keys = path.split('.');
    let cur = obj;
    keys.slice(0, -1).forEach((key) => {
      cur[key] = cur[key] || {};
      cur = cur[key];
    });
    cur[keys[keys.length - 1]] = value;
  }

  function fillForm(settings) {
    const form = $('mntForm');
    for (const el of form.elements) {
      if (!el.name) continue;
      const value = getPath(settings, el.name);
      if (el.type === 'checkbox') el.checked = Boolean(value);
      else if (value !== undefined) el.value = String(value);
    }
    syncForm();
  }

  function readForm() {
    const out = {};
    for (const el of $('mntForm').elements) {
      if (!el.name) continue;
      let value = el.type === 'checkbox' ? el.checked : el.value;
      if (el.type === 'number') value = Number(el.value);
      setPath(out, el.name, value);
    }
    return out;
  }

  function syncForm() {
    const form = $('mntForm');
    const mode = form.elements['schedule.mode'].value;
    form.querySelectorAll('[data-show]').forEach((node) => {
      node.hidden = node.dataset.show !== mode;
    });
    form.querySelectorAll('fieldset').forEach((fieldset) => {
      const toggle = fieldset.querySelector('legend input[type=checkbox]');
      fieldset.classList.toggle('off', toggle && !toggle.checked);
    });
  }

  /* --- rendering --------------------------------------------------------------------------- */

  function kv(rows) {
    return rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('');
  }

  function render() {
    const d = state.data;
    if (!d) return;
    const readOnly = Boolean(d.read_only);
    const running = Boolean(d.status && d.status.running);
    document.querySelectorAll('#view-maintenance [data-mnt-run]').forEach((b) => {
      b.disabled = readOnly || running;
    });
    $('mntSave').disabled = readOnly;
    $('mntBadge').hidden = !running;

    const s = d.settings;
    $('mntWs').innerHTML = kv([
      ['Rule', `task data unused for ${s.workspaces.older_than_days} days`],
      ['Trash', `${d.trash_entries} entr${d.trash_entries === 1 ? 'y' : 'ies'} in .webui-trash`],
      ['Safety', 'in-use tasks are never touched'],
    ]);

    const docker = d.docker || {};
    $('mntDockerHint').textContent = docker.available ? `older than ${s.docker.older_than_hours} h` : 'not available';
    $('mntDocker').innerHTML = docker.available
      ? (docker.rows || [])
          .map((r) => `<tr><td>${escapeHtml(r.type)}<span class="ws-dir">${escapeHtml(r.active)} active of ${escapeHtml(r.total)}</span></td><td class="num">${escapeHtml(r.size)}</td><td class="num">${escapeHtml(r.reclaimable)}</td></tr>`)
          .join('')
      : `<tr><td colspan="3" class="muted">${escapeHtml(docker.error || 'Docker CLI or socket not available here.')}</td></tr>`;

    const nix = d.nix || {};
    const scope =
      nix.shared_with_daemon === true
        ? 'same store as the daemon'
        : nix.shared_with_daemon === false
          ? 'this container only — run the webui in the daemon container (WEBUI_EMBEDDED=1) to clean the agents’ store'
          : 'this container (daemon not visible)';
    $('mntNixHint').textContent = nix.available ? `older than ${s.nix.older_than_days} days` : 'not installed';
    $('mntNix').innerHTML = kv([
      ['Filesystem', nix.fs_total ? `${humanBytes(nix.fs_total - nix.fs_free)} used · ${humanBytes(nix.fs_free)} free` : '–'],
      ['Store', scope],
    ]);
    $('mntTrash').textContent = `${d.trash_entries} entries; files a rootful nested container left as root stay.`;

    if (!state.dirty) fillForm(s);
    $('mntNext').textContent = s.enabled
      ? d.next_run
        ? `next run ${clockTime(d.next_run)} (in ${duration(d.next_run - Date.now() / 1000)})`
        : 'scheduled'
      : 'off';
    $('mntStateFile').textContent = `Saved in ${d.state_file}`;

    $('mntRunning').hidden = !running;
    if (running) {
      const st = d.status;
      $('mntRunningText').textContent = `${st.dry_run ? 'Preview' : 'Cleanup'} running: ${(st.steps || []).map((x) => (x === st.current ? `[${STEP_LABEL[x]}]` : STEP_LABEL[x])).join(' → ')} · started ${duration(Date.now() / 1000 - st.started_at)} ago`;
    }

    const history = d.history || [];
    $('mntHistoryEmpty').hidden = history.length > 0;
    $('mntHistory').innerHTML = history
      .map((h, i) => {
        const steps = Object.entries(h.steps || {})
          .map(([name, r]) => {
            const cls = r.skipped ? 'skip' : r.ok ? 'ok' : 'fail';
            const items = (r.items || []).length ? `\n${r.items.join('\n')}` : '';
            const output = r.output ? `\n${r.output}` : '';
            return `<span class="step ${cls}" title="${escapeHtml(`${r.detail || ''}${items}${output}`)}">${escapeHtml(STEP_LABEL[name] || name)}: ${escapeHtml(r.detail || '')}</span>`;
          })
          .join('');
        return `<tr data-history="${i}">
          <td class="nowrap">${escapeHtml(clockTime(h.started_at))}</td>
          <td>${escapeHtml(h.trigger)}${h.dry_run ? ' <span class="pill locked">preview</span>' : ''}</td>
          <td class="steps">${steps}</td>
          <td class="num nowrap">${h.dry_run ? '–' : escapeHtml(humanBytes(h.freed_bytes || 0))}</td>
          <td class="num">${escapeHtml(duration((h.finished_at || h.started_at) - h.started_at))}</td>
        </tr>`;
      })
      .join('');

    // a job that just finished: tell the user once
    const last = d.status && d.status.last;
    if (last && last.finished_at !== state.lastFinished) {
      if (state.lastFinished !== null) {
        toast(last.dry_run ? 'Preview finished' : 'Cleanup finished', last.dry_run ? 'See the history for what would be removed.' : `${humanBytes(last.freed_bytes || 0)} freed`, last.ok ? '' : 'error');
      }
      state.lastFinished = last.finished_at;
    } else if (state.lastFinished === null) {
      state.lastFinished = last ? last.finished_at : 0;
    }
  }

  async function load() {
    try {
      state.data = await api('/api/maintenance');
      render();
    } catch (error) {
      toast('Could not load maintenance data', String(error.message || error), 'error');
    }
    schedule();
  }

  function schedule() {
    clearTimeout(state.timer);
    const running = state.data && state.data.status && state.data.status.running;
    state.timer = setTimeout(() => {
      if (UI.current === 'maintenance' && document.visibilityState === 'visible') load();
      else schedule();
    }, running ? 2000 : 15000);
  }

  async function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-WebUI': '1' },
      body: JSON.stringify(body),
    });
  }

  /* --- wiring ------------------------------------------------------------------------------ */

  $('view-maintenance').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-mnt-run]');
    if (!button) return;
    const step = button.dataset.mntRun;
    const dry = button.dataset.dry === '1';
    if (!dry && !window.confirm(step ? `Run the ${STEP_LABEL[step]} cleanup now?` : 'Run all enabled cleanup steps now?')) return;
    try {
      await post('/api/maintenance/run', { steps: step ? [step] : null, dry_run: dry });
      toast(dry ? 'Preview started' : 'Cleanup started', step ? STEP_LABEL[step] : 'all enabled steps');
    } catch (error) {
      toast('Cannot start', String(error.message || error), 'error');
    }
    load();
  });

  $('mntForm').addEventListener('input', () => {
    state.dirty = true;
    syncForm();
  });

  $('mntForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const result = await post('/api/maintenance/settings', { settings: readForm() });
      state.dirty = false;
      toast('Settings saved', result.settings.enabled ? `next run ${clockTime(result.next_run)}` : 'automatic cleanup is off');
    } catch (error) {
      toast('Not saved', String(error.message || error), 'error');
    }
    load();
  });

  registerView('maintenance', {
    show() {
      load();
    },
    hide() {
      clearTimeout(state.timer);
    },
    refresh() {
      state.dirty = false;
      load();
    },
  });
})();
