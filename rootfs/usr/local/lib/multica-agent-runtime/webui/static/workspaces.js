/* Workspaces: workspace and task data, in-use state, deletion */
'use strict';

(() => {
  const state = {
    data: null,
    filter: 'all',
    query: '',
    selected: new Set(), // "dir" (whole workspace) or "dir/task"
    expanded: new Set(),
    targets: [],
    busy: false,
    timer: null,
    loading: false,
  };

  const keyOf = (dir, task) => (task ? `${dir}/${task}` : dir);
  const TRASH_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>';

  function readOnly() {
    return Boolean(UI.config.read_only);
  }

  /* --- data -------------------------------------------------------------------------------- */

  async function load(force) {
    if (state.loading) return;
    state.loading = true;
    clearTimeout(state.timer);
    try {
      state.data = await api(force ? '/api/workspaces?refresh=1' : '/api/workspaces');
      if (state.data.config) UI.config = { ...UI.config, ...state.data.config };
      pruneSelection();
      render();
      renderDaemonPill(state.data.daemon);
      renderProcessView(state.data.process_view);
    } catch (error) {
      $('empty').hidden = false;
      $('emptyText').textContent = error.status === 401 ? 'Authentication required' : 'Could not load workspaces';
      $('emptyHint').textContent = String(error.message || error);
      $('rows').innerHTML = '';
      $('generatedAt').textContent = '';
      if (error.status !== 401) toast('Scan failed', String(error.message || error), 'error');
    } finally {
      state.loading = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    const seconds = Number(UI.config.refresh_seconds) || 15;
    state.timer = setTimeout(() => {
      if (UI.current !== 'workspaces') return;
      if (document.visibilityState === 'visible' && !state.busy && !$('confirmDialog').open) load(false);
      else schedule();
    }, seconds * 1000);
  }

  function lookup() {
    const workspaces = new Map();
    const tasks = new Map();
    for (const ws of state.data?.workspaces || []) {
      workspaces.set(ws.dir, ws);
      for (const task of ws.tasks || []) tasks.set(keyOf(ws.dir, task.name), { ws, task });
    }
    return { workspaces, tasks };
  }

  function pruneSelection() {
    const { workspaces, tasks } = lookup();
    for (const key of [...state.selected]) {
      const ws = workspaces.get(key);
      const entry = tasks.get(key);
      const ok = ws ? ws.deletable : entry ? entry.task.deletable : false;
      if (!ok) state.selected.delete(key);
    }
  }

  /* --- rendering --------------------------------------------------------------------------- */

  function hasDeletable(ws) {
    return ws.deletable || (ws.tasks_deletable || 0) > 0;
  }

  function visibleWorkspaces() {
    const workspaces = state.data?.workspaces || [];
    const query = state.query.trim().toLowerCase();
    return workspaces.filter((ws) => {
      const matches = state.filter === 'all' || (state.filter === 'idle' ? hasDeletable(ws) : ws.in_use);
      if (!matches) return false;
      if (!query) return true;
      const haystack = [
        ws.name,
        ws.slug,
        ws.dir,
        ws.workspace_id,
        ...(ws.tasks || []).flatMap((task) => [task.name, task.issue_id, task.task_id, task.agent_id]),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  function render() {
    const data = state.data;
    if (!data) return;
    const workspaces = data.workspaces || [];
    const totals = data.totals || {};

    $('rootPath').textContent = data.root || '';
    $('statSize').textContent = totals.size || humanBytes(totals.size_bytes || 0);
    $('statSizeHint').textContent = `${totals.task_count || 0} task directories`;
    $('statCount').textContent = String(totals.workspace_count || 0);
    $('statTaskHint').textContent = `${totals.workspace_count === 1 ? 'workspace directory' : 'workspace directories'}`;
    $('statInUse').textContent = String(totals.tasks_in_use || 0);
    $('statInUseHint').textContent = `task${totals.tasks_in_use === 1 ? '' : 's'} in use · ${totals.in_use_count || 0} workspace${totals.in_use_count === 1 ? '' : 's'}`;
    $('statReclaim').textContent = totals.deletable_task_size || humanBytes(totals.deletable_task_size_bytes || 0);
    $('statReclaimHint').textContent = `${totals.deletable_task_count || 0} idle task director${totals.deletable_task_count === 1 ? 'y' : 'ies'}`;

    renderRows();
    renderCounts(workspaces);
    renderSelection();

    $('generatedAt').textContent = `Scanned in ${Number(data.scan_seconds || 0).toFixed(2)}s · ${clockTime(data.generated_at)}`;
    $('footNote').textContent = `${readOnly() ? 'Read-only mode · ' : ''}Deletion removes local data only; the daemon creates new directories for the next task.`;
    $('selectIdleWrap').hidden = readOnly() || !workspaces.some(hasDeletable);
  }

  function renderCounts(workspaces) {
    $('countAll').textContent = String(workspaces.length);
    $('countIdle').textContent = String(workspaces.filter(hasDeletable).length);
    $('countInUse').textContent = String(workspaces.filter((ws) => ws.in_use).length);
  }

  function statusPill(item) {
    if (item.protected) return '<span class="pill locked">Protected</span>';
    if (item.in_use) return '<span class="pill busy">In use</span>';
    return '<span class="pill ok">Deletable</span>';
  }

  function renderRows() {
    const workspaces = state.data?.workspaces || [];
    const visible = visibleWorkspaces();
    const rows = $('rows');
    const empty = $('empty');
    const maxSize = Math.max(1, ...workspaces.map((ws) => ws.size_bytes || 0));

    if (!visible.length) {
      rows.innerHTML = '';
      empty.hidden = false;
      $('emptyText').textContent = workspaces.length ? 'Nothing matches this filter.' : 'No workspaces found.';
      $('emptyHint').textContent = workspaces.length
        ? 'Clear the search or switch the filter.'
        : `Nothing under ${state.data?.root || 'the workspaces root'}. New data appears here after the first task.`;
      return;
    }
    empty.hidden = true;

    rows.innerHTML = visible
      .map((ws) => {
        const selected = state.selected.has(ws.dir);
        const expanded = state.expanded.has(ws.dir);
        const fill = Math.max(2, Math.round(((ws.size_bytes || 0) / maxSize) * 64));
        const name = ws.name || ws.slug || ws.dir;
        const tasksNote = ws.tasks_in_use ? `<span class="ws-dir">${ws.tasks_in_use} in use · ${ws.tasks_deletable} idle</span>` : '';
        const action = ws.deletable
          ? `<button class="btn small danger" data-delete="${escapeHtml(ws.dir)}" ${readOnly() ? 'disabled' : ''}>Delete</button>`
          : `<button class="btn small ghost" data-expand="${escapeHtml(ws.dir)}">${ws.tasks_deletable ? 'Tasks' : 'Why?'}</button>`;
        return `
        <tr class="row ${selected ? 'selected' : ''} ${expanded ? 'expanded' : ''}" data-dir="${escapeHtml(ws.dir)}">
          <td class="c-check"><input type="checkbox" data-check="${escapeHtml(ws.dir)}" ${selected ? 'checked' : ''} ${ws.deletable && !readOnly() ? '' : 'disabled'} aria-label="Select ${escapeHtml(name)}" /></td>
          <td class="c-name">
            <button class="expand" data-expand="${escapeHtml(ws.dir)}" aria-expanded="${expanded}" aria-label="Details of ${escapeHtml(name)}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>
            </button>
            <span class="ws-cell">
              <span class="ws-name">${escapeHtml(name)}</span>
              <span class="ws-dir">${escapeHtml(ws.dir)}</span>
            </span>
          </td>
          <td class="num">
            <span class="size-wrap">
              <svg class="bar" viewBox="0 0 64 4" preserveAspectRatio="none" aria-hidden="true"><rect class="track" width="64" height="4" rx="2" /><rect class="fill" width="${fill}" height="4" rx="2" /></svg>
              <span>${escapeHtml(ws.size || humanBytes(ws.size_bytes))}</span>
            </span>
          </td>
          <td class="num tasks-col">${ws.task_count || 0}${tasksNote}</td>
          <td class="age-col"><span class="age ${ws.last_activity_seconds != null && ws.last_activity_seconds < 3600 ? 'fresh' : ''}" title="${escapeHtml(clockTime(ws.newest_mtime))}">${escapeHtml(relativeTime(ws.last_activity_seconds))}</span></td>
          <td>${statusPill(ws)}</td>
          <td class="actions">${action}</td>
        </tr>
        ${expanded ? detailsRow(ws) : ''}`;
      })
      .join('');
  }

  function taskRow(ws, task) {
    const key = keyOf(ws.dir, task.name);
    const selected = state.selected.has(key);
    const meta = [task.issue_id ? `issue ${shortId(task.issue_id)}` : '', task.agent_id ? `agent ${shortId(task.agent_id)}` : '']
      .filter(Boolean)
      .join(' · ');
    const reasons = (task.reasons || []).length
      ? `<span class="task-reasons">${task.reasons.map((r) => escapeHtml(r)).join(' · ')}</span>`
      : '';
    const shell = UI.config.shell
      ? `<button class="btn small ghost" data-shell-dir="${escapeHtml(ws.dir)}" data-shell-task="${escapeHtml(task.name)}" title="Open a terminal in this task's directory">Shell</button>`
      : '';
    const del = task.deletable && !readOnly()
      ? `<button class="btn small danger" data-delete-task="${escapeHtml(task.name)}" data-dir="${escapeHtml(ws.dir)}">Delete</button>`
      : '';
    return `
      <tr class="task-row ${selected ? 'selected' : ''}">
        <td class="c-check"><input type="checkbox" data-check="${escapeHtml(key)}" ${selected ? 'checked' : ''} ${task.deletable && !readOnly() ? '' : 'disabled'} aria-label="Select ${escapeHtml(task.name)}" /></td>
        <td><span class="ws-cell"><code class="task-name">${escapeHtml(task.name)}</code><span class="ws-dir">${escapeHtml(meta || 'no task metadata')}</span>${reasons}</span></td>
        <td class="num">${escapeHtml(task.size || humanBytes(task.size_bytes))}</td>
        <td><span class="age" title="${escapeHtml(clockTime(task.newest_mtime))}">${escapeHtml(relativeTime(task.last_activity_seconds))}</span></td>
        <td>${statusPill(task)}</td>
        <td class="actions">${shell}${del}</td>
      </tr>`;
  }

  function detailsRow(ws) {
    const tasks = ws.tasks || [];
    const idle = tasks.filter((task) => task.deletable);
    const processes = (ws.processes || []).length
      ? ws.processes
          .slice(0, 12)
          .map((proc) => `<li><code>${escapeHtml(proc.name || 'pid ' + proc.pid)}</code><span class="dim">pid ${escapeHtml(proc.pid)}${proc.terminal ? ' · web terminal' : ''}</span></li>`)
          .join('')
      : '<li class="note">no process in this workspace</li>';
    const reasons = (ws.reasons || []).length
      ? ws.reasons.map((reason) => `<li class="reason">${escapeHtml(reason)}</li>`).join('')
      : `<li class="note">${ws.protected ? 'protection list (WEBUI_PROTECT_WORKSPACES)' : 'idle: safe to delete'}</li>`;
    const selectIdle = idle.length && !readOnly()
      ? `<button class="btn small ghost" data-select-idle="${escapeHtml(ws.dir)}">Select ${idle.length} idle task${idle.length === 1 ? '' : 's'} (${escapeHtml(humanBytes(ws.tasks_deletable_size_bytes))})</button>`
      : '';
    return `
    <tr class="details-row">
      <td class="details-cell" colspan="7">
        <div class="details">
          <div class="detail-block">
            <h4>Safety checks</h4>
            <ul>${reasons}</ul>
          </div>
          <div class="detail-block">
            <h4>Processes</h4>
            <ul>${processes}</ul>
          </div>
          <div class="detail-block">
            <h4>Location</h4>
            <ul>
              <li><code>${escapeHtml(ws.path)}</code></li>
              ${ws.workspace_id ? `<li><span class="dim">workspace ${escapeHtml(ws.workspace_id)}</span></li>` : ''}
              ${ws.partial ? '<li class="reason">size scan incomplete</li>' : ''}
            </ul>
          </div>
        </div>
        <div class="tasks-head">
          <h4>Task directories (${tasks.length})</h4>
          ${selectIdle}
        </div>
        ${
          tasks.length
            ? `<table class="tasks-table"><tbody>${tasks.map((task) => taskRow(ws, task)).join('')}</tbody></table>`
            : '<p class="note">no task directories</p>'
        }
      </td>
    </tr>`;
  }

  function renderSelection() {
    const workspaces = state.data?.workspaces || [];
    const idleTasks = workspaces.flatMap((ws) => (ws.tasks || []).filter((t) => t.deletable).map((t) => keyOf(ws.dir, t.name)));
    const allSelected = idleTasks.length > 0 && idleTasks.every((key) => state.selected.has(key));
    $('selectIdle').checked = allSelected;
    $('selectIdle').indeterminate = !allSelected && state.selected.size > 0;
    const count = state.selected.size;
    const button = $('deleteSelected');
    button.disabled = count === 0 || readOnly() || state.busy;
    button.innerHTML = `${TRASH_ICON}${count ? `Delete ${count}` : 'Delete selected'}`;
  }

  /* --- selection and expansion ------------------------------------------------------------- */

  function toggleExpand(dir) {
    if (state.expanded.has(dir)) state.expanded.delete(dir);
    else state.expanded.add(dir);
    renderRows();
  }

  /* --- deletion ---------------------------------------------------------------------------- */

  function openConfirm(keys) {
    const { workspaces, tasks } = lookup();
    const targets = [];
    for (const key of keys) {
      const ws = workspaces.get(key);
      if (ws) {
        targets.push({ dir: ws.dir, task: null, label: ws.dir, size_bytes: ws.size_bytes, deletable: ws.deletable });
        continue;
      }
      const entry = tasks.get(key);
      // a task of a workspace that is deleted as a whole needs no request of its own
      if (entry && !keys.includes(entry.ws.dir)) {
        targets.push({ dir: entry.ws.dir, task: entry.task.name, label: `${entry.ws.dir}/${entry.task.name}`, size_bytes: entry.task.size_bytes, deletable: entry.task.deletable });
      }
    }
    if (!targets.length) return;
    if (targets.some((t) => !t.deletable)) {
      toast('Not deletable', 'A selected item is in use or protected.', 'error');
      return;
    }
    state.targets = targets;
    $('confirmList').innerHTML = targets
      .map((t) => `<li><code>${escapeHtml(t.label)}</code><span class="sz">${escapeHtml(humanBytes(t.size_bytes))}</span></li>`)
      .join('');
    const single = targets.length === 1;
    const expected = single ? targets[0].task || targets[0].dir : 'DELETE ALL';
    $('confirmTitle').textContent = single && targets[0].task ? 'Delete task data' : 'Delete workspace data';
    $('confirmLabel').textContent = `Type ${expected} to confirm`;
    $('confirmInput').value = '';
    $('confirmInput').placeholder = expected;
    $('confirmDelete').disabled = true;
    $('confirmDelete').textContent = single ? 'Delete' : `Delete ${targets.length} items`;
    $('confirmDialog').showModal();
    $('confirmInput').focus();
  }

  function confirmMatches() {
    if (!state.targets.length) return false;
    const expected = state.targets.length === 1 ? state.targets[0].task || state.targets[0].dir : 'DELETE ALL';
    return $('confirmInput').value.trim() === expected;
  }

  async function runDelete() {
    if (!state.targets.length || !confirmMatches() || state.busy) return;
    const items = state.targets.map((t) => (t.task ? { dir: t.dir, task: t.task, confirm: t.task } : { dir: t.dir, confirm: t.dir }));
    state.busy = true;
    $('confirmDelete').disabled = true;
    $('confirmDelete').textContent = 'Deleting…';
    try {
      const result = await api('/api/workspaces/delete', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-WebUI': '1' },
        body: JSON.stringify({ items }),
      });
      const deleted = result.deleted || [];
      const freed = deleted.reduce((sum, item) => sum + (item.size_bytes || 0), 0);
      if (deleted.length) {
        const leftovers = deleted.flatMap((item) => item.leftovers || []);
        toast(
          deleted.length === 1 ? 'Deleted' : `${deleted.length} items deleted`,
          `${humanBytes(freed)} freed${leftovers.length ? ` · ${leftovers.length} root-owned file(s) left in .webui-trash` : ''}`,
        );
        state.selected.clear();
      }
      for (const item of result.skipped || []) {
        toast(`Skipped ${item.task || item.dir}`, [item.error, ...(item.reasons || [])].filter(Boolean).join(' · '), 'error');
      }
      for (const item of result.failed || []) {
        toast(`Failed ${item.task || item.dir || 'request'}`, item.error, 'error');
      }
      $('confirmDialog').close();
    } catch (error) {
      const reasons = (error.body && error.body.reasons) || [];
      toast('Delete failed', [String(error.message || error), ...reasons].join(' · '), 'error');
    } finally {
      state.busy = false;
      $('confirmDelete').textContent = 'Delete';
      $('confirmDelete').disabled = !confirmMatches();
    }
    await load(true);
  }

  /* --- wiring ------------------------------------------------------------------------------ */

  const view = $('view-workspaces');

  view.addEventListener('click', (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.dataset.expand) {
      toggleExpand(target.dataset.expand);
    } else if (target.dataset.delete) {
      openConfirm([target.dataset.delete]);
    } else if (target.dataset.deleteTask) {
      openConfirm([keyOf(target.dataset.dir, target.dataset.deleteTask)]);
    } else if (target.dataset.selectIdle) {
      const ws = lookup().workspaces.get(target.dataset.selectIdle);
      (ws?.tasks || []).filter((t) => t.deletable).forEach((t) => state.selected.add(keyOf(ws.dir, t.name)));
      renderRows();
      renderSelection();
    }
  });

  view.addEventListener('change', (event) => {
    const checkbox = event.target;
    if (checkbox.dataset && checkbox.dataset.check !== undefined) {
      if (checkbox.checked) state.selected.add(checkbox.dataset.check);
      else state.selected.delete(checkbox.dataset.check);
      const row = checkbox.closest('tr');
      if (row) row.classList.toggle('selected', checkbox.checked);
      renderSelection();
    }
  });

  $('search').addEventListener('input', (event) => {
    state.query = event.target.value;
    renderRows();
  });

  view.querySelectorAll('.segmented button').forEach((button) => {
    button.addEventListener('click', () => {
      state.filter = button.dataset.filter;
      view.querySelectorAll('.segmented button').forEach((other) => other.classList.toggle('active', other === button));
      renderRows();
    });
  });

  $('selectIdle').addEventListener('change', (event) => {
    const workspaces = state.data?.workspaces || [];
    if (event.target.checked) {
      workspaces.forEach((ws) => (ws.tasks || []).filter((t) => t.deletable).forEach((t) => state.selected.add(keyOf(ws.dir, t.name))));
    } else {
      state.selected.clear();
    }
    renderRows();
    renderSelection();
  });

  $('deleteSelected').addEventListener('click', () => openConfirm([...state.selected]));

  $('confirmInput').addEventListener('input', () => {
    $('confirmDelete').disabled = !confirmMatches() || state.busy;
  });

  $('confirmCancel').addEventListener('click', () => $('confirmDialog').close());

  $('confirmDialog').addEventListener('close', () => {
    state.targets = [];
    $('confirmDelete').disabled = true;
  });

  // Enter in the input submits the form: only the Delete button is a submit button
  $('confirmDialog').addEventListener('submit', (event) => {
    event.preventDefault();
    if (confirmMatches()) runDelete();
  });

  registerView('workspaces', {
    show() {
      load(false);
    },
    hide() {
      clearTimeout(state.timer);
    },
    refresh(force) {
      load(Boolean(force));
    },
  });
})();
