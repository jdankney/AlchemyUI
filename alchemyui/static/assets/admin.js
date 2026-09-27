// AlchemyUI admin -- vanilla JS, no build step. A separate page rather than a
// view inside the SPA, so it survives any rework of the chat UI untouched.
//
// Metadata only. The API never returns anyone's prompts or images, and this
// page never asks.
const $app = document.getElementById('app');

const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
};
async function get(p) { const r = await fetch(p); if (!r.ok) throw new Error(`${r.status} ${p}`); return r.json(); }
async function post(p, b) {
  const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const S = { live: null, users: [], failures: [], fallbacks: [], days: 30, error: null };
// The API returns exact figures; rounding happens here, and a tiny non-zero
// amount says so rather than printing as zero.
const fmtMoney = n => (n > 0 && n < 0.0001) ? '<$0.0001' : '$' + (n || 0).toFixed(n >= 1 ? 2 : 4);
const fmtKwh = n => (n > 0 && n < 0.001) ? '<0.001' : (n || 0).toFixed(3);
const fmtAgo = ts => {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago`
    : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function tile(label, value, sub) {
  return h('div', { class: 'adm-tile' }, h('div', { class: 'small faint' }, label),
    h('div', { class: 'adm-big' }, value), sub ? h('div', { class: 'small muted' }, sub) : null);
}

// The router's two backends, in words. `ok` is null until a backend is first
// used, so a quiet morning does not read as an outage.
function backend(b) {
  if (!b) return 'off';
  if (b.ok === null) return 'not used yet';
  return b.ok ? 'ok' : (b.error || 'down');
}
function routerTile(R) {
  if (!R) return null;
  const r = R.router || {};
  const p = r.primary;
  const main = p ? (p.ok === false ? 'unreachable' : 'Llamacracy') : (r.fallback ? 'backup only' : 'off');
  return tile('Planner', main,
    `${p ? `${p.model}: ${backend(p)} · ` : ''}backup: ${backend(r.fallback)}` +
    (R.planning ? ` · ${R.planning} planning` : ''));
}

function liveSection() {
  const L = S.live;
  if (!L) return h('p', { class: 'muted' }, 'Loading…');
  const g = L.gpu || {};
  const running = L.queue.jobs.find(j => j.position === 0);
  // The one reading that explains "why is everything slow?" from a distance.
  const capped = g.available && g.capped
    ? h('div', { class: 'card', style: 'padding:10px 14px;margin-bottom:10px;color:var(--warn)' },
      `GPU power-capped at ${g.power_limit_w} W of ${g.default_limit_w} W`,
      L.gpu.on_mains === false ? ' — the machine is on battery.' : '.',
      ' Generations will run several times slower until it is lifted.')
    : null;
  return h('section', {}, capped,
    h('div', { class: 'adm-tiles' },
      tile('GPU draw', g.available ? `${g.power_w.toFixed(0)} W` : 'n/a', g.available ? `${g.temp_c.toFixed(0)} °C` : null),
      tile('Utilisation', g.available ? `${g.util_pct.toFixed(0)}%` : 'n/a'),
      tile('VRAM', g.available ? `${(g.vram_used_mib / 1024).toFixed(1)} / ${(g.vram_total_mib / 1024).toFixed(0)} GB` : 'n/a'),
      tile('Queue', String(L.queue.depth), running ? `running: ${running.owner}` : 'idle'),
      tile('ComfyUI', L.comfy_connected ? 'connected' : 'down', (L.resident_checkpoint || '').split('/').pop() || 'nothing loaded'),
      routerTile(L.router)),
    L.queue.jobs.length ? h('table', { class: 'adm-table' },
      h('tr', {}, ['#', 'Owner', 'Workflow', 'Model', 'Status', 'Progress'].map(c => h('th', {}, c))),
      L.queue.jobs.map(j => h('tr', {},
        h('td', {}, String(j.position)), h('td', {}, j.owner), h('td', {}, j.workflow_id),
        h('td', {}, (j.checkpoint || '').split('/').pop()), h('td', {}, j.status),
        h('td', {}, j.position === 0 ? `${Math.round(j.overall * 100)}%` : (j.eta_s ? `~${Math.round(j.eta_s)}s` : ''))))) : null);
}

function usersSection() {
  const pick = d => h('button', { class: 'btn btn-sm' + (S.days === d ? ' btn-primary' : ''), onclick: () => { S.days = d; loadUsers(); } },
    d ? `${d} days` : 'All time');
  return h('section', {},
    h('div', { class: 'adm-head' }, h('h2', {}, 'Usage'), h('div', { class: 'row' }, pick(7), pick(30), pick(0))),
    h('table', { class: 'adm-table' },
      h('tr', {}, ['User', 'Jobs', 'GPU min', 'kWh', 'Cost', 'Last seen', ''].map(c => h('th', {}, c))),
      S.users.map(u => h('tr', { class: u.disabled ? 'faint' : '' },
        h('td', {}, u.display_name, u.is_admin ? h('span', { class: 'small muted' }, ' · admin') : null),
        h('td', {}, String(u.jobs)), h('td', {}, u.gpu_minutes.toFixed(1)),
        h('td', {}, fmtKwh(u.kwh)), h('td', {}, fmtMoney(u.cost_usd)),
        h('td', { class: 'small muted' }, fmtAgo(u.last_active_at)),
        h('td', {}, u.is_admin ? null : h('button', { class: 'btn btn-sm', onclick: () => toggle(u) },
          u.disabled ? 'Enable' : 'Disable'))))),
    h('p', { class: 'small faint' },
      'Energy is the GPU\'s measured draw plus an estimate for the rest of the laptop, over the time each job held the GPU. An estimate of the GPU\'s share, not a wall-socket reading.'));
}

function failuresSection() {
  return h('section', {}, h('h2', {}, 'Recent failures'),
    S.failures.length ? h('table', { class: 'adm-table' },
      S.failures.map(f => h('tr', {}, h('td', { class: 'small muted' }, fmtAgo(f.finished_at)),
        h('td', {}, f.owner), h('td', {}, f.workflow_id), h('td', { class: 'small' }, f.error_text || ''))))
      : h('p', { class: 'muted' }, 'None.'));
}

function fallbacksSection() {
  return h('section', {}, h('h2', {}, 'Planned without Llamacracy'),
    S.fallbacks.length ? h('table', { class: 'adm-table' },
      S.fallbacks.map(f => h('tr', {}, h('td', { class: 'small muted' }, fmtAgo(f.created_at)),
        h('td', {}, f.owner || ''),
        h('td', {}, f.plan_source === 'default' ? 'no planner (words used as-is)' : 'backup planner'),
        h('td', { class: 'small' }, f.plan_error || ''))))
      : h('p', { class: 'muted' }, 'None.'));
}

function render() {
  $app.replaceChildren(h('div', { class: 'wrap adm' },
    h('header', { class: 'adm-head' }, h('h1', {}, 'AlchemyUI admin'), h('a', { href: '/' }, 'Back to the app')),
    S.error ? h('div', { class: 'card', style: 'padding:12px;color:var(--danger)' }, S.error) : null,
    liveSection(), usersSection(), failuresSection(), fallbacksSection()));
}

async function toggle(u) {
  try { await post(`/api/admin/users/${u.id}/disabled`, { disabled: !u.disabled }); await loadUsers(); }
  catch (e) { S.error = e.message; render(); }
}
async function loadUsers() {
  try { S.users = (await get(`/api/admin/users?days=${S.days}`)).users; S.error = null; }
  catch (e) { S.error = e.message; }
  render();
}
async function loadFailures() {
  try {
    S.failures = (await get('/api/admin/failures')).failures;
    S.fallbacks = (await get('/api/admin/fallbacks')).fallbacks;
  }
  catch (e) { S.error = e.message; }
  render();
}
async function poll() {
  // Only while someone is looking: this is also what keeps nvidia-smi quiet.
  if (document.visibilityState === 'visible') {
    try { S.live = await get('/api/admin/live'); S.error = null; }
    catch (e) { S.error = e.message; }
    render();
  }
  setTimeout(poll, 2000);
}

async function boot() {
  render();
  // Live first: it is what the page is for, and it starts the poll loop.
  await poll();
  await Promise.all([loadUsers(), loadFailures()]);
}
boot();
