/* Terminal: xterm.js sessions over a WebSocket to a PTY on the server (see terminal.py) */
'use strict';

const termState = { sessions: [], active: null, counter: 0 };

function termAvailable() {
  if (!UI.config.shell) return { ok: false, reason: `The terminal is disabled: ${UI.config.shell_reason || 'by configuration'}.` };
  if (typeof window.Terminal !== 'function') {
    return { ok: false, reason: 'xterm.js is not installed next to the web UI (WEBUI_XTERM_DIR); the image ships it.' };
  }
  return { ok: true };
}

function termTheme() {
  return {
    background: '#0b0e15',
    foreground: '#e7eaf2',
    cursor: '#6d92ff',
    cursorAccent: '#0b0e15',
    selectionBackground: 'rgba(109, 146, 255, 0.35)',
    black: '#1b2030',
    brightBlack: '#626b81',
    red: '#ff6166',
    brightRed: '#ff8a8e',
    green: '#3ddc97',
    brightGreen: '#6ee7b4',
    yellow: '#ffb224',
    brightYellow: '#ffc95c',
    blue: '#6d92ff',
    brightBlue: '#97b2ff',
    magenta: '#9a7bff',
    brightMagenta: '#b9a3ff',
    cyan: '#4cc9e6',
    brightCyan: '#86dcef',
    white: '#c9cfdc',
    brightWhite: '#ffffff',
  };
}

function renderTermTabs() {
  const tabs = $('termTabs');
  tabs.innerHTML = termState.sessions
    .map(
      (s) => `
      <div class="term-tab ${s === termState.active ? 'active' : ''} ${s.status}" role="tab" aria-selected="${s === termState.active}" data-term="${s.id}">
        <span class="dot"></span><span class="term-title" title="${escapeHtml(s.cwdLabel)}">${escapeHtml(s.title)}</span>
        <button class="term-close" data-term-close="${s.id}" aria-label="Close ${escapeHtml(s.title)}">×</button>
      </div>`,
    )
    .join('');
  $('termEmpty').hidden = termState.sessions.length > 0;
  $('termCount').hidden = termState.sessions.length === 0;
  $('termCount').textContent = String(termState.sessions.length);
}

function activateTerm(session) {
  termState.active = session;
  for (const s of termState.sessions) s.el.hidden = s !== session;
  renderTermTabs();
  if (session) {
    requestAnimationFrame(() => {
      fitTerm(session);
      session.term.focus();
    });
  }
}

function sizeTermPanel() {
  // fill the viewport below the panel's top edge (the banner above it may or may not be shown)
  const panel = document.querySelector('.term-panel');
  if (!panel || UI.current !== 'terminal') return;
  const top = panel.getBoundingClientRect().top + window.scrollY;
  panel.style.height = `${Math.max(420, window.innerHeight - top - 24)}px`;
}

window.addEventListener('resize', sizeTermPanel);

function fitTerm(session) {
  if (!session || session.el.hidden || UI.current !== 'terminal') return;
  try {
    session.fit.fit();
  } catch {
    /* not visible yet */
  }
}

function sendResize(session) {
  if (session.ws && session.ws.readyState === WebSocket.OPEN) {
    session.ws.send(JSON.stringify({ type: 'resize', cols: session.term.cols, rows: session.term.rows }));
  }
}

function connectTerm(session) {
  const params = new URLSearchParams({ mode: session.mode, cols: session.term.cols, rows: session.term.rows });
  if (session.dir && session.task) {
    params.set('dir', session.dir);
    params.set('task', session.task);
  }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/api/terminal?${params}`);
  ws.binaryType = 'arraybuffer';
  session.ws = ws;
  session.status = 'connecting';
  renderTermTabs();
  ws.onopen = () => {
    session.status = 'open';
    renderTermTabs();
    sendResize(session);
  };
  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'exit') session.exitCode = message.code;
      } catch {
        /* ignore */
      }
      return;
    }
    session.term.write(new Uint8Array(event.data));
  };
  ws.onclose = () => {
    if (session.ws !== ws) return;
    session.status = 'closed';
    renderTermTabs();
    if (!session.disposed) {
      const code = session.exitCode == null ? '' : ` (exit ${session.exitCode})`;
      session.term.write(`\r\n\x1b[2m[session ended${code} · press Enter to reconnect]\x1b[0m\r\n`);
    }
  };
}

function openTerminal(options) {
  const opts = options || {};
  navigate('terminal');
  const check = termAvailable();
  if (!check.ok) {
    toast('Terminal not available', check.reason, 'error');
    return null;
  }
  termState.counter += 1;
  const mode = opts.mode || $('termMode').value || 'shell';
  const el = document.createElement('div');
  el.className = 'term-host';
  $('termStage').appendChild(el);
  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", "DejaVu Sans Mono", monospace',
    fontSize: 13.5,
    lineHeight: 1.15,
    scrollback: 5000,
    allowProposedApi: false,
    theme: termTheme(),
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  const session = {
    id: termState.counter,
    mode: opts.dir && opts.task ? 'shell' : mode,
    dir: opts.dir || '',
    task: opts.task || '',
    title: opts.task ? opts.task : mode === 'tmux' ? 'tmux main' : `shell ${termState.counter}`,
    cwdLabel: opts.task ? `${opts.dir}/${opts.task}` : mode === 'tmux' ? 'tmux session main' : UI.config.home || '~',
    term,
    fit,
    el,
    ws: null,
    status: 'connecting',
    exitCode: null,
    disposed: false,
  };
  const encoder = new TextEncoder();
  term.onData((data) => {
    if (session.status === 'closed') {
      if (data === '\r') {
        session.exitCode = null;
        term.write('\x1b[2m[reconnecting…]\x1b[0m\r\n');
        connectTerm(session);
      }
      return;
    }
    if (session.ws && session.ws.readyState === WebSocket.OPEN) session.ws.send(encoder.encode(data));
  });
  term.onBinary((data) => {
    if (session.ws && session.ws.readyState === WebSocket.OPEN) {
      const bytes = Uint8Array.from(data, (ch) => ch.charCodeAt(0) & 0xff);
      session.ws.send(bytes);
    }
  });
  term.onResize(() => sendResize(session));
  termState.sessions.push(session);
  activateTerm(session);
  fitTerm(session);
  connectTerm(session);
  return session;
}

function closeTerm(id) {
  const index = termState.sessions.findIndex((s) => s.id === id);
  if (index < 0) return;
  const [session] = termState.sessions.splice(index, 1);
  session.disposed = true;
  try {
    if (session.ws) session.ws.close();
  } catch {
    /* ignore */
  }
  session.term.dispose();
  session.el.remove();
  if (termState.active === session) activateTerm(termState.sessions[Math.max(0, index - 1)] || null);
  else renderTermTabs();
}

$('termNew').addEventListener('click', () => openTerminal({ mode: $('termMode').value }));

$('termTabs').addEventListener('click', (event) => {
  const close = event.target.closest('[data-term-close]');
  if (close) {
    closeTerm(Number(close.dataset.termClose));
    return;
  }
  const tab = event.target.closest('[data-term]');
  if (tab) activateTerm(termState.sessions.find((s) => s.id === Number(tab.dataset.term)) || null);
});

new ResizeObserver(() => fitTerm(termState.active)).observe($('termStage'));

window.addEventListener('beforeunload', (event) => {
  if (termState.sessions.some((s) => s.status === 'open' && s.mode !== 'tmux')) {
    event.preventDefault();
    event.returnValue = '';
  }
});

registerView('terminal', {
  show() {
    sizeTermPanel();
    const check = termAvailable();
    $('termNew').disabled = !check.ok;
    $('termMode').disabled = !check.ok;
    if (!check.ok) {
      $('termEmptyText').textContent = 'Terminal not available';
      $('termEmptyHint').textContent = check.reason;
    }
    activateTerm(termState.active);
  },
  refresh() {
    fitTerm(termState.active);
  },
});
