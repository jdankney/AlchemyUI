// The Photoreal Lab -- vanilla JS, no build step. Admin only (the server
// refuses anyone else). A separate page from the chat, like /admin.
//
// Make a grid of settings, let it render in the gaps between people's
// messages, compare cells that share a seed, star the keepers, and make the
// winning combination a model's default -- for everyone, until it is reset.
const $app = document.getElementById('app');

/* ---------------------------------------------------------------- helpers */
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'value') e.value = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
};
const ICONS = {
  star: 'M12 3l2.9 6 6.6.9-4.8 4.6 1.2 6.5-5.9-3.1-5.9 3.1 1.2-6.5L2.5 9.9 9.1 9 12 3z',
  flask: 'M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7.5 15h9',
  x: 'M18 6L6 18M6 6l12 12',
  left: 'M15 18l-6-6 6-6',
  right: 'M9 18l6-6-6-6',
  check: 'M20 6L9 17l-5-5',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
};
const icon = (name, cls = '') => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  s.setAttribute('class', 'icon ' + cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name]); s.append(p);
  return s;
};
async function call(method, p, body) {
  const r = await fetch(p, { method, headers: body ? { 'content-type': 'application/json' } : {},
                             body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let d; try { d = (await r.json()).detail; } catch { d = r.statusText; }
    throw new Error(typeof d === 'string' ? d : JSON.stringify(d));
  }
  return r.json();
}
const get = p => call('GET', p);
const post = (p, b) => call('POST', p, b || {});
const fmtMin = s => s < 90 ? `about ${Math.max(1, Math.round(s))} s` : s < 5400 ? `about ${Math.round(s / 60)} min` : `about ${(s / 3600).toFixed(1)} h`;
const glow = pal => pal && pal.length ? `--c1:${pal[0]};--c2:${pal[1] || pal[0]};--c3:${pal[2] || pal[0]}` : '';
const AXES = { cfg: 'CFG', steps: 'Steps', sampler: 'Sampler', look: 'Look', negative: 'Negative' };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* ------------------------------------------------------------------ state */
const S = {
  presets: null, models: [], runs: [], run: null, profiles: [],
  view: 'new',             // new | run | profiles
  form: { checkpoints: [], prompts: ['portrait', 'food', 'landscape'], custom: '', seeds: 1,
          axes: [{ param: 'cfg', text: '3.5, 5, 7', values: [] }, { param: '', text: '', values: [] }] },
  estimate: null, estimateError: null, starting: false,
  tab: { model: null, prompt: null, seed: null },
  viewer: null, error: null,
};
const label = name => (S.models.find(m => m.name === name) || {}).label || (name || '').split('/').pop();

/* ------------------------------------------------------------------ form */
function axisValues(a) {
  if (!a.param) return [];
  if (a.param === 'cfg' || a.param === 'steps') {
    return a.text.split(/[,\s]+/).map(x => x.trim()).filter(Boolean).map(Number).filter(x => !Number.isNaN(x));
  }
  return a.values;
}
function specOf() {
  const f = S.form;
  return {
    checkpoints: f.checkpoints, prompts: f.prompts, custom: f.custom, seeds: f.seeds,
    axes: f.axes.filter(a => a.param).map(a => ({ param: a.param, values: axisValues(a) })),
  };
}
let _estTimer = null;
function reestimate() {
  clearTimeout(_estTimer);
  _estTimer = setTimeout(async () => {
    try { S.estimate = await post('/api/lab/estimate', specOf()); S.estimateError = null; }
    catch (e) { S.estimate = null; S.estimateError = e.message; }
    swap('estimate', estimateLine());
  }, 250);
}
const swap = (id, el) => { const old = document.getElementById(id); if (old) old.replaceWith(el); };

function toggle(list, v) { const i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v); }
function chips(options, selected, onToggle) {
  return h('div', { class: 'chips' }, options.map(([value, text]) => h('button', {
    type: 'button', class: 'pill' + (selected.includes(value) ? ' is-on' : ''),
    'aria-pressed': selected.includes(value) ? 'true' : 'false',
    onclick: () => { onToggle(value); render(); reestimate(); },
  }, text)));
}

function axisEditor(a, i) {
  const P = S.presets;
  const other = S.form.axes[1 - i]?.param;
  const params = Object.keys(AXES).filter(p => p !== other);
  let body = null;
  if (a.param === 'cfg' || a.param === 'steps') {
    body = h('input', { class: 'input', value: a.text, 'aria-label': `${AXES[a.param]} values`,
                        placeholder: P.suggest[a.param].join(', '),
                        oninput: e => { a.text = e.target.value; reestimate(); } });
  } else if (a.param === 'sampler') {
    body = chips(P.samplers.map(s => [s, s]), a.values, v => toggle(a.values, v));
  } else if (a.param === 'look') {
    body = chips(Object.entries(P.looks).map(([k, v]) => [k, v.label]), a.values, v => toggle(a.values, v));
  } else if (a.param === 'negative') {
    body = chips(Object.entries(P.negatives).map(([k, v]) => [k, v.label]), a.values, v => toggle(a.values, v));
  }
  return h('div', { class: 'axis' },
    h('div', { class: 'axis-head' },
      h('span', { class: 'lab-label' }, i ? 'Columns' : 'Rows'),
      h('select', { class: 'select', 'aria-label': `Axis ${i + 1}`,
                    onchange: e => {
                      a.param = e.target.value; a.values = [];
                      a.text = (a.param === 'cfg' || a.param === 'steps') ? S.presets.suggest[a.param].join(', ') : '';
                      if (a.param === 'look') a.values = ['none', 'raw'];
                      if (a.param === 'negative') a.values = ['house', 'strict'];
                      render(); reestimate();
                    } },
        h('option', { value: '' }, i ? 'Nothing (one column)' : 'Nothing (one row)'),
        params.map(p => h('option', { value: p, selected: a.param === p }, AXES[p])))),
    body);
}

function estimateLine() {
  let text;
  if (S.estimateError) text = h('span', { style: 'color:var(--danger)' }, S.estimateError);
  else if (S.estimate) text = [h('b', {}, `${S.estimate.cells} images`), ` · ${fmtMin(S.estimate.seconds)} of GPU time, `,
    'in the gaps between people\'s messages'];
  else text = 'Pick models and prompts to see how long it takes.';
  return h('div', { class: 'estimate', id: 'estimate' }, text);
}

function newRunPanel() {
  const P = S.presets, f = S.form;
  return h('div', { class: 'lab-panel glass' },
    h('h2', {}, 'New run'),
    h('div', { class: 'lab-field' }, h('label', {}, 'Models'),
      chips(S.models.map(m => [m.name, m.label]), f.checkpoints, v => toggle(f.checkpoints, v))),
    h('div', { class: 'lab-field' }, h('label', {}, 'Prompts'),
      chips(P.prompts.map(p => [p.key, p.label]), f.prompts, v => toggle(f.prompts, v)),
      h('input', { class: 'input', placeholder: 'Or your own prompt…', value: f.custom, 'aria-label': 'Your own prompt',
                   oninput: e => { f.custom = e.target.value; reestimate(); } })),
    h('div', { class: 'lab-field' }, h('label', {}, 'Seeds (shared by every image)'),
      chips([[1, '1'], [2, '2'], [3, '3']], [f.seeds], v => { f.seeds = v; })),
    h('div', { class: 'lab-field' }, h('label', {}, 'What to compare'),
      f.axes.map((a, i) => axisEditor(a, i))),
    estimateLine(),
    h('button', { class: 'btn btn-primary', disabled: S.starting || !S.estimate, onclick: startRun },
      icon('flask', 'icon-sm'), S.starting ? 'Starting…' : 'Start the run'));
}

async function startRun() {
  S.starting = true; render();
  try {
    const run = await post('/api/lab/runs', specOf());
    await loadRuns(); openRun(run);
  } catch (e) { S.error = e.message; }
  S.starting = false; render();
}

/* ------------------------------------------------------------------- runs */
function runsList() {
  return h('div', { class: 'lab-panel glass' },
    h('div', { class: 'row' }, h('h2', {}, 'Runs'), h('div', { class: 'spacer' }),
      h('button', { class: 'btn btn-sm' + (S.view === 'profiles' ? ' is-on' : ''),
                    onclick: () => { S.view = 'profiles'; loadProfiles(); } }, 'Model defaults')),
    S.runs.length ? h('div', { class: 'runs' }, S.runs.map(r => h('button', {
      class: 'run-link' + (S.run && S.run.id === r.id && S.view === 'run' ? ' is-active' : ''),
      onclick: async () => openRun(await get(`/api/lab/runs/${r.id}`)),
    }, h('span', { class: 't' }, r.title), h('span', { class: 'n' },
      r.state === 'running' ? `${r.done}/${r.total}` : r.state === 'cancelled' ? 'stopped' : `${r.total}`))))
      : h('p', { class: 'muted small', style: 'margin:0' }, 'No runs yet.'));
}

function openRun(run) {
  S.run = run; S.view = 'run';
  const cells = run.cells;
  S.tab = { model: cells[0]?.checkpoint, prompt: cells[0]?.prompt_key, seed: cells[0]?.seed };
  render();
}

function runView() {
  const R = S.run;
  if (!R) return null;
  const cells = R.cells;
  const T = S.tab;
  const models = [...new Set(cells.map(c => c.checkpoint))];
  const prompts = [...new Set(cells.map(c => c.prompt_key))];
  const seeds = [...new Set(cells.map(c => c.seed))];
  const promptLabel = k => k === 'custom' ? 'Your prompt' : (S.presets.prompts.find(p => p.key === k) || {}).label || k;
  const axes = R.spec.axes;
  const rows = axes[0] ? axes[0].values : [null];
  const cols = axes[1] ? axes[1].values : [null];
  const valueLabel = (param, v) => param === 'look' ? S.presets.looks[v].label
    : param === 'negative' ? S.presets.negatives[v].label : String(v);
  const find = (rv, cv) => cells.find(c => c.checkpoint === T.model && c.prompt_key === T.prompt && c.seed === T.seed
    && (!axes[0] || same(c.combo[axes[0].param], rv)) && (!axes[1] || same(c.combo[axes[1].param], cv)));
  const visible = [];
  for (const rv of rows) for (const cv of cols) { const c = find(rv, cv); if (c) visible.push(c); }
  const pct = R.progress.total ? Math.round(100 * (R.progress.done + R.progress.failed) / R.progress.total) : 0;

  return h('div', { class: 'run-view' },
    h('div', { class: 'run-head' },
      h('h2', {}, R.title),
      h('span', { class: 'pill' + (R.state === 'running' ? ' is-on' : '') },
        R.state === 'running' ? `${R.progress.done} of ${R.progress.total} rendered` : R.state === 'done' ? 'done' : 'stopped'),
      h('div', { class: 'spacer' }),
      R.state === 'running' ? h('button', { class: 'btn btn-sm btn-ghost btn-danger', onclick: cancelRun }, 'Stop the run') : null),
    R.state === 'running' ? h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })) : null,
    h('div', { class: 'tabs' }, models.map(m => h('button', {
      class: 'pill' + (T.model === m ? ' is-on' : ''), onclick: () => { T.model = m; render(); } }, label(m)))),
    h('div', { class: 'tabs' }, prompts.map(p => h('button', {
      class: 'pill' + (T.prompt === p ? ' is-on' : ''), onclick: () => { T.prompt = p; render(); } }, promptLabel(p))),
    seeds.length > 1 ? seeds.map((s, i) => h('button', {
      class: 'pill' + (T.seed === s ? ' is-on' : ''), onclick: () => { T.seed = s; render(); } }, `seed ${i + 1}`)) : null),
    h('div', { class: 'lab-grid', style: `grid-template-columns:${axes[0] ? '70px ' : ''}repeat(${cols.length}, minmax(0, 1fr))` },
      axes[1] ? [axes[0] ? h('div') : null, cols.map(cv => h('div', { class: 'hdr' }, `${AXES[axes[1].param]} ${valueLabel(axes[1].param, cv)}`))] : null,
      rows.map(rv => [
        axes[0] ? h('div', { class: 'rhdr' }, AXES[axes[0].param], h('br'), valueLabel(axes[0].param, rv)) : null,
        cols.map(cv => cellEl(find(rv, cv), visible))])),
    h('p', { class: 'small muted', style: 'margin:0' },
      'Every image in this grid shares its seed, so what differs is the setting. ',
      'Star the ones that look most like a real photograph; the tally below adds them up across every prompt and seed.'),
    tallies(R));
}

function cellEl(c, visible) {
  if (!c) return h('div', { class: 'cell', style: 'aspect-ratio:1' }, h('div', { class: 'pending' }, '—'));
  const img = c.image;
  const ar = img && img.width ? `aspect-ratio:${img.width} / ${img.height}` : 'aspect-ratio:1';
  return h('div', { class: 'cell' + (c.stars ? ' is-starred' : '') + (c.running ? ' is-running' : ''), style: ar },
    img ? h('img', { src: img.url, alt: '', loading: 'lazy',
                     onclick: () => { S.viewer = { items: visible, i: visible.indexOf(c) }; render(); } })
      : h('div', { class: 'pending' }, c.state === 'queued' ? (c.running ? 'rendering…' : 'queued') : c.state),
    img ? h('button', { class: 'star' + (c.stars ? ' is-on' : ''), title: c.stars ? 'Unstar' : 'Star',
                        'aria-pressed': c.stars ? 'true' : 'false', onclick: () => star(c) }, icon('star', 'icon-sm')) : null);
}

function tallies(R) {
  if (!R.tallies.some(t => t.rows.some(r => r.stars))) return null;
  return h('div', { class: 'tally glass' },
    h('h2', {}, 'Tally'),
    R.tallies.map(t => [
      h('div', { class: 'lab-label', style: 'margin:10px 0 4px' }, label(t.checkpoint)),
      h('table', {}, t.rows.map((r, i) => h('tr', {},
        h('td', {}, r.label), h('td', { class: 'stars' }, r.stars ? '★'.repeat(Math.min(r.stars, 12)) + (r.stars > 12 ? ` ${r.stars}` : '') : ''),
        h('td', { class: 'small muted' }, `${r.done} images`),
        h('td', { style: 'text-align:right' }, r.cell_id && (i === 0 || r.stars) ? h('button', {
          class: 'btn btn-sm' + (i === 0 && r.stars ? ' btn-primary' : ''), onclick: () => adopt(r.cell_id, t.checkpoint, r.label),
        }, 'Make default') : null))))]));
}

async function star(c) {
  try {
    const r = await post(`/api/lab/cells/${c.id}/star`, { stars: c.stars ? 0 : 1 });
    c.stars = r.stars;
    S.run = await get(`/api/lab/runs/${S.run.id}`);
    render();
  } catch (e) { S.error = e.message; render(); }
}
async function adopt(cellId, checkpoint, what) {
  if (!confirm(`Make "${what}" the default for ${label(checkpoint)}? Every message on that model will use it, for everyone, until you reset it.`)) return;
  try { await post(`/api/lab/cells/${cellId}/adopt`); S.error = null; await loadModels(); S.view = 'profiles'; await loadProfiles(); }
  catch (e) { S.error = e.message; render(); }
}
async function cancelRun() {
  try { S.run = await post(`/api/lab/runs/${S.run.id}/cancel`); await loadRuns(); render(); }
  catch (e) { S.error = e.message; render(); }
}

/* --------------------------------------------------------------- profiles */
function profilesView() {
  return h('div', { class: 'run-view' },
    h('div', { class: 'run-head' }, h('h2', {}, 'Model defaults')),
    h('p', { class: 'small muted', style: 'margin:0' },
      'What each model runs with now. A value in gold was adopted from a run; the rest come from workflows/registry.json. ',
      'Once a setting has proved itself, copy it into the registry so it survives a fresh database.'),
    S.profiles.map(p => {
      const eff = p.effective;
      const tuned = k => k in (p.settings || {});
      const m = S.models.find(x => x.name === p.name);
      return h('div', { class: 'profile-card glass' },
        h('span', { class: 'mm-cover mono-' + (m?.family || 'sd15') },
          m?.cover_url ? h('img', { src: m.cover_url, alt: '' }) : p.label[0]),
        h('div', { style: 'flex:1;min-width:0' },
          h('div', { class: 'row' }, h('b', {}, p.label), h('div', { class: 'spacer' }),
            Object.keys(p.settings || {}).length ? [
              h('button', { class: 'btn btn-sm', title: 'Copy these defaults as JSON for workflows/registry.json',
                            onclick: () => copyJson(p) }, icon('copy', 'icon-sm'), 'Registry JSON'),
              h('button', { class: 'btn btn-sm btn-ghost btn-danger', onclick: () => reset(p) }, 'Reset')] : null),
          h('div', { class: 'small', style: 'margin-top:6px;line-height:1.7' },
            ['steps', 'cfg', 'sampler', 'scheduler'].map(k => h('span', { class: tuned(k) ? 'diff' : 'muted' },
              `${k} ${eff[k]}`, ' · ')),
            h('br'),
            h('span', { class: tuned('look') ? 'diff' : 'muted' }, `look: ${eff.look || 'none'}`), h('br'),
            h('span', { class: tuned('negative') ? 'diff' : 'muted' }, `negative: ${eff.negative || 'none'}`))));
    }));
}
async function reset(p) {
  if (!confirm(`Put ${p.label} back to its registry defaults?`)) return;
  try { await call('DELETE', `/api/lab/profiles?name=${encodeURIComponent(p.name)}`); await loadModels(); await loadProfiles(); }
  catch (e) { S.error = e.message; render(); }
}
function copyJson(p) {
  const text = JSON.stringify({ [p.name]: { defaults: { ...p.base, ...p.settings } } }, null, 2);
  const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text;
  document.body.append(ta); ta.select();
  try { document.execCommand('copy'); } catch { /* shown below regardless */ }
  ta.remove();
  prompt('Copied. For workflows/registry.json:', text);
}

/* ----------------------------------------------------------------- viewer */
function viewer() {
  const V = S.viewer, c = V.items[V.i];
  if (!c || !c.image) { S.viewer = null; return null; }
  const close = () => { S.viewer = null; render(); };
  const go = d => { const i = V.i + d; if (i >= 0 && i < V.items.length && V.items[i].image) { V.i = i; render(); } };
  const s = c.settings;
  const kv = (k, v) => h('div', {}, h('div', { class: 'k' }, k), h('div', { class: 'v' }, String(v)));
  return h('div', { class: 'viewer', role: 'dialog', 'aria-modal': 'true' },
    h('div', { class: 'v-stage', onclick: e => { if (e.target === e.currentTarget) close(); } },
      h('div', { class: 'v-pic', style: glow(c.image.palette) }, h('div', { class: 'halo' }),
        h('img', { src: c.image.url, alt: '' })),
      h('button', { class: 'v-nav prev glass', disabled: V.i === 0, onclick: () => go(-1) }, icon('left')),
      h('button', { class: 'v-nav next glass', disabled: V.i >= V.items.length - 1, onclick: () => go(1) }, icon('right')),
      h('button', { class: 'v-close glass', onclick: close }, icon('x')),
      h('div', { class: 'v-count mono' }, `${V.i + 1} of ${V.items.length} · ← → to compare · Esc to close`)),
    h('aside', { class: 'v-side' },
      kv('Model', label(c.checkpoint)),
      h('div', { class: 'v-grid' }, kv('Steps', s.steps), kv('CFG', s.cfg), kv('Sampler', `${s.sampler} ${s.scheduler}`), kv('Seed', c.seed)),
      kv('Look', s.look || 'none'), kv('Negative', s.negative || 'none'),
      h('div', { class: 'v-acts' },
        h('button', { class: 'btn fav wide' + (c.stars ? ' is-on' : ''), onclick: () => star(c) },
          icon('star', 'icon-sm'), c.stars ? 'Starred' : 'Star'))));
}

/* ------------------------------------------------------------------ page */
function render() {
  const main = S.view === 'run' ? runView() : S.view === 'profiles' ? profilesView()
    : h('div', { class: 'run-view' }, h('div', { class: 'empty-note' },
      'Pick models, prompts and what to compare, then start a run. ',
      'It renders only while nobody else needs the GPU, and gives way the moment someone sends a message.'));
  $app.replaceChildren(h('div', { class: 'wrap wide adm' },
    h('header', { class: 'adm-head' },
      h('h1', {}, h('span', { class: 'grad-text' }, 'Photoreal Lab')),
      h('div', { class: 'links' }, h('a', { class: 'btn btn-sm', href: '/admin' }, 'Admin'),
        h('a', { class: 'btn btn-sm btn-ghost', href: '/' }, 'Back to the app'))),
    S.error ? h('div', { class: 'card', style: 'padding:12px 14px;margin-top:14px;color:var(--danger)' }, S.error) : null,
    S.presets ? h('div', { class: 'lab', style: 'margin-top:20px' },
      h('div', { style: 'display:flex;flex-direction:column;gap:16px' }, newRunPanel(), runsList()),
      main) : h('p', { class: 'muted' }, 'Loading…')),
  S.viewer ? viewer() : null);
}

async function loadRuns() { try { S.runs = (await get('/api/lab/runs')).runs; } catch (e) { S.error = e.message; } }
async function loadModels() { try { S.models = (await get('/api/models')).models; } catch (e) { S.error = e.message; } }
async function loadProfiles() { try { S.profiles = (await get('/api/lab/profiles')).profiles; } catch (e) { S.error = e.message; } render(); }

async function poll() {
  // Only while someone is looking, and only while the run is still going.
  if (document.visibilityState === 'visible' && S.view === 'run' && S.run && S.run.state === 'running') {
    try {
      S.run = await get(`/api/lab/runs/${S.run.id}`);
      if (S.run.state !== 'running') await loadRuns();
      render();
    } catch { /* the next tick tries again */ }
  }
  setTimeout(poll, 2500);
}

async function boot() {
  render();
  try { S.presets = await get('/api/lab/presets'); }
  catch (e) { S.error = e.message; render(); return; }
  await Promise.all([loadRuns(), loadModels()]);
  S.form.checkpoints = S.models.filter(m => m.traits.includes('fast')).map(m => m.name);
  render();
  reestimate();
  document.addEventListener('keydown', e => {
    if (!S.viewer) return;
    if (e.key === 'Escape') { S.viewer = null; render(); }
    else if (e.key === 'ArrowLeft' && S.viewer.i > 0) { S.viewer.i--; render(); }
    else if (e.key === 'ArrowRight' && S.viewer.i < S.viewer.items.length - 1) { S.viewer.i++; render(); }
  });
  poll();
}
boot();
