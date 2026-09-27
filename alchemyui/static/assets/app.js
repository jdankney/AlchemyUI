// AlchemyUI SPA -- vanilla JS, no build step, no framework, no CDN.
// Served by FastAPI at /. Styling lives in styles.css (hand-written).
//
// Design brief (spec section 12): one prompt box, like ChatGPT. You type,
// optionally attach images, and get images back in a conversation. No modes,
// samplers or models to choose. Waits are minute-scale, so never a bare
// spinner: every turn says in plain words what is happening, then where its
// jobs are (queue position, then steps, elapsed and ETA, then a preview).
const $app = document.getElementById('app');

/* ------------------------------------------------------------------ api */
const api = {
  async get(p) { const r = await fetch(p); if (!r.ok) throw await err(r); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async patch(p, b) {
    const r = await fetch(p, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async del(p) { const r = await fetch(p, { method: 'DELETE' }); if (!r.ok) throw await err(r); return r.json(); },
  async upload(p, file) {
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch(p, { method: 'POST', body: fd }); if (!r.ok) throw await err(r); return r.json();
  },
};
async function err(r) {
  let d; try { d = (await r.json()).detail; } catch { d = r.statusText; }
  const e = new Error(typeof d === 'string' ? d : JSON.stringify(d)); e.status = r.status; return e;
}

/* --------------------------------------------------------------- helpers */
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'value') e.value = v;          // property, not attribute
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
};

// Inline SVG icons (stroke-based, 24-unit grid). Emoji render differently on
// every platform; these don't.
const ICONS = {
  sparkle: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z',
  star: 'M12 3l2.9 6 6.6.9-4.8 4.6 1.2 6.5-5.9-3.1-5.9 3.1 1.2-6.5L2.5 9.9 9.1 9 12 3z',
  search: 'M21 21l-4.3-4.3M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z',
  stop: 'M7 7h10v10H7z',
  x: 'M18 6L6 18M6 6l12 12',
  chevron: 'M6 9l6 6 6-6',
  down: 'M12 5v14M19 12l-7 7-7-7',
  alert: 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01',
  trash: 'M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6',
  redo: 'M3 12a9 9 0 1 0 2.6-6.4M3 4v5h5',
  image: 'M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM8.5 10.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM22 15l-5-5L6 19',
  clip: 'M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5',
  send: 'M12 19V5M5 12l7-7 7 7',
  plus: 'M12 5v14M5 12h14',
  menu: 'M3 6h18M3 12h18M3 18h18',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  face: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM8.5 10h.01M15.5 10h.01M8.5 15a5 5 0 0 0 7 0',
  corner: 'M9 14l-5-5 5-5M20 20v-7a4 4 0 0 0-4-4H4',
};
const icon = (name, cls = '') => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  s.setAttribute('class', 'icon ' + cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name]); s.append(p);
  return s;
};

const fmtDur = s => s < 60 ? `${Math.round(s)}s`
  : s < 3600 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s`
  : `${(s / 3600).toFixed(1)}h`;
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const shortCkpt = c => (c || '').split('/').pop().replace(/\.(safetensors|ckpt|pt)$/, '');
const fmtMoney = n => (n > 0 && n < 0.0001) ? '<$0.0001' : '$' + (n || 0).toFixed(n >= 1 ? 2 : 4);
const fmtKwh = n => (n > 0 && n < 0.001) ? '<0.001' : (n || 0).toFixed(3);
const ago = ts => {
  const s = Date.now() / 1000 - ts;
  return s < 60 ? 'now' : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h`
    : `${Math.floor(s / 86400)}d`;
};
const LIVE = new Set(['queued', 'running']);
const BUSY_TURN = new Set(['planning', 'queued']);

/* ----------------------------------------------------------------- state */
const S = {
  me: null,
  threads: [],             // the rail, newest first
  view: 'chat',            // chat | library | settings
  threadId: null,          // null: a new, empty chat
  thread: null,
  turns: [],
  threadError: null,
  open: new Set(),         // turn ids whose plan details are expanded
  previews: {},            // job id -> object URL of the latest preview frame
  connected: false,
  comfyConnected: true,
  depth: 0,                // jobs in the queue, everyone's
  draft: { text: '', atts: [] },   // atts: {id, url, busy}
  sending: false,
  lib: { images: [], q: '', fav: false, done: false, loading: false },
  refs: [], refsBusy: false, usage: null,
  lightbox: null,
  drawer: false,           // the rail, on a phone
  menu: false,             // the account menu
};

const turnOf = jobId => S.turns.find(t => t.jobs.some(j => j.id === jobId));
const busyTurn = () => [...S.turns].reverse().find(t => BUSY_TURN.has(t.state));
const pendingQuestion = () => {
  const last = S.turns[S.turns.length - 1];
  return last && last.state === 'needs_input' ? last : null;
};

/* ---------------------------------------------------------------- routing */
// #/ new chat · #/t/<id> a thread · #/library · #/settings
function parseRoute() {
  const r = (location.hash || '').replace(/^#\/?/, '');
  if (r.startsWith('t/')) return { view: 'chat', id: decodeURIComponent(r.slice(2)) };
  if (r === 'library') return { view: 'library' };
  if (r === 'settings') return { view: 'settings' };
  return { view: 'chat', id: null };
}

async function route() {
  const r = parseRoute();
  S.view = r.view; S.drawer = false; S.menu = false;
  if (r.view === 'chat' && (r.id !== S.threadId || (r.id && !S.thread))) {
    await openThread(r.id);
    return;
  }
  render();
  if (r.view === 'library') loadLibrary(true);
  else if (r.view === 'settings') loadSettings();
  else { stickToBottom(true); focusComposer(); }
}

function newChat() {
  S.threadId = null; S.thread = null; S.turns = []; S.threadError = null;
  if (location.hash && location.hash !== '#/') location.hash = '#/';
  else route();
}

async function openThread(id) {
  S.threadId = id; S.thread = null; S.turns = []; S.threadError = null;
  render();
  if (!id) { focusComposer(); return; }
  try {
    const d = await api.get(`/api/threads/${encodeURIComponent(id)}`);
    if (S.threadId !== id) return;
    S.thread = d.thread; S.turns = d.turns.map(remember);
  } catch (e) {
    if (S.threadId !== id) return;
    S.threadError = e.status === 404
      ? 'This conversation doesn\'t exist, or was deleted.' : e.message;
  }
  render();
  stickToBottom(true);
  focusComposer();
}

async function loadThreads() {
  try { S.threads = (await api.get('/api/threads')).threads; renderRail(); }
  catch { /* the rail stays as it was */ }
}

/* --------------------------------------------------------- SSE: one stream */
// One EventSource for the whole app. Turns, queue snapshots, job progress and
// finished images all arrive here, for this user only. EventSource brings its
// own reconnect backoff; whatever a gap swallowed is re-fetched on reopen.
function connectEvents() {
  const es = new EventSource('/api/events');
  es.onopen = () => {
    const wasDown = !S.connected;
    S.connected = true;
    if (wasDown && S.booted) resync();
    renderRail();
  };
  es.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    const on = { turn: onTurn, queue: onQueue, job: onJob, progress: onProgress,
                 preview: onPreview, image: onImage }[d.type];
    if (on) on(d);
  };
  es.onerror = () => { S.connected = false; renderRail(); };
}

async function resync() {
  await loadThreads();
  if (S.view === 'chat' && S.threadId) {
    try {
      const d = await api.get(`/api/threads/${encodeURIComponent(S.threadId)}`);
      S.thread = d.thread; S.turns = d.turns.map(remember); renderThread();
    } catch { /* the next reconnect tries again */ }
  }
}

// A turn arrives both in the response to the request that made or changed it
// and on the event stream, and the two race: the stream can deliver a turn's
// next state before the POST that created it has even returned. So the newest
// snapshot seen from either path wins, and events for a thread this tab has
// not adopted yet are remembered rather than dropped.
const SEEN = new Map();          // turn id -> newest snapshot, any thread
const newer = (a, b) => (!b || (a && a.at >= b.at)) ? a : b;
function remember(t) {
  const best = newer(t, SEEN.get(t.id));
  SEEN.delete(t.id); SEEN.set(t.id, best);
  if (SEEN.size > 300) SEEN.delete(SEEN.keys().next().value);
  return best;
}
function upsertTurn(t) {
  t = remember(t);
  const i = S.turns.findIndex(x => x.id === t.id);
  if (i >= 0) S.turns[i] = newer(t, S.turns[i]);
  else { S.turns.push(t); S.turns.sort((a, b) => a.seq - b.seq); }
  return i < 0;
}

function onTurn(d) {
  if (d.thread) {
    const i = S.threads.findIndex(x => x.id === d.thread.id);
    if (i >= 0) S.threads[i] = d.thread; else S.threads.unshift(d.thread);
    S.threads.sort((a, b) => b.updated_at - a.updated_at);
    renderRail();
  }
  remember(d.turn);
  if (d.turn.thread_id !== S.threadId) return;
  for (const j of d.turn.jobs) if (!LIVE.has(j.status)) releasePreview(j.id);
  if (upsertTurn(d.turn)) renderThread(); else renderTurn(d.turn.id);
  renderTools();
}

function onQueue(d) {
  S.comfyConnected = d.comfy_connected; S.depth = d.depth;
  const touched = new Set();
  for (const qj of d.jobs) {
    const t = turnOf(qj.id);
    if (!t) continue;
    const j = t.jobs.find(x => x.id === qj.id);
    Object.assign(j, { position: qj.position, eta_s: qj.eta_s, status: qj.status,
                       cold_start: qj.cold_start, started_at: qj.started_at });
    touched.add(t.id);
  }
  touched.forEach(renderTurn);
  renderStatus();
}

function onJob(d) {
  const job = d.job;
  const t = S.turns.find(x => x.id === job.turn_id);
  if (!t) return;
  const i = t.jobs.findIndex(x => x.id === job.id);
  if (i >= 0) t.jobs[i] = { ...t.jobs[i], ...job }; else t.jobs.push(job);
  if (!LIVE.has(job.status)) releasePreview(job.id);
  renderTurn(t.id);
}

function onProgress(d) {
  const t = turnOf(d.job_id);
  if (!t) return;
  const j = t.jobs.find(x => x.id === d.job_id);
  j.progress = { value: d.value, max: d.max, overall: d.overall, stage: d.stage };
  j.eta_s = d.eta_s; j.status = 'running';
  j.started_at = d.started_at || j.started_at;
  renderTurn(t.id);
}

// The frame itself comes over HTTP; the event only says a newer one exists.
// One fetch in flight per job at a time, so a slow connection drops frames
// instead of queueing them up.
const _previewBusy = {};
async function onPreview(d) {
  if (_previewBusy[d.job_id] || !turnOf(d.job_id)) return;
  _previewBusy[d.job_id] = true;
  try {
    const r = await fetch(`/api/jobs/${d.job_id}/preview?v=${d.seq}`);
    if (r.ok) {
      const url = URL.createObjectURL(await r.blob());
      if (S.previews[d.job_id]) URL.revokeObjectURL(S.previews[d.job_id]);
      S.previews[d.job_id] = url;
      const t = turnOf(d.job_id); if (t) renderTurn(t.id);
    }
  } catch { /* a dropped frame is not worth reporting */ }
  _previewBusy[d.job_id] = false;
}
function releasePreview(jobId) {
  if (!S.previews[jobId]) return;
  URL.revokeObjectURL(S.previews[jobId]);
  delete S.previews[jobId];
}

function onImage(d) {
  const img = d.image;
  if (S.lib.images.length && !S.lib.q && !S.lib.fav && !S.lib.images.some(x => x.id === img.id)) {
    S.lib.images.unshift(img);
    if (S.view === 'library') render();
  }
  const t = S.turns.find(x => x.id === img.turn_id);
  if (!t || t.images.some(x => x.id === img.id)) return;
  t.images.push({ ...img, handle: `t${t.seq}.${t.images.length + 1}` });
  renderTurn(t.id);
}

/* --------------------------------------------------------------- rendering */
function render() {
  $app.replaceChildren(
    h('div', { class: 'shell' + (S.drawer ? ' drawer-open' : '') },
      rail(),
      h('div', { class: 'scrim', onclick: () => { S.drawer = false; render(); } }),
      h('main', { class: 'main' }, mobileTop(), mainView())),
    S.lightbox ? lightbox() : null,
    h('div', { class: 'toasts', id: 'toasts' }));
}
const swap = (id, el) => { const old = document.getElementById(id); if (old) old.replaceWith(el); };
function renderRail() { swap('rail', rail()); }
function renderStatus() { swap('status', statusBadge()); }
function renderTools() {
  const el = document.getElementById('compose-tools'); if (el) el.replaceChildren(...toolKids());
  const ta = document.getElementById('prompt'); if (ta) ta.placeholder = placeholder();
}
function renderAtts() { const el = document.getElementById('atts'); if (el) el.replaceChildren(...attKids()); }
function renderThread() {
  const el = document.getElementById('thread'); if (!el) return;
  const stick = nearBottom();
  el.replaceChildren(...threadKids());
  stickToBottom(stick);
}
function renderTurn(id) {
  const t = S.turns.find(x => x.id === id);
  const el = document.getElementById('turn-' + id);
  if (!t || !el) { renderThread(); return; }
  const stick = nearBottom();
  el.replaceWith(turnEl(t));
  stickToBottom(stick);
}
function nearBottom() {
  const s = document.getElementById('scroll');
  return !s || s.scrollHeight - s.scrollTop - s.clientHeight < 160;
}
function stickToBottom(yes) {
  const s = document.getElementById('scroll');
  if (s && yes) requestAnimationFrame(() => { s.scrollTop = s.scrollHeight; });
}

/* -------------------------------------------------------------------- rail */
function rail() {
  return h('nav', { class: 'rail', id: 'rail', 'aria-label': 'Conversations' },
    h('div', { class: 'rail-top' },
      h('a', { class: 'brand', href: '#/', onclick: e => { e.preventDefault(); newChat(); } },
        h('img', { src: '/assets/alchemyui-favicon.svg', alt: '' }),
        h('span', { class: 'wordmark' }, 'AlchemyUI')),
      h('div', { class: 'spacer' }),
      h('button', { class: 'btn btn-ghost btn-icon rail-close', title: 'Close',
                    onclick: () => { S.drawer = false; render(); } }, icon('x', 'icon-sm'))),
    h('button', { class: 'btn rail-new', onclick: newChat }, icon('plus', 'icon-sm'), 'New chat'),
    h('a', { class: 'rail-link' + (S.view === 'library' ? ' is-active' : ''), href: '#/library' },
      icon('image', 'icon-sm'), 'Library'),
    h('div', { class: 'rail-label' }, 'Conversations'),
    h('div', { class: 'threads' },
      S.threads.length ? S.threads.map(threadLink)
        : h('p', { class: 'rail-empty' }, 'Your conversations will appear here.')),
    h('div', { class: 'rail-foot' }, statusBadge(), account()));
}

function threadLink(t) {
  const active = S.view === 'chat' && S.threadId === t.id;
  return h('div', { class: 'thread-link' + (active ? ' is-active' : '') },
    h('a', { href: '#/t/' + encodeURIComponent(t.id), title: t.title }, t.title || 'Untitled'),
    h('span', { class: 'when' }, ago(t.updated_at)),
    h('button', { class: 'del', title: 'Delete conversation', 'aria-label': 'Delete conversation',
                  onclick: () => deleteThread(t) }, icon('trash', 'icon-sm')));
}

function statusBadge() {
  let cls = 'is-on', label = 'Ready';
  if (!S.connected) { cls = 'is-off'; label = 'Reconnecting…'; }
  else if (!S.comfyConnected) { cls = 'is-off'; label = 'Generator offline'; }
  else if (S.depth) { cls = 'is-busy'; label = S.depth === 1 ? '1 job running' : `${S.depth} jobs in the queue`; }
  return h('span', { class: 'badge ' + cls, id: 'status', title: 'Queue status, across everyone' },
    h('i', { class: 'dot' }), label);
}

function account() {
  const name = S.me?.display_name || S.me?.email || '';
  return h('div', { class: 'account' },
    h('button', { class: 'account-btn', 'aria-expanded': S.menu ? 'true' : 'false',
                  onclick: () => { S.menu = !S.menu; renderRail(); } },
      h('span', { class: 'avatar' }, (name[0] || '?').toUpperCase()),
      h('span', { class: 'account-name' }, name), icon('chevron', 'icon-sm')),
    S.menu ? h('div', { class: 'menu', role: 'menu' },
      h('a', { href: '#/settings', role: 'menuitem' }, icon('settings', 'icon-sm'), 'Settings'),
      S.me?.is_admin ? h('a', { href: '/admin', role: 'menuitem' }, icon('shield', 'icon-sm'), 'Admin') : null,
      h('a', { href: '/oauth2/sign_out', role: 'menuitem' }, icon('logout', 'icon-sm'), 'Sign out')) : null);
}

function mobileTop() {
  const title = S.view === 'library' ? 'Library' : S.view === 'settings' ? 'Settings'
    : (S.thread?.title || 'New chat');
  return h('header', { class: 'mtop' },
    h('button', { class: 'btn btn-ghost btn-icon', title: 'Conversations',
                  onclick: () => { S.drawer = true; render(); } }, icon('menu', 'icon-sm')),
    h('span', { class: 'mtop-title' }, title),
    h('button', { class: 'btn btn-ghost btn-icon', title: 'New chat', onclick: newChat },
      icon('plus', 'icon-sm')));
}

function mainView() {
  if (S.view === 'library') return libraryView();
  if (S.view === 'settings') return settingsView();
  return h('div', { class: 'chat' },
    h('div', { class: 'chat-scroll', id: 'scroll' },
      h('div', { class: 'wrap thread', id: 'thread' }, threadKids())),
    composer());
}

/* ------------------------------------------------------------------ thread */
const EXAMPLES = [
  'A spooky lighthouse at night',
  'Snowy forest during a snowstorm',
  'Me as an astronaut',
];

function threadKids() {
  if (S.threadError) return [h('div', { class: 'empty-note' }, S.threadError)];
  if (S.threadId && !S.thread) return [h('div', { class: 'loading' }, h('i', { class: 'pulse' }), 'Loading…')];
  if (!S.turns.length) {
    return [h('div', { class: 'welcome' },
      h('h1', {}, 'What should we make?'),
      h('p', { class: 'muted' }, 'Describe an image, or attach a photo and say what to change.'),
      h('div', { class: 'examples' }, EXAMPLES.map(x => h('button', {
        class: 'btn', onclick: () => { S.draft.text = x; const ta = document.getElementById('prompt');
          if (ta) { ta.value = x; ta.focus(); } renderTools(); },
      }, x))))];
  }
  return S.turns.map(turnEl);
}

function turnEl(t) {
  return h('article', { class: 'turn', id: 'turn-' + t.id }, userMsg(t), botMsg(t));
}

function userMsg(t) {
  return h('div', { class: 'msg-user' },
    t.attachments.length ? h('div', { class: 'att-row' }, t.attachments.map((a, i) =>
      h('a', { class: 'att', href: a.url, target: '_blank', rel: 'noopener', title: `Attachment ${i + 1}` },
        h('img', { src: a.url, alt: `Attachment ${i + 1}`, loading: 'lazy' }),
        // numbered to match the a1, a2 the planner is told about
        h('span', { class: 'att-n' }, String(i + 1))))) : null,
    t.text ? h('div', { class: 'bubble' }, t.text) : null);
}

function botMsg(t) {
  const live = t.jobs.filter(j => LIVE.has(j.status));
  const failed = t.state === 'failed' ? t.jobs.find(j => j.error_detail) : null;
  return h('div', { class: 'msg-bot' },
    chip(t),
    S.open.has(t.id) ? planDetails(t) : null,
    t.answers.map(a => h('div', { class: 'qa' }, icon('corner', 'icon-sm'),
      h('span', { class: 'muted' }, a.question), h('b', {}, a.answer))),
    t.state === 'needs_input' ? question(t)
      : t.reply ? h('div', { class: 'reply' + (t.state === 'failed' ? ' is-err' : '') }, t.reply)
      : t.state === 'cancelled' ? h('div', { class: 'reply muted' }, 'Stopped.') : null,
    failed ? h('details', { class: 'tech' }, h('summary', {}, 'Technical details'),
      h('pre', {}, failed.error_detail)) : null,
    live.length ? h('div', { class: 'grid grid-turn' }, live.map(jobCard)) : null,
    t.images.length ? h('div', { class: 'grid grid-turn' },
      t.images.map((img, i) => imageCard(img, t.images.length > 1 ? String(i + 1) : null))) : null);
}

function chip(t) {
  let label = t.chip;
  const busy = t.state === 'planning';
  if (busy) {
    const s = Math.max(0, Date.now() / 1000 - (t.planned_at && t.answers.length ? t.planned_at : t.created_at));
    label = (t.answers.length ? 'Thinking about your answer' : 'Working out what you want')
      + (s >= 3 ? ` · ${Math.round(s)}s` : '…');
  } else if (!label) {
    label = t.state === 'failed' ? 'Couldn\'t do that' : t.state === 'cancelled' ? 'Stopped' : 'Needs your answer';
  }
  const canOpen = !!(t.plan && t.compiled);
  const open = S.open.has(t.id);
  return h('button', {
    class: 'chip' + (busy ? ' is-busy' : '') + (t.state === 'failed' ? ' is-err' : '') + (canOpen ? '' : ' is-static'),
    type: 'button', 'aria-expanded': canOpen ? String(open) : null,
    title: canOpen ? 'What I understood, and how it will be made' : null,
    onclick: canOpen ? () => { open ? S.open.delete(t.id) : S.open.add(t.id); renderTurn(t.id); } : null,
  }, busy ? h('i', { class: 'pulse' }) : icon('sparkle', 'icon-sm'), h('span', {}, label),
  canOpen ? icon('chevron', 'icon-sm chev' + (open ? ' is-open' : '')) : null);
}

const SOURCE = {
  primary: 'Llamacracy',
  fallback: 'the backup planner on this machine',
  default: 'no planner: your words were used as the prompt',
};

function planDetails(t) {
  const p = t.plan || {}, c = t.compiled || {};
  const seeds = [...new Set(t.jobs.map(j => j.params?.seed).filter(s => s != null))];
  const row = (label, val, mono) => val == null || val === '' ? null
    : h('div', { class: 'kv' }, h('span', { class: 'k' }, label), h('span', { class: 'v' + (mono ? ' mono' : '') }, String(val)));
  return h('div', { class: 'plan' },
    row('Prompt', p.prompt, true),
    row('Avoiding', p.negative, true),
    row('Model', t.model || shortCkpt(c.checkpoint)),
    row(seeds.length > 1 ? 'Seeds' : 'Seed', seeds.join(', '), true),
    row('Planned by', (SOURCE[t.plan_source] || t.plan_source || '')
      + (t.router_ms ? ` in ${fmtDur(t.router_ms / 1000)}` : '')),
    c.workflow && !BUSY_TURN.has(t.state) ? h('div', { class: 'row' },
      h('button', { class: 'btn btn-sm', onclick: () => retry(t) }, icon('redo', 'icon-sm'), 'Run again')) : null);
}

function question(t) {
  const q = t.question || { question: t.reply, options: [] };
  return h('div', { class: 'question' },
    h('div', { class: 'q' }, q.question),
    q.options.length ? h('div', { class: 'q-opts' }, q.options.map((o, i) =>
      h('button', { class: 'btn btn-sm', onclick: () => answer(t, { option: i }) }, o))) : null,
    h('div', { class: 'small faint' }, q.options.length ? 'Or type an answer below.' : 'Type your answer below.'));
}

// A job in flight -- never a bare spinner. The card says exactly where it
// is: queue position, then steps, then elapsed and a learned ETA.
function jobCard(job) {
  const p = job.progress || { value: 0, max: 0 };
  const stepping = job.status === 'running' && p.max > 0;
  const pct = stepping ? Math.round(100 * (p.overall > 0 ? p.overall : p.value / p.max)) : 0;
  let state;
  if (job.status === 'queued') {
    state = job.position > 0 ? `Queued · ${job.position} ahead of you`
      : job.position === 0 ? 'Starting…' : 'Queued';
  } else if (stepping) {
    state = p.stage ? `${p.stage} · ${p.value}/${p.max}` : `Step ${p.value} of ${p.max}`;
  } else if (job.cold_start) {
    state = 'Loading the model…';
  } else {
    state = 'Starting…';
  }
  const elapsed = job.started_at ? Date.now() / 1000 - job.started_at : null;
  const preview = S.previews[job.id];
  return h('div', { class: 'card card-job' + (preview ? ' has-preview' : ''), id: 'job-' + job.id },
    preview ? h('img', { class: 'job-preview', src: preview, alt: 'Preview of the image being made' }) : null,
    h('div', { class: 'state' }, h('i', { class: 'pulse' }), h('span', {}, state)),
    h('div', { class: 'bar' + (stepping ? '' : ' indet') }, h('i', { style: stepping ? `width:${pct}%` : '' })),
    h('div', { class: 'meta' },
      elapsed != null ? h('span', { title: 'Elapsed' }, fmtDur(elapsed)) : null,
      job.eta_s != null ? h('span', { class: 'faint', title: 'Estimated time remaining, learned from earlier runs' },
        `~${fmtDur(job.eta_s)} left`) : null));
}

function imageCard(img, n) {
  return h('div', { class: 'card img-card' },
    h('img', {
      src: img.url, alt: img.params?.prompt || 'Generated image', loading: 'lazy',
      width: img.width || undefined, height: img.height || undefined,
      onclick: () => { S.lightbox = img; render(); },
    }),
    n ? h('span', { class: 'img-n', title: `Image ${n}` }, n) : null,
    h('div', { class: 'card-foot' },
      favButton(img),
      h('button', { class: 'btn btn-ghost btn-sm', title: 'Attach to your next message',
                    onclick: () => useInNext(img) }, icon('clip', 'icon-sm'), 'Use'),
      h('div', { class: 'spacer' }),
      h('a', { class: 'btn btn-ghost btn-sm', href: img.url, download: `alchemyui-${img.seed ?? img.id}.png`,
               title: 'Download' }, icon('down', 'icon-sm'))));
}

function favButton(img) {
  return h('button', {
    class: 'fav' + (img.favorite ? ' is-on' : ''), type: 'button',
    title: img.favorite ? 'Remove from favourites' : 'Add to favourites',
    'aria-pressed': img.favorite ? 'true' : 'false',
    onclick: e => { e.stopPropagation(); toggleFavorite(img); },
  }, icon('star', 'icon-sm'));
}

/* ---------------------------------------------------------------- composer */
function placeholder() {
  if (pendingQuestion()) return 'Type your answer…';
  return S.turns.length ? 'Ask for a change, or something new…' : 'Describe an image, or attach one to change…';
}

function composer() {
  return h('form', {
    class: 'composer wrap', id: 'composer',
    onsubmit: e => { e.preventDefault(); send(); },
    ondragover: e => { e.preventDefault(); e.currentTarget.classList.add('is-over'); },
    ondragleave: e => e.currentTarget.classList.remove('is-over'),
    ondrop: e => { e.preventDefault(); e.currentTarget.classList.remove('is-over');
      attachFiles([...(e.dataTransfer?.files || [])]); },
  },
  h('div', { class: 'compose-card' },
    h('div', { class: 'draft-atts', id: 'atts' }, attKids()),
    h('div', { class: 'compose-row' },
      h('button', { class: 'btn btn-ghost btn-icon', type: 'button', title: 'Attach images (up to 4)',
                    onclick: () => document.getElementById('file')?.click() }, icon('clip')),
      h('textarea', {
        id: 'prompt', rows: 1, placeholder: placeholder(), value: S.draft.text,
        'aria-label': 'Message',
        oninput: e => { S.draft.text = e.target.value; autosize(e.target); renderTools(); },
        onkeydown: e => {
          // Enter sends; Shift+Enter is a new line. Never while an IME is composing.
          if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
        },
        onpaste: e => {
          const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
          if (files.length) { e.preventDefault(); attachFiles(files); }
        },
      }),
      h('div', { class: 'compose-tools', id: 'compose-tools' }, toolKids()))),
  h('input', { type: 'file', id: 'file', accept: 'image/*', multiple: true, hidden: true,
               onchange: e => { attachFiles([...e.target.files]); e.target.value = ''; } }));
}

function attKids() {
  return S.draft.atts.map((a, i) => h('div', { class: 'draft-att' + (a.busy ? ' is-busy' : '') },
    h('img', { src: a.url, alt: `Attachment ${i + 1}` }),
    h('span', { class: 'att-n' }, String(i + 1)),
    h('button', { type: 'button', class: 'del', title: 'Remove', 'aria-label': `Remove attachment ${i + 1}`,
                  onclick: () => { S.draft.atts.splice(i, 1); renderAtts(); renderTools(); } }, icon('x', 'icon-sm'))));
}

function toolKids() {
  const running = busyTurn();
  if (running && !S.draft.text.trim() && !S.draft.atts.length) {
    return [h('button', { class: 'btn btn-icon btn-stop', type: 'button', title: 'Stop',
                          onclick: () => cancelTurn(running) }, icon('stop'))];
  }
  const empty = !S.draft.text.trim() && !S.draft.atts.length;
  const uploading = S.draft.atts.some(a => a.busy);
  return [h('button', { class: 'btn btn-primary btn-icon', type: 'submit', title: 'Send',
                        disabled: empty || uploading || S.sending }, icon('send'))];
}

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + 'px';
}
// Only on a real pointer: focusing on a phone throws the keyboard up over
// the very thing the person was about to look at.
function hasFinePointer() { return matchMedia('(pointer: fine)').matches; }
function focusComposer() {
  if (!hasFinePointer()) return;
  const ta = document.getElementById('prompt'); if (ta) ta.focus();
}

/* ----------------------------------------------------------------- actions */
async function send() {
  const text = S.draft.text.trim();
  if (S.sending || (!text && !S.draft.atts.length)) return;
  if (S.draft.atts.some(a => a.busy)) { toast('Still uploading…'); return; }
  const q = pendingQuestion();
  S.sending = true; renderTools();
  try {
    if (q && !S.draft.atts.length) {
      // Typing while the planner is waiting on you answers it.
      upsertTurn(await api.post(`/api/turns/${q.id}/answer`, { text }));
      renderTurn(q.id);
    } else {
      const t = await api.post('/api/turns', {
        thread_id: S.threadId, text, attachments: S.draft.atts.map(a => a.id),
      });
      if (!S.threadId) {
        S.threadId = t.thread_id;
        S.thread = { id: t.thread_id, title: text.slice(0, 60) || 'Images' };
        location.hash = '#/t/' + encodeURIComponent(t.thread_id);
      }
      upsertTurn(t);
      renderThread();
    }
    S.draft = { text: '', atts: [] };
    const ta = document.getElementById('prompt'); if (ta) { ta.value = ''; autosize(ta); }
    renderAtts();
    stickToBottom(true);
  } catch (e) { toast(e.message, true); }
  S.sending = false; renderTools();
}

async function attachFiles(files) {
  for (const f of files) {
    if (!f.type || !f.type.startsWith('image/')) continue;
    if (S.draft.atts.length >= 4) { toast('Up to 4 images per message.'); break; }
    const a = { id: null, url: URL.createObjectURL(f), busy: true };
    S.draft.atts.push(a); renderAtts(); renderTools();
    try {
      a.id = (await api.upload('/api/uploads', f)).id; a.busy = false;
    } catch (e) {
      S.draft.atts = S.draft.atts.filter(x => x !== a); toast(e.message, true);
    }
    renderAtts(); renderTools();
  }
}

async function useInNext(img) {
  if (S.draft.atts.length >= 4) { toast('Up to 4 images per message.'); return; }
  try {
    const up = await api.post(`/api/images/${img.id}/reuse`);
    S.draft.atts.push({ id: up.id, url: up.url, busy: false });
    S.lightbox = null;
    if (S.view !== 'chat') location.hash = S.threadId ? '#/t/' + encodeURIComponent(S.threadId) : '#/';
    render(); focusComposer();
  } catch (e) { toast(e.message, true); }
}

async function answer(t, body) {
  try { upsertTurn(await api.post(`/api/turns/${t.id}/answer`, body)); renderTurn(t.id); renderTools(); }
  catch (e) { toast(e.message, true); }
}
async function cancelTurn(t) {
  try { upsertTurn(await api.post(`/api/turns/${t.id}/cancel`)); renderTurn(t.id); renderTools(); }
  catch (e) { toast(e.message, true); }
}
async function retry(t) {
  try { upsertTurn(await api.post(`/api/turns/${t.id}/retry`)); renderTurn(t.id); renderTools(); }
  catch (e) { toast(e.message, true); }
}
async function deleteThread(t) {
  if (!confirm(`Delete "${t.title}"? Its images stay in your library.`)) return;
  try {
    await api.del('/api/threads/' + encodeURIComponent(t.id));
    S.threads = S.threads.filter(x => x.id !== t.id);
    if (S.threadId === t.id) newChat(); else renderRail();
  } catch (e) { toast(e.message, true); }
}
async function toggleFavorite(img) {
  try {
    const updated = await api.patch(`/api/images/${img.id}`, { favorite: !img.favorite });
    for (const list of [S.lib.images, ...S.turns.map(t => t.images)]) {
      for (const x of list) if (x.id === img.id) x.favorite = updated.favorite;
    }
    img.favorite = updated.favorite;
    if (S.lib.fav && !img.favorite) S.lib.images = S.lib.images.filter(i => i.id !== img.id);
    if (S.view === 'chat' && !S.lightbox) { const t = S.turns.find(x => x.id === img.turn_id); if (t) renderTurn(t.id); }
    else render();
  } catch (e) { toast(e.message, true); }
}
async function delImage(img) {
  if (!confirm('Delete this image for good?')) return;
  try {
    await api.del('/api/images/' + img.id);
    S.lib.images = S.lib.images.filter(i => i.id !== img.id);
    for (const t of S.turns) t.images = t.images.filter(i => i.id !== img.id);
    S.lightbox = null; render();
  } catch (e) { toast(e.message, true); }
}

/* ----------------------------------------------------------------- library */
let _libTimer = null;
async function loadLibrary(reset) {
  const L = S.lib;
  if (L.loading) return;
  if (reset) { L.images = []; L.done = false; }
  L.loading = true; render();
  const q = new URLSearchParams({ limit: '60' });
  if (L.q.trim()) q.set('q', L.q.trim());
  if (L.fav) q.set('favorite', 'true');
  if (!reset && L.images.length) q.set('before', String(L.images[L.images.length - 1].created_at));
  try {
    const got = (await api.get('/api/images?' + q)).images;
    L.images = reset ? got : L.images.concat(got);
    L.done = got.length < 60;
  } catch (e) { toast(e.message, true); }
  L.loading = false; render();
}

function libraryView() {
  const L = S.lib;
  return h('div', { class: 'page' }, h('div', { class: 'wrap wide' },
    h('div', { class: 'page-head' },
      h('h1', {}, 'Library'),
      h('div', { class: 'spacer' }),
      h('label', { class: 'search' }, icon('search', 'icon-sm'),
        h('input', { class: 'input', type: 'search', placeholder: 'Search prompts', value: L.q,
                     'aria-label': 'Search prompts',
                     oninput: e => { L.q = e.target.value; clearTimeout(_libTimer);
                       _libTimer = setTimeout(() => loadLibrary(true), 250); } })),
      h('button', { class: 'btn btn-sm' + (L.fav ? ' is-on' : ''), 'aria-pressed': String(L.fav),
                    onclick: () => { L.fav = !L.fav; loadLibrary(true); } }, icon('star', 'icon-sm'), 'Favourites')),
    L.images.length ? h('div', { class: 'grid' }, L.images.map(img => imageCard(img, null)))
      : h('div', { class: 'empty-note' }, L.loading ? 'Loading…'
        : L.q || L.fav ? 'Nothing matches.' : 'Nothing here yet. Your images will collect here.'),
    !L.done && L.images.length ? h('div', { class: 'more' },
      h('button', { class: 'btn', disabled: L.loading, onclick: () => loadLibrary(false) },
        L.loading ? 'Loading…' : 'Load more')) : null));
}

/* ---------------------------------------------------------------- settings */
async function loadSettings() {
  try {
    const [refs, usage] = await Promise.all([api.get('/api/references'), api.get('/api/me/usage')]);
    S.refs = refs.references; S.usage = usage;
  } catch (e) { toast(e.message, true); }
  render();
}

async function addLikeness(files) {
  S.refsBusy = true; render();
  for (const f of files) {
    if (S.refs.length >= 4) { toast('You can save up to 4 photos.'); break; }
    try { S.refs.unshift(await api.upload('/api/references', f)); }
    catch (e) { toast(e.message, true); }
  }
  S.refsBusy = false; render();
}
async function removeLikeness(r) {
  try { await api.del('/api/references/' + r.id); S.refs = S.refs.filter(x => x.id !== r.id); render(); }
  catch (e) { toast(e.message, true); }
}

function settingsView() {
  const u = S.usage;
  return h('div', { class: 'page' }, h('div', { class: 'wrap' },
    h('div', { class: 'page-head' }, h('h1', {}, 'Settings')),
    h('section', { class: 'setting' },
      h('h2', {}, icon('face', 'icon-sm'), 'Your likeness'),
      h('p', { class: 'muted' }, 'One to four clear photos of your face. When you ask for "me as an astronaut", ',
        'these are what "me" means. Nobody else\'s messages can use them.'),
      h('div', { class: 'ref-list' },
        S.refs.map(r => h('div', { class: 'ref' },
          h('img', { src: r.url, alt: r.label || 'Likeness photo' }),
          h('button', { class: 'del', title: 'Remove', 'aria-label': 'Remove this photo',
                        onclick: () => removeLikeness(r) }, icon('x', 'icon-sm')))),
        S.refs.length < 4 ? h('button', { class: 'ref-add', disabled: S.refsBusy,
                                          onclick: () => document.getElementById('face-file')?.click() },
          S.refsBusy ? h('i', { class: 'pulse' }) : icon('plus'), h('span', { class: 'small' }, S.refsBusy ? 'Checking…' : 'Add')) : null),
      h('input', { type: 'file', id: 'face-file', accept: 'image/*', multiple: true, hidden: true,
                   onchange: e => { addLikeness([...e.target.files]); e.target.value = ''; } })),
    h('section', { class: 'setting' },
      h('h2', {}, 'This month'),
      u ? h('div', { class: 'stats' },
        h('div', {}, h('b', {}, String(u.images)), h('span', {}, plural(u.images, 'image').replace(/^\d+ /, ''))),
        h('div', {}, h('b', {}, fmtKwh(u.kwh)), h('span', {}, 'kWh')),
        h('div', {}, h('b', {}, fmtMoney(u.cost_usd)), h('span', {}, 'electricity'))) : h('p', { class: 'muted' }, 'Loading…'),
      h('p', { class: 'small faint' }, 'An estimate of the GPU\'s share of the electricity, not a wall-socket reading. ',
        'Shown so you can see it; nothing is charged or limited.'))));
}

/* --------------------------------------------------------------- lightbox */
function lightbox() {
  const img = S.lightbox;
  const p = img.params || {};
  const close = () => { S.lightbox = null; render(); };
  const meta = (label, val) => val == null || val === '' ? null
    : h('div', {}, h('b', {}, label + ' '), h('span', { class: 'p' }, String(val)));
  return h('div', {
    class: 'lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Image',
    onclick: e => { if (e.target === e.currentTarget) close(); },
  },
    h('div', { class: 'lightbox-bar' },
      // <a download> works over plain HTTP; the Clipboard API would not, which
      // is why there is no "copy image" button here.
      h('a', { class: 'btn btn-primary', href: img.url, download: `alchemyui-${img.seed ?? img.id}.png` },
        icon('down', 'icon-sm'), 'Download'),
      h('button', { class: 'btn', onclick: () => useInNext(img) }, icon('clip', 'icon-sm'), 'Use in next message'),
      h('button', { class: 'btn' + (img.favorite ? ' is-on' : ''), onclick: () => toggleFavorite(img) },
        icon('star', 'icon-sm'), img.favorite ? 'Favourited' : 'Favourite'),
      h('button', { class: 'btn btn-ghost btn-danger', onclick: () => delImage(img) }, icon('trash', 'icon-sm'), 'Delete'),
      h('button', { class: 'btn btn-ghost', onclick: close }, icon('x', 'icon-sm'), 'Close')),
    h('img', { src: img.url, alt: p.prompt || 'Generated image' }),
    h('div', { class: 'lightbox-meta' },
      meta('Prompt', p.prompt), meta('Seed', p.seed),
      meta('Size', img.width && img.height ? `${img.width} × ${img.height}` : null),
      meta('Model', shortCkpt(p.checkpoint))));
}

function toast(message, isError = false) {
  const box = document.getElementById('toasts'); if (!box) return;
  const el = h('div', { class: 'toast' + (isError ? ' err' : '') }, message);
  box.append(el);
  setTimeout(() => el.remove(), 6000);
}

/* -------------------------------------------------------------------- boot */
async function boot() {
  try { S.me = await api.get('/api/me'); }
  catch (e) {
    $app.replaceChildren(h('div', { class: 'wrap', style: 'padding-top:12vh' },
      h('div', { class: 'card', style: 'padding:20px' },
        h('h3', { style: 'color:var(--danger);margin:0 0 8px' }, 'Cannot start'),
        h('p', { class: 'muted', style: 'margin:0' }, e.message))));
    return;
  }
  render();
  await loadThreads();
  await route();
  window.addEventListener('hashchange', route);
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (S.lightbox) { S.lightbox = null; render(); }
    else if (S.drawer || S.menu) { S.drawer = false; S.menu = false; render(); }
  });
  connectEvents();
  S.booted = true;
  // Clocks tick on their own: nothing else pushes an event between sampler
  // steps, or while the planner thinks.
  setInterval(() => {
    for (const t of S.turns) {
      if (t.state === 'planning' || t.jobs.some(j => j.status === 'running')) renderTurn(t.id);
    }
  }, 1000);
}

boot();
