/* multica-agent-runtime · management web UI: shared helpers, router, daemon pill (no build step) */
'use strict';

const UI = {
  config: {},
  views: {},
  current: null,
  daemon: null,
};

const $ = (id) => document.getElementById(id);

/* --- formatting ---------------------------------------------------------------------------- */

function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function humanBytes(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let value = Number(bytes) || 0;
  let index = 0;
  while (Math.abs(value) >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(value < 10 ? 1 : 0)} ${units[index]}`;
}

function humanRate(bytesPerSecond) {
  if (bytesPerSecond == null) return '–';
  return `${humanBytes(bytesPerSecond)}/s`;
}

function relativeTime(seconds) {
  if (seconds == null) return 'unknown';
  const s = Math.max(0, Number(seconds));
  if (s < 45) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  if (s < 2592000) return `${Math.round(s / 86400)} d ago`;
  return `${Math.round(s / 2592000)} mo ago`;
}

function duration(seconds) {
  if (seconds == null) return '–';
  const s = Math.max(0, Math.floor(Number(seconds)));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

function clockTime(epochSeconds) {
  if (!epochSeconds) return '–';
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return '–';
  }
}

function shortId(id) {
  return id ? String(id).slice(0, 8) : '';
}

/* --- toasts and tooltip -------------------------------------------------------------------- */

function toast(title, detail, kind) {
  const node = document.createElement('div');
  node.className = `toast ${kind || ''}`;
  node.innerHTML = `<span class="icon"></span><span class="body"><strong>${escapeHtml(title)}</strong>${
    detail ? `<span>${escapeHtml(detail)}</span>` : ''
  }</span>`;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), kind === 'error' ? 9000 : 5000);
}

function showTooltip(html, clientX, clientY) {
  const tip = $('tooltip');
  tip.innerHTML = html;
  tip.hidden = false;
  const rect = tip.getBoundingClientRect();
  let x = clientX + 14;
  let y = clientY - rect.height - 12;
  if (x + rect.width > window.innerWidth - 8) x = clientX - rect.width - 14;
  if (y < 8) y = clientY + 16;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${y}px`;
}

function hideTooltip() {
  $('tooltip').hidden = true;
}

/* --- API ----------------------------------------------------------------------------------- */

async function api(path, options) {
  const response = await fetch(path, { headers: { Accept: 'application/json' }, ...(options || {}) });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    const error = new Error('Authentication required: reload the page and enter the WEBUI_AUTH credentials.');
    error.status = 401;
    throw error;
  }
  if (!response.ok && response.status !== 207) {
    const error = new Error(body.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

/* --- daemon pill and process-view banner (fed by whichever view loaded data last) ---------- */

function renderDaemonPill(daemon) {
  UI.daemon = daemon || UI.daemon;
  const node = $('daemon');
  node.classList.remove('up', 'busy', 'down');
  if (!daemon || !daemon.reachable) {
    node.classList.add('down');
    $('daemonText').textContent = 'daemon offline';
    node.title = `Local daemon not reachable at ${(daemon && daemon.url) || 'the configured URL'}`;
    return;
  }
  const running = Number(daemon.running_task_count || 0);
  const active = Number(daemon.active_task_count || 0);
  node.classList.add(running > 0 ? 'busy' : 'up');
  $('daemonText').textContent = running > 0 ? `${running} task${running === 1 ? '' : 's'} running` : 'daemon idle';
  node.title = `${daemon.daemon_id || 'daemon'} · ${active} active task${active === 1 ? '' : 's'} · uptime ${daemon.uptime || '?'}`;
  if (daemon.device_name) $('deviceName').textContent = daemon.device_name;
}

function renderProcessView(view) {
  const banner = $('blindBanner');
  const wasHidden = banner.hidden;
  if (!view || view.mode !== 'blind') {
    banner.hidden = true;
    if (!wasHidden) window.dispatchEvent(new Event('resize'));
    return;
  }
  const hours = Math.round((Number(view.blind_grace_seconds) || 0) / 360) / 10;
  banner.hidden = false;
  $('blindText').textContent =
    'The daemon’s processes are not visible from this container, so running tasks cannot be detected directly ' +
    '(Kubernetes: set shareProcessNamespace: true on the pod). ' +
    (view.strict
      ? `Until then anything changed within the last ${hours} h counts as in use.`
      : 'The daemon reports no running task, so only the normal grace period applies.');
  if (wasHidden) window.dispatchEvent(new Event('resize')); // the terminal panel fills the viewport
}

/* --- router -------------------------------------------------------------------------------- */

function registerView(name, view) {
  UI.views[name] = view;
}

function showView(name) {
  if (!UI.views[name]) name = 'overview';
  if (UI.current === name) return;
  const previous = UI.current;
  UI.current = name;
  document.querySelectorAll('.view').forEach((node) => {
    node.hidden = node.id !== `view-${name}`;
  });
  document.querySelectorAll('.tabs .tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.view === name));
  if (previous && UI.views[previous].hide) UI.views[previous].hide();
  if (UI.views[name].show) UI.views[name].show();
}

function navigate(name) {
  showView(name); // synchronously: callers may render into the view right away
  if (location.hash !== `#${name}`) location.hash = name;
}

window.addEventListener('hashchange', () => showView(location.hash.slice(1)));

$('refresh').addEventListener('click', () => {
  const view = UI.views[UI.current];
  if (view && view.refresh) view.refresh(true);
});

document.addEventListener('visibilitychange', () => {
  const view = UI.views[UI.current];
  if (document.visibilityState === 'visible' && view && view.refresh) view.refresh(false);
});

async function boot() {
  try {
    const health = await api('/api/health');
    UI.config = health.config || {};
  } catch (error) {
    toast('Cannot reach the web UI API', String(error.message || error), 'error');
  }
  showView(location.hash.slice(1) || 'overview');
  // the daemon pill and the banner for views that do not load agent data themselves
  api('/api/agents')
    .then((data) => {
      renderDaemonPill(data.daemon);
      renderProcessView(data.process_view);
    })
    .catch(() => {});
}

window.addEventListener('DOMContentLoaded', boot);
