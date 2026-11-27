/* Environment: variables of the webui container, the daemon and the running agent tasks */
'use strict';

(() => {
  const state = { source: 'webui', data: null, filter: 'all', query: '', revealed: new Map(), loading: false };

  function revealAllowed() {
    return (state.data?.mode || UI.config.env) === 'full';
  }

  async function loadSources() {
    const select = $('envSource');
    try {
      const data = await api('/api/env/sources');
      const sources = data.sources || [];
      if (!sources.some((s) => s.id === state.source)) state.source = 'webui';
      select.innerHTML = sources
        .map((s) => `<option value="${escapeHtml(s.id)}" ${s.id === state.source ? 'selected' : ''}>${escapeHtml(s.label)} (${escapeHtml(s.detail)})</option>`)
        .join('');
    } catch (error) {
      select.innerHTML = '<option value="webui">This container (webui)</option>';
      if (error.status === 403) showDisabled(error.message);
    }
  }

  function showDisabled(message) {
    $('envRows').innerHTML = '';
    $('envEmpty').hidden = false;
    $('envEmptyText').textContent = message;
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      await loadSources();
      state.data = await api(`/api/env?source=${encodeURIComponent(state.source)}`);
      state.revealed.clear();
      render();
    } catch (error) {
      state.data = null;
      showDisabled(String(error.message || error));
    } finally {
      state.loading = false;
    }
  }

  function visible() {
    const vars = state.data?.variables || [];
    const query = state.query.trim().toLowerCase();
    return vars.filter((v) => {
      if (state.filter === 'secret' && !v.secret) return false;
      if (state.filter === 'differs' && !v.differs) return false;
      if (!query) return true;
      const shown = state.revealed.get(v.name) ?? v.value;
      return v.name.toLowerCase().includes(query) || (!v.secret || state.revealed.has(v.name) ? String(shown).toLowerCase().includes(query) : false);
    });
  }

  function render() {
    const data = state.data;
    if (!data) return;
    const vars = data.variables || [];
    const hasDiff = vars.some((v) => v.differs !== undefined);
    $('envCountAll').textContent = String(vars.length);
    $('envCountSecret').textContent = String(data.secret_count || 0);
    $('envDiffersBtn').hidden = !hasDiff;
    $('envCountDiffers').textContent = String(vars.filter((v) => v.differs).length);
    if (!hasDiff && state.filter === 'differs') setFilter('all');
    $('envNote').textContent = revealAllowed()
      ? 'Secrets are masked; revealing a value is a separate request and is logged by the server.'
      : 'Secrets are masked; revealing values is disabled (WEBUI_ENV=masked or read-only mode).';
    $('envGenerated').textContent = `${data.source?.label || ''} · ${clockTime(data.generated_at)}`;

    const rows = visible();
    $('envEmpty').hidden = rows.length > 0;
    $('envEmptyText').textContent = vars.length ? 'Nothing matches the filter.' : 'No variables.';
    $('envRows').innerHTML = rows
      .map((v) => {
        const revealed = state.revealed.has(v.name);
        const value = revealed ? state.revealed.get(v.name) : v.value;
        const masked = v.secret && !revealed;
        const badges = [
          v.secret ? '<span class="pill locked">secret</span>' : '',
          v.differs ? '<span class="pill busy" title="Not the same as in the webui container">differs</span>' : '',
        ].join('');
        const toggle = v.secret && revealAllowed()
          ? `<button class="btn small ghost" data-env-reveal="${escapeHtml(v.name)}">${revealed ? 'Hide' : 'Reveal'}</button>`
          : '';
        const copy = !v.secret || revealed || revealAllowed()
          ? `<button class="btn small ghost" data-env-copy="${escapeHtml(v.name)}">Copy</button>`
          : '';
        return `
          <tr>
            <td class="env-name"><code>${escapeHtml(v.name)}</code>${badges}</td>
            <td class="env-value ${masked ? 'masked' : ''}"><code>${escapeHtml(value === '' ? '(empty)' : value)}</code>${masked ? `<span class="dim">${v.length} char${v.length === 1 ? '' : 's'}</span>` : ''}</td>
            <td class="actions">${toggle}${copy}</td>
          </tr>`;
      })
      .join('');
  }

  async function revealValue(name) {
    const result = await api('/api/env/reveal', {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-WebUI': '1' },
      body: JSON.stringify({ source: state.source, name }),
    });
    return result.value;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // http on a non-localhost origin has no clipboard API
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
  }

  function setFilter(filter) {
    state.filter = filter;
    document.querySelectorAll('[data-env-filter]').forEach((b) => b.classList.toggle('active', b.dataset.envFilter === filter));
  }

  $('view-env').addEventListener('click', async (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.envFilter) {
      setFilter(button.dataset.envFilter);
      render();
      return;
    }
    const v = (state.data?.variables || []).find((item) => item.name === (button.dataset.envReveal || button.dataset.envCopy));
    if (!v) return;
    try {
      if (button.dataset.envReveal) {
        if (state.revealed.has(v.name)) state.revealed.delete(v.name);
        else state.revealed.set(v.name, await revealValue(v.name));
        render();
      } else {
        const value = !v.secret ? v.value : state.revealed.get(v.name) ?? (await revealValue(v.name));
        await copyText(value);
        toast('Copied', v.name);
      }
    } catch (error) {
      toast('Not available', String(error.message || error), 'error');
    }
  });

  $('envSource').addEventListener('change', (event) => {
    state.source = event.target.value;
    load();
  });

  $('envSearch').addEventListener('input', (event) => {
    state.query = event.target.value;
    render();
  });

  // "Env" buttons elsewhere (agent tasks) open this view with that source
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-env-source]');
    if (!button) return;
    state.source = button.dataset.envSource;
    navigate('env');
  });

  registerView('env', {
    show() {
      load();
    },
    refresh() {
      load();
    },
  });
})();
