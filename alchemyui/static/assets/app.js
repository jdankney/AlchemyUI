// AlchemyUI SPA -- vanilla JS, no build step, no framework, no CDN.
// Served by FastAPI at /. Styling lives in styles.css (hand-written).
//
// Design brief: minute-scale waits. Never a bare spinner -- a card always says
// where a job actually is (queue position, then step count, then elapsed and
// ETA). One input and one button by default; everything else behind a
// disclosure. The gallery is the main surface.
const $app = document.getElementById('app');

/* ------------------------------------------------------------------ api */
const api = {
  async get(p) { const r = await fetch(p); if (!r.ok) throw await err(r); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
    if (!r.ok) throw await err(r); return r.json();
  },
  async del(p) { const r = await fetch(p, { method: 'DELETE' }); if (!r.ok) throw await err(r); return r.json(); },
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
  stop: 'M7 7h10v10H7z',
  x: 'M18 6L6 18M6 6l12 12',
  chevron: 'M6 9l6 6 6-6',
  down: 'M12 5v14M19 12l-7 7-7-7',
  dice: 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01',
  lock: 'M6 10h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM8 10V7a4 4 0 0 1 8 0v3',
  unlock: 'M6 10h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM8 10V7a4 4 0 0 1 7.5-2',
  alert: 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01',
  trash: 'M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6',
  redo: 'M3 12a9 9 0 1 0 2.6-6.4M3 4v5h5',
  zap: 'M13 2L3 14h9l-1 8 10-12h-9l1-8z',
  upload: 'M12 19V5M5 12l7-7 7 7',
  image: 'M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM8.5 10.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM22 15l-5-5L6 19',
  brush: 'M9.06 11.9l8.07-8.06a2.85 2.85 0 1 1 4.03 4.03l-8.06 8.08M7.07 14.94c-1.66 0-3 1.35-3 3.02 0 1.33-2.5 1.52-2 2.02 1.08 1.1 2.49 2.02 4 2.02 2.2 0 4-1.8 4-4.04a3.01 3.01 0 0 0-3-3.02z',
  eraser: 'M20 20H8.5L3.2 14.7a2 2 0 0 1 0-2.8l8.4-8.4a2 2 0 0 1 2.8 0l6 6a2 2 0 0 1 0 2.8L13 20M8 20l-4.8-4.8M14.5 6.5L18 10',
  undo: 'M3 7v6h6M3.5 13a9 9 0 1 0 2.6-6.4L3 9',
  expand: 'M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7',
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
const shortCkpt = c => (c || '').replace(/\.(safetensors|ckpt|pt)$/, '');

/* ----------------------------------------------------------------- state */
const S = {
  me: null,
  workflows: [],
  wf: null,               // selected workflow id
  jobs: [],               // live + recent jobs, newest first
  images: [],             // gallery, newest first
  connected: false,       // our SSE stream
  comfyConnected: false,  // the app's socket to ComfyUI
  resident: null,         // checkpoint ComfyUI is believed to have loaded
  lightbox: null,
  // composer -- kept in state so a re-render never loses what was typed
  form: {
    prompt: '', negative: null, aspect: null, seed: '', steps: null, cfg: null,
    sampler: null, scheduler: null,
    denoise: null, grow_mask: null, feathering: null,          // Phase 2
  },
  seedLocked: false,
  advancedOpen: false,
  optionsOpen: false,
  submitting: false,

  // Phase 2 -----------------------------------------------------------------
  mode: 'txt2img',        // which kind of workflow the composer is showing
  upload: null,           // {id, url, width, height, hasMask}
  uploading: false,
  // The mask is kept as strokes in the source image's own pixel coordinates,
  // not as a bitmap. Undo is then exact and free, the display canvas can be
  // any size, and the export is always at the image's true resolution --
  // where a snapshot stack would cost ~7MB per step at 1536x1152.
  mask: { strokes: [], brush: 64, erasing: false },
  pads: { left: 0, top: 0, right: 256, bottom: 0 },
};

const byKind = kind => S.workflows.find(w => w.kind === kind);
const workflow = () => byKind(S.mode) || S.workflows.find(w => w.id === S.wf);
const activeJobs = () => S.jobs.filter(j => j.status === 'queued' || j.status === 'running');
const failedJobs = () => S.jobs.filter(j => j.status === 'failed' && !j.dismissed);

/* --------------------------------------------------------- SSE: one stream */
// One EventSource for the whole app. Image jobs outlive the request that
// created them, so progress can't ride on the submit response the way
// Llamacracy's tokens do. EventSource brings its own reconnect backoff;
// anything missed while disconnected is recovered from /api/jobs + /api/images.
function connectEvents() {
  const es = new EventSource('/api/events');
  es.onopen = () => {
    const wasDown = !S.connected;
    S.connected = true;
    if (wasDown) recoverState();   // fill whatever the gap swallowed
    renderTopbar();
  };
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type === 'queue') onQueue(d);
    else if (d.type === 'progress') onProgress(d);
    else if (d.type === 'job') onJob(d.job);
    else if (d.type === 'image') onImage(d.image);
  };
  es.onerror = () => { S.connected = false; renderTopbar(); /* browser retries */ };
}

function onQueue(d) {
  S.comfyConnected = d.comfy_connected;
  S.resident = d.resident_checkpoint;
  for (const qj of d.jobs) {
    const j = S.jobs.find(x => x.id === qj.id);
    if (j) { j.position = qj.position; j.eta_s = qj.eta_s; j.status = qj.status; j.cold_start = qj.cold_start; }
    else S.jobs.unshift(qj);
  }
  renderTopbar(); renderJobCards();
}

function onProgress(d) {
  const j = S.jobs.find(x => x.id === d.job_id);
  if (!j) return;
  j.progress = { value: d.value, max: d.max };
  j.eta_s = d.eta_s;
  j.started_at = d.started_at || j.started_at;
  renderJobCard(j.id);
}

function onJob(job) {
  const i = S.jobs.findIndex(x => x.id === job.id);
  if (i >= 0) S.jobs[i] = { ...S.jobs[i], ...job };
  else S.jobs.unshift(job);
  // A completed job's images speak for it; the placeholder card steps aside.
  if (job.status === 'cancelled' || job.status === 'completed') {
    setTimeout(() => { S.jobs = S.jobs.filter(x => x.id !== job.id || x.status === 'failed'); renderJobCards(); }, 400);
  }
  renderJobCards();
}

function onImage(image) {
  if (!S.images.some(x => x.id === image.id)) S.images.unshift(image);
  renderGallery();
}

async function recoverState() {
  try {
    const [jobs, images] = await Promise.all([api.get('/api/jobs?limit=50'), api.get('/api/images?limit=200')]);
    const live = jobs.jobs.filter(j => j.status === 'queued' || j.status === 'running' || j.status === 'failed');
    // Keep any locally-dismissed failures dismissed across a reconnect.
    const dismissed = new Set(S.jobs.filter(j => j.dismissed).map(j => j.id));
    S.jobs = live.map(j => dismissed.has(j.id) ? { ...j, dismissed: true } : j);
    S.images = images.images;
    renderJobCards(); renderGallery();
  } catch { /* the next reconnect tries again */ }
}

/* --------------------------------------------------------------- rendering */
function render() {
  $app.replaceChildren(
    topbar(),
    h('div', { class: 'app-body' },
      h('div', { class: 'wrap' }, composer(), gallery())),
    h('div', { class: 'toasts', id: 'toasts' }),
    S.lightbox ? lightbox() : null,
  );
  autosize(document.getElementById('prompt'));
}

/* topbar */
function topbar() {
  return h('header', { class: 'topbar' },
    h('a', { class: 'brand', href: '#', onclick: e => e.preventDefault() },
      h('img', { src: '/assets/alchemyui-favicon.svg', alt: '' }),
      h('span', { class: 'wordmark' }, 'AlchemyUI')),
    h('span', { id: 'status-badge' }, statusBadge()),
    h('div', { class: 'spacer' }),
    h('span', { id: 'queue-badge', class: 'small muted' }, queueLabel()),
  );
}
function renderTopbar() {
  const b = document.getElementById('status-badge'); if (b) b.replaceChildren(statusBadge());
  const q = document.getElementById('queue-badge'); if (q) q.replaceChildren(queueLabel());
}
function statusBadge() {
  if (!S.comfyConnected) {
    return h('span', { class: 'badge is-off', title: 'The app cannot reach ComfyUI. It keeps retrying.' },
      h('i', { class: 'dot' }), 'ComfyUI offline');
  }
  const running = activeJobs().some(j => j.status === 'running');
  if (running) return h('span', { class: 'badge is-busy', title: 'Generating' }, h('i', { class: 'dot' }), 'generating');
  if (S.resident) {
    return h('span', { class: 'badge is-on', title: `${S.resident} is loaded -- no swap needed` },
      h('i', { class: 'dot' }), shortCkpt(S.resident));
  }
  return h('span', { class: 'badge', title: 'Nothing loaded yet -- the first run pays a cold start' },
    h('i', { class: 'dot' }), 'idle');
}
function queueLabel() {
  const n = activeJobs().length;
  return h('span', {}, n ? `${plural(n, 'job')} in the queue` : '');
}

/* ---------------------------------------------------------------- composer */
const MODES = [
  ['txt2img',  'Generate', 'sparkle'],
  ['img2img',  'Transform', 'image'],
  ['inpaint',  'Paint over', 'brush'],
  ['outpaint', 'Extend', 'expand'],
];

function modeControl() {
  const available = MODES.filter(([k]) => byKind(k));
  if (available.length < 2) return null;
  return h('div', { class: 'seg seg-modes', role: 'group', 'aria-label': 'What to do' },
    available.map(([k, label, ic]) => h('button', {
      type: 'button', class: k === S.mode ? 'is-active' : '',
      'aria-pressed': k === S.mode ? 'true' : 'false',
      onclick: () => setMode(k),
    }, icon(ic, 'icon-sm'), label)));
}

function setMode(kind) {
  if (S.mode === kind) return;
  S.mode = kind;
  // The upload carries over between modes -- transforming an image and then
  // painting over the same one is the obvious next thing to want. The mask
  // does not: it was painted for a different job.
  S.mask = { strokes: [], brush: S.mask.brush, erasing: false };
  render();
  focusPrompt();
}

function composer() {
  const w = workflow();
  if (!w) return h('div', { class: 'composer' }, h('div', { class: 'empty-note' }, 'No workflows configured.'));
  const f = S.form;
  const needsImage = (w.requires || []).includes('image');
  const needsMask = (w.requires || []).includes('mask');

  return h('div', { class: 'composer' },
    modeControl(),
    h('form', { class: 'compose-card', onsubmit: submit },
      needsImage ? uploadZone(w) : null,
      needsMask && S.upload ? maskEditor() : null,
      w.kind === 'outpaint' && S.upload ? outpaintControls() : null,

      h('textarea', {
        id: 'prompt', rows: 1, value: f.prompt,
        placeholder: promptPlaceholder(w), 'aria-label': 'Prompt',
        oninput: e => { f.prompt = e.target.value; autosize(e.target); },
        onkeydown: e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit(e); } },
      }),

      w.kind === 'img2img' ? denoiseField(w) : null,

      h('div', { class: 'compose-tools' },
        w.aspects && Object.keys(w.aspects).length && w.inputs.includes('aspect')
          ? aspectControl(w, f.aspect || w.defaults.aspect || Object.keys(w.aspects)[0]) : null,
        h('div', { class: 'spacer' }),
        h('button', {
          type: 'submit', class: 'btn btn-primary',
          disabled: S.submitting || !S.comfyConnected || (needsImage && !S.upload),
          title: !S.comfyConnected ? 'ComfyUI is not reachable'
            : needsImage && !S.upload ? 'Add an image first' : 'Generate (Ctrl+Enter)',
        }, icon('sparkle', 'icon-sm'), submitLabel(w))),

      h('details', {
        class: 'panel', open: S.optionsOpen,
        ontoggle: e => { S.optionsOpen = e.target.open; },
      },
        h('summary', {}, icon('chevron', 'icon-sm chev'), 'More options'),
        h('div', { class: 'panel-body' },
          h('div', { class: 'field' },
            h('label', { for: 'negative' }, 'Negative prompt — what to keep out'),
            h('textarea', {
              id: 'negative', class: 'input', rows: 2,
              value: f.negative == null ? (w.defaults.negative || '') : f.negative,
              oninput: e => { f.negative = e.target.value; },
            })),

          h('details', {
            class: 'panel', open: S.advancedOpen,
            ontoggle: e => { S.advancedOpen = e.target.open; },
          },
            h('summary', {}, icon('chevron', 'icon-sm chev'), 'Advanced'),
            h('div', { class: 'panel-body' },
              sliderField('steps', 'Steps', w.limits.steps || [1, 60], 1,
                f.steps ?? w.defaults.steps, v => { S.form.steps = v; }, v => String(v)),
              sliderField('cfg', 'CFG — prompt adherence', w.limits.cfg || [1, 15], 0.1,
                f.cfg ?? w.defaults.cfg, v => { S.form.cfg = v; }, v => v.toFixed(1)),
              w.inputs.includes('grow_mask') ? sliderField(
                'grow_mask', 'Mask feather — blends the seam', w.limits.grow_mask || [0, 64], 1,
                f.grow_mask ?? w.defaults.grow_mask, v => { S.form.grow_mask = v; },
                v => v + ' px') : null,
              w.inputs.includes('feathering') ? sliderField(
                'feathering', 'Edge blend — softens the join to the new area',
                w.limits.feathering || [0, 128], 1,
                f.feathering ?? w.defaults.feathering, v => { S.form.feathering = v; },
                v => v + ' px') : null,
              h('div', { class: 'grid-2' },
                selectField('sampler', 'Sampler', w.choices.sampler || [], f.sampler ?? w.defaults.sampler),
                selectField('scheduler', 'Scheduler', w.choices.scheduler || [], f.scheduler ?? w.defaults.scheduler)),
              seedField())))),

      h('div', { class: 'compose-hint', id: 'compose-hint' }, ...hintKids()),
    ),
  );
}

const promptPlaceholder = w =>
  w.kind === 'inpaint' ? 'Describe what should appear in the painted area…'
  : w.kind === 'outpaint' ? 'Describe what continues beyond the edges…'
  : w.kind === 'img2img' ? 'Describe what the image should become…'
  : 'Describe the image you want…';
const submitLabel = w =>
  w.kind === 'inpaint' ? 'Repaint' : w.kind === 'outpaint' ? 'Extend'
  : w.kind === 'img2img' ? 'Transform' : 'Generate';

/* ------------------------------------------------------------ upload zone */
function uploadZone(w) {
  if (S.upload) {
    return h('div', { class: 'upload-row' },
      h('img', { class: 'thumb', src: S.upload.url, alt: 'Source image' }),
      h('div', { class: 'upload-meta' },
        h('div', {}, 'Source image'),
        h('div', { class: 'small faint' },
          `${S.upload.width} × ${S.upload.height}` +
          (S.upload.resized ? ' · scaled to fit the GPU' : ''))),
      h('div', { class: 'spacer' }),
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm', title: 'Use a different image',
        onclick: () => { S.upload = null; S.mask.strokes = []; render(); },
      }, icon('x', 'icon-sm'), 'Replace'));
  }

  const pick = () => document.getElementById('img-input').click();
  return h('div', {
    class: 'dropzone' + (S.uploading ? ' is-busy' : ''),
    role: 'button', tabindex: 0,
    onclick: pick,
    onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } },
    ondragover: e => { e.preventDefault(); e.currentTarget.classList.add('is-over'); },
    ondragleave: e => e.currentTarget.classList.remove('is-over'),
    ondrop: e => {
      e.preventDefault(); e.currentTarget.classList.remove('is-over');
      const file = e.dataTransfer.files[0];
      if (file) doUpload(file);
    },
  },
    h('input', {
      type: 'file', id: 'img-input', hidden: true, accept: 'image/png,image/jpeg,image/webp',
      onchange: e => { const f = e.target.files[0]; e.target.value = ''; if (f) doUpload(f); },
    }),
    icon('upload'),
    h('div', {}, S.uploading ? 'Uploading…' : 'Drop an image here, or click to choose one'),
    h('div', { class: 'small faint' },
      w.kind === 'outpaint' ? 'It will be placed in the middle of a bigger canvas'
        : 'PNG, JPEG or WebP'));
}

async function doUpload(file) {
  S.uploading = true; render();
  try {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/uploads', { method: 'POST', body: fd });
    if (!r.ok) throw await err(r);
    const up = await r.json();
    // The server may have scaled the image down; everything after this point
    // -- the mask canvas especially -- works in the returned dimensions.
    S.upload = { ...up, hasMask: false };
    S.mask.strokes = [];
  } catch (e) {
    toast(e.message, true);
  } finally {
    S.uploading = false; render();
  }
}

/* ----------------------------------------------------------- mask editor */
// Strokes are recorded in the source image's own pixel space, so the canvas
// can be laid out at any size and the exported mask is always exact.
function maskEditor() {
  const up = S.upload;
  const canvas = h('canvas', {
    class: 'mask-canvas', width: up.width, height: up.height,
    'aria-label': 'Brush over the area to replace',
  });

  let drawing = null;
  const at = e => {
    const r = canvas.getBoundingClientRect();
    return [(e.clientX - r.left) * (up.width / r.width),
            (e.clientY - r.top) * (up.height / r.height)];
  };
  canvas.addEventListener('pointerdown', e => {
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    drawing = { points: [at(e)], brush: S.mask.brush, erase: S.mask.erasing };
    S.mask.strokes.push(drawing);
    paintMask(canvas);
  });
  canvas.addEventListener('pointermove', e => {
    if (!drawing) return;
    drawing.points.push(at(e));
    paintMask(canvas);
  });
  const stop = () => {
    if (!drawing) return;
    // A tap with no movement should still leave a dot.
    if (drawing.points.length === 1) drawing.points.push(drawing.points[0]);
    drawing = null;
    renderMaskTools();
  };
  canvas.addEventListener('pointerup', stop);
  canvas.addEventListener('pointercancel', stop);
  canvas.addEventListener('pointerleave', stop);

  requestAnimationFrame(() => paintMask(canvas));

  return h('div', { class: 'mask-editor' },
    h('div', { class: 'mask-stage', style: `aspect-ratio:${up.width}/${up.height}` },
      h('img', { src: up.url, alt: 'Source image', draggable: false }),
      canvas),
    h('div', { class: 'mask-tools', id: 'mask-tools' }, ...maskToolKids()),
  );
}

function maskToolKids() {
  const size = h('span', { class: 'val' }, S.mask.brush + ' px');
  return [
    h('button', {
      type: 'button', class: 'btn btn-sm' + (S.mask.erasing ? '' : ' is-on'),
      title: 'Brush', 'aria-pressed': S.mask.erasing ? 'false' : 'true',
      onclick: () => { S.mask.erasing = false; renderMaskTools(); },
    }, icon('brush', 'icon-sm'), 'Brush'),
    h('button', {
      type: 'button', class: 'btn btn-sm' + (S.mask.erasing ? ' is-on' : ''),
      title: 'Eraser', 'aria-pressed': S.mask.erasing ? 'true' : 'false',
      onclick: () => { S.mask.erasing = true; renderMaskTools(); },
    }, icon('eraser', 'icon-sm'), 'Erase'),
    h('span', { class: 'slider mask-size' },
      h('input', {
        type: 'range', min: 8, max: 256, step: 4, value: S.mask.brush,
        'aria-label': 'Brush size',
        oninput: e => { S.mask.brush = +e.target.value; size.textContent = S.mask.brush + ' px'; },
      }), size),
    h('div', { class: 'spacer' }),
    h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', title: 'Undo the last stroke',
      disabled: !S.mask.strokes.length,
      onclick: () => { S.mask.strokes.pop(); repaintMask(); },
    }, icon('undo', 'icon-sm'), 'Undo'),
    h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', title: 'Clear the mask',
      disabled: !S.mask.strokes.length,
      onclick: () => { S.mask.strokes = []; repaintMask(); },
    }, icon('trash', 'icon-sm'), 'Clear'),
  ];
}
function renderMaskTools() {
  const el = document.getElementById('mask-tools');
  if (el) el.replaceChildren(...maskToolKids());
}
function repaintMask() {
  const c = document.querySelector('.mask-canvas');
  if (c) paintMask(c);
  renderMaskTools();
}

// One routine draws both the on-screen overlay and the exported mask; only
// the colours differ. That is what guarantees what you painted is what gets
// replaced -- there is no second implementation to drift.
function strokePath(ctx, stroke) {
  ctx.lineWidth = stroke.brush;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  stroke.points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.stroke();
}
function paintMask(canvas) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = 'rgba(201, 162, 255, .55)';
  for (const st of S.mask.strokes) {
    ctx.globalCompositeOperation = st.erase ? 'destination-out' : 'source-over';
    strokePath(ctx, st);
  }
  ctx.globalCompositeOperation = 'source-over';
}
// White on black: white is the region to replace, end to end -- what the
// person paints, what LoadImageMask reads from the red channel, and what
// VAEEncodeForInpaint blanks out.
function exportMask() {
  const up = S.upload;
  const c = document.createElement('canvas');
  c.width = up.width; c.height = up.height;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.strokeStyle = '#fff';
  for (const st of S.mask.strokes) {
    ctx.globalCompositeOperation = st.erase ? 'destination-out' : 'source-over';
    strokePath(ctx, st);
  }
  // destination-out leaves transparent holes; flatten back onto black so the
  // red channel is unambiguous.
  const flat = document.createElement('canvas');
  flat.width = c.width; flat.height = c.height;
  const fctx = flat.getContext('2d');
  fctx.fillStyle = '#000';
  fctx.fillRect(0, 0, flat.width, flat.height);
  fctx.drawImage(c, 0, 0);
  return new Promise(res => flat.toBlob(res, 'image/png'));
}

/* ------------------------------------------------------ outpaint controls */
function outpaintControls() {
  const up = S.upload;
  const w = workflow();
  const max = (w.limits.pad_left || [0, 512])[1];
  const box = (key, label) => {
    const out = h('span', { class: 'val' }, S.pads[key] + ' px');
    return h('div', { class: 'field' },
      h('label', {}, label),
      h('div', { class: 'slider' },
        h('input', {
          type: 'range', min: 0, max, step: 8, value: S.pads[key],
          'aria-label': label,
          oninput: e => { S.pads[key] = +e.target.value; out.textContent = S.pads[key] + ' px'; renderPadPreview(); },
        }), out));
  };
  return h('div', { class: 'outpaint' },
    h('div', { class: 'pad-preview', id: 'pad-preview' }, padPreview()),
    h('div', { class: 'grid-2' },
      box('left', 'Left'), box('right', 'Right'),
      box('top', 'Top'), box('bottom', 'Bottom')),
    h('div', { class: 'small faint', id: 'pad-note' }, padNote(up)));
}
// A scale drawing of what the canvas becomes. Four numbers do not tell you
// that you asked for a panorama; this does.
function padPreview() {
  const up = S.upload, p = S.pads;
  const totalW = up.width + p.left + p.right;
  const totalH = up.height + p.top + p.bottom;
  const scale = 150 / Math.max(totalW, totalH);
  return h('div', {
    class: 'pad-box',
    style: `width:${Math.round(totalW * scale)}px;height:${Math.round(totalH * scale)}px`,
  }, h('div', {
    class: 'pad-orig',
    style: `left:${p.left * scale}px;top:${p.top * scale}px;` +
           `width:${up.width * scale}px;height:${up.height * scale}px`,
  }));
}
function padNote(up) {
  const p = S.pads;
  const totalW = up.width + p.left + p.right;
  const totalH = up.height + p.top + p.bottom;
  const added = totalW * totalH - up.width * up.height;
  if (!added) return 'Choose a direction to extend into.';
  return `New canvas ${totalW} × ${totalH} — ` +
    `${Math.round(100 * added / (totalW * totalH))}% of it generated.` +
    (Math.max(totalW, totalH) > 1600 ? ' That is a lot to ask of 6GB of VRAM.' : '');
}
function renderPadPreview() {
  const el = document.getElementById('pad-preview');
  if (el) el.replaceChildren(padPreview());
  const n = document.getElementById('pad-note');
  if (n && S.upload) n.textContent = padNote(S.upload);
}

/* ------------------------------------------------------------ denoise */
// The spec's plain-language labelling: the number is meaningless to most
// people, but "keeps the composition" is not.
const DENOISE_LABELS = [
  [0.40, 'Subtle — a light polish'],
  [0.52, 'Noticeable — keeps the composition'],
  [0.66, 'Strong — keeps the rough shapes'],
  [0.80, 'Loose — the source is only a suggestion'],
  [1.01, 'Complete reimagining'],
];
const denoiseLabel = v => (DENOISE_LABELS.find(([t]) => v < t) || DENOISE_LABELS.at(-1))[1];

function denoiseField(w) {
  const [lo, hi] = w.limits.denoise || [0.3, 0.9];
  const value = S.form.denoise ?? w.defaults.denoise;
  const out = h('div', { class: 'denoise-label' }, denoiseLabel(value));
  return h('div', { class: 'field denoise' },
    h('label', { for: 'denoise' }, 'How much should change?'),
    h('input', {
      type: 'range', id: 'denoise', min: lo, max: hi, step: 0.01, value,
      oninput: e => {
        S.form.denoise = parseFloat(e.target.value);
        out.textContent = denoiseLabel(S.form.denoise);
      },
    }),
    h('div', { class: 'denoise-ends' },
      h('span', {}, 'Keep the original'), h('span', {}, 'Start over')),
    out);
}

function aspectControl(w, current) {
  const keys = Object.keys(w.aspects);
  return h('div', { class: 'seg', role: 'group', 'aria-label': 'Aspect ratio' },
    keys.map(k => {
      const [cw, ch] = w.aspects[k];
      // A tiny box in the actual proportions reads faster than the numbers.
      const box = 13;
      const bw = cw >= ch ? box : Math.round(box * cw / ch);
      const bh = ch >= cw ? box : Math.round(box * ch / cw);
      return h('button', {
        type: 'button', class: k === current ? 'is-active' : '',
        title: `${cw} × ${ch}`, 'aria-pressed': k === current ? 'true' : 'false',
        onclick: () => { S.form.aspect = k; render(); },
      }, h('i', { class: 'ratio', style: `width:${bw}px;height:${bh}px` }), k);
    }));
}

function sliderField(name, label, [lo, hi], step, value, onchange, fmt) {
  const out = h('span', { class: 'val' }, fmt(value));
  return h('div', { class: 'field' },
    h('label', { for: name }, label),
    h('div', { class: 'slider' },
      h('input', {
        type: 'range', id: name, min: lo, max: hi, step, value,
        oninput: e => {
          const v = step < 1 ? parseFloat(e.target.value) : parseInt(e.target.value, 10);
          onchange(v); out.textContent = fmt(v); renderHint();
        },
      }),
      out));
}

function selectField(name, label, choices, value) {
  return h('div', { class: 'field' },
    h('label', { for: name }, label),
    h('select', {
      id: name, class: 'select',
      onchange: e => { S.form[name] = e.target.value; },
    }, choices.map(c => h('option', { value: c, selected: c === value }, c))));
}

function seedField() {
  return h('div', { class: 'field' },
    h('label', { for: 'seed' }, 'Seed — lock it to vary one thing at a time'),
    h('div', { class: 'seed-row' },
      h('div', { class: 'field' },
        h('input', {
          type: 'text', inputmode: 'numeric', id: 'seed', class: 'input mono',
          placeholder: 'random', value: S.form.seed,
          oninput: e => { S.form.seed = e.target.value.replace(/[^0-9]/g, ''); },
        })),
      h('button', {
        type: 'button', class: 'btn btn-icon' + (S.seedLocked ? ' is-on' : ''),
        'aria-pressed': S.seedLocked ? 'true' : 'false',
        title: S.seedLocked ? 'Seed locked — reused on every run' : 'Seed unlocked — a new one each run',
        onclick: () => { S.seedLocked = !S.seedLocked; render(); },
      }, icon(S.seedLocked ? 'lock' : 'unlock', 'icon-sm')),
      h('button', {
        type: 'button', class: 'btn btn-icon', title: 'New random seed',
        onclick: () => { S.form.seed = ''; S.seedLocked = false; render(); },
      }, icon('dice', 'icon-sm'))));
}

function hintKids() {
  const w = workflow();
  if (!w) return [];
  let steps = S.form.steps ?? w.defaults.steps;
  // img2img runs steps*denoise of them, so the honest estimate is shorter.
  if (w.kind === 'img2img') steps *= (S.form.denoise ?? w.defaults.denoise ?? 1);
  const cold = !w.resident;
  const est = Math.round(steps * w.seconds_per_step + (cold ? w.load_seconds : 0));
  const ahead = activeJobs().length;
  const kids = [
    h('span', {}, w.display),
    h('span', { class: 'sep' }, '·'),
    h('span', { title: cold ? 'The checkpoint is not loaded — this run pays the swap' : 'Checkpoint already loaded' },
      cold ? `~${fmtDur(est)} (incl. ~${fmtDur(w.load_seconds)} cold start)` : `~${fmtDur(est)}`),
  ];
  if (ahead) kids.push(h('span', { class: 'sep' }, '·'), h('span', {}, `${plural(ahead, 'job')} ahead`));
  if (w.blurb) kids.push(h('span', { class: 'sep' }, '·'), h('span', { class: 'faint' }, w.blurb));
  kids.push(h('span', { class: 'spacer' }), h('span', {}, h('kbd', {}, 'Ctrl'), ' ', h('kbd', {}, 'Enter'), ' to generate'));
  return kids;
}
function renderHint() { const el = document.getElementById('compose-hint'); if (el) el.replaceChildren(...hintKids()); }

function autosize(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.35) + 'px';
}
// true only on a real mouse/trackpad, so the composer auto-focus never summons
// the on-screen keyboard on a phone (a Llamacracy lesson).
function hasFinePointer() {
  try { return matchMedia('(hover: hover) and (pointer: fine)').matches; } catch { return false; }
}
function focusPrompt() {
  if (!hasFinePointer()) return;
  const ta = document.getElementById('prompt'); if (ta) ta.focus();
}

/* ----------------------------------------------------------------- gallery */
function gallery() {
  return h('div', { class: 'gallery' },
    h('div', { class: 'gallery-head' }, h('h2', {}, 'Gallery'),
      h('span', { class: 'small faint', id: 'gallery-count' }, galleryCount())),
    h('div', { class: 'grid', id: 'grid' }, ...gridKids()));
}
function galleryCount() { return S.images.length ? plural(S.images.length, 'image') : ''; }
function gridKids() {
  const cards = [...failedJobs(), ...activeJobs()].map(jobCard);
  if (!cards.length && !S.images.length) {
    return [h('div', { class: 'empty-note', style: 'grid-column:1/-1' },
      'Nothing generated yet. Describe something above and hit Generate.')];
  }
  return [...cards, ...S.images.map(imageCard)];
}
function renderGallery() {
  const g = document.getElementById('grid'); if (g) g.replaceChildren(...gridKids());
  const c = document.getElementById('gallery-count'); if (c) c.textContent = galleryCount();
}
// job cards change often (every sampler step); the images below them don't.
function renderJobCards() { renderGallery(); renderTopbar(); renderHint(); }
function renderJobCard(id) {
  const el = document.getElementById('job-' + id);
  const job = S.jobs.find(j => j.id === id);
  if (!el || !job) return;
  el.replaceWith(jobCard(job));
}

function jobCard(job) {
  if (job.status === 'failed') return failedCard(job);
  const p = job.progress || { value: 0, max: 0 };
  const stepping = p.max > 0;
  const pct = stepping ? Math.round(100 * p.value / p.max) : 0;

  let state;
  if (job.status === 'queued') {
    // A real position, not a spinner: "2 ahead of you" is a different
    // experience from "loading".
    state = job.position > 0 ? `Queued · ${job.position} ahead of you`
      : job.position === 0 ? 'Starting…' : 'Queued';
  } else if (stepping) {
    state = `Step ${p.value} of ${p.max}`;
  } else if (job.cold_start) {
    state = 'Loading the checkpoint…';
  } else {
    state = 'Starting…';
  }

  const elapsed = job.started_at ? (Date.now() / 1000 - job.started_at) : null;

  return h('div', { class: 'card card-job', id: 'job-' + job.id },
    h('div', { class: 'state' },
      h('i', { class: 'pulse' }), h('span', {}, state),
      h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn btn-ghost btn-sm btn-danger', title: 'Cancel this job',
        onclick: () => cancelJob(job.id),
      }, icon('stop', 'icon-sm'))),
    h('div', { class: 'bar' + (stepping ? '' : ' indet') },
      h('i', { style: stepping ? `width:${pct}%` : '' })),
    h('div', { class: 'meta' },
      elapsed != null ? h('span', { title: 'Elapsed' }, fmtDur(elapsed)) : null,
      job.eta_s != null ? h('span', { class: 'faint', title: 'Estimated time remaining, learned from previous runs' }, `~${fmtDur(job.eta_s)} left`) : null),
    h('div', { class: 'prompt' }, job.params?.prompt || ''),
  );
}

function failedCard(job) {
  return h('div', { class: 'card card-job card-failed', id: 'job-' + job.id },
    h('div', { class: 'state' },
      icon('alert', 'icon-sm'), h('span', {}, 'Failed'),
      h('div', { class: 'spacer' }),
      h('button', {
        class: 'btn btn-ghost btn-sm', title: 'Dismiss',
        onclick: () => { job.dismissed = true; renderJobCards(); },
      }, icon('x', 'icon-sm'))),
    h('div', { class: 'err-msg' }, job.error_text || 'Something went wrong.'),
    // The traceback stays available -- on this GPU a black or NaN image is a
    // precision problem, not an app bug, and only the real trace says which.
    job.error_detail ? h('details', {},
      h('summary', {}, 'Technical details'),
      h('pre', {}, job.error_detail)) : null,
    h('div', { class: 'prompt' }, job.params?.prompt || ''),
    h('div', { class: 'row' },
      h('button', { class: 'btn btn-sm', onclick: () => { reuse(job.params); job.dismissed = true; renderJobCards(); } },
        icon('redo', 'icon-sm'), 'Reuse settings')),
  );
}

function imageCard(img) {
  return h('div', { class: 'card' },
    h('img', {
      src: img.url, alt: img.params?.prompt || 'Generated image', loading: 'lazy',
      width: img.width || undefined, height: img.height || undefined,
      onclick: () => { S.lightbox = img; render(); },
    }),
    h('div', { class: 'card-foot' },
      h('span', { class: 'mono', title: 'Seed' }, String(img.seed ?? '')),
      h('div', { class: 'spacer' }),
      h('span', { class: 'faint' }, img.width && img.height ? `${img.width}×${img.height}` : '')));
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
      h('button', { class: 'btn', onclick: () => { reuse(p); close(); } }, icon('redo', 'icon-sm'), 'Reuse settings'),
      h('button', { class: 'btn', onclick: () => { reuse(p, true); close(); } }, icon('dice', 'icon-sm'), 'Vary seed'),
      h('button', { class: 'btn btn-ghost btn-danger', onclick: () => delImage(img.id) }, icon('trash', 'icon-sm'), 'Delete'),
      h('button', { class: 'btn btn-ghost', onclick: close }, icon('x', 'icon-sm'), 'Close')),
    h('img', { src: img.url, alt: p.prompt || 'Generated image' }),
    h('div', { class: 'lightbox-meta' },
      meta('Prompt', p.prompt),
      meta('Negative', p.negative),
      meta('Seed', p.seed),
      meta('Size', img.width && img.height ? `${img.width} × ${img.height}` : p.aspect),
      meta('Steps', p.steps), meta('CFG', p.cfg),
      meta('Sampler', p.sampler), meta('Scheduler', p.scheduler),
      meta('Checkpoint', shortCkpt(p.checkpoint))));
}

// Every image carries its full parameter set, which is what makes this and
// "vary seed" possible at all -- and a reproducibility trail besides.
function reuse(params, newSeed = false) {
  if (!params) return;
  S.form = {
    prompt: params.prompt || '',
    negative: params.negative ?? null,
    aspect: params.aspect || null,
    seed: newSeed ? '' : String(params.seed ?? ''),
    steps: params.steps ?? null,
    cfg: params.cfg ?? null,
    sampler: params.sampler ?? null,
    scheduler: params.scheduler ?? null,
  };
  S.form.denoise = params.denoise ?? null;
  S.form.grow_mask = params.grow_mask ?? null;
  S.form.feathering = params.feathering ?? null;
  S.seedLocked = !newSeed && params.seed != null;
  S.advancedOpen = true; S.optionsOpen = true;
  // Jump to the mode this image came from, if it is still on offer -- but not
  // its source image or mask, which are a different job's inputs.
  const from = S.workflows.find(x => x.id === params.workflow_id);
  if (from && byKind(from.kind)) S.mode = from.kind;
  if (params.pad_left != null) {
    S.pads = { left: params.pad_left || 0, top: params.pad_top || 0,
               right: params.pad_right || 0, bottom: params.pad_bottom || 0 };
  }
  render();
  document.querySelector('.app-body')?.scrollTo({ top: 0, behavior: 'smooth' });
  focusPrompt();
}

/* ---------------------------------------------------------------- actions */
async function submit(e) {
  e.preventDefault();
  const w = workflow();
  if (!w || S.submitting) return;
  const prompt = S.form.prompt.trim();
  if (!prompt) { focusPrompt(); return; }

  const needsImage = (w.requires || []).includes('image');
  const needsMask = (w.requires || []).includes('mask');
  if (needsImage && !S.upload) { toast('Add an image first.', true); return; }
  if (needsMask && !S.mask.strokes.length) {
    toast('Brush over the area you want changed.', true); return;
  }

  const body = { workflow_id: w.id, prompt };
  const f = S.form;
  if (f.negative != null) body.negative = f.negative;
  if (w.inputs.includes('aspect')) body.aspect = f.aspect || w.defaults.aspect;
  if (f.steps != null) body.steps = f.steps;
  if (f.cfg != null) body.cfg = f.cfg;
  if (f.sampler) body.sampler = f.sampler;
  if (f.scheduler) body.scheduler = f.scheduler;
  if (S.seedLocked && f.seed !== '') body.seed = parseInt(f.seed, 10);
  if (w.kind === 'img2img') body.denoise = f.denoise ?? w.defaults.denoise;
  if (w.inputs.includes('grow_mask')) body.grow_mask = f.grow_mask ?? w.defaults.grow_mask;
  if (w.inputs.includes('feathering')) body.feathering = f.feathering ?? w.defaults.feathering;
  if (needsImage) body.image_id = S.upload.id;
  if (w.kind === 'outpaint') {
    Object.assign(body, {
      pad_left: S.pads.left, pad_top: S.pads.top,
      pad_right: S.pads.right, pad_bottom: S.pads.bottom,
    });
    if (!S.pads.left && !S.pads.top && !S.pads.right && !S.pads.bottom) {
      toast('Choose at least one direction to extend into.', true); return;
    }
  }

  S.submitting = true;
  // The mask goes up before the job does: a mask the server rejects should
  // stop the submit, not fail a generation that already holds the GPU.
  if (needsMask) {
    try {
      const blob = await exportMask();
      const fd = new FormData();
      fd.append('file', blob, 'mask.png');
      const r = await fetch(`/api/uploads/${S.upload.id}/mask`, { method: 'POST', body: fd });
      if (!r.ok) throw await err(r);
      S.upload.hasMask = true;
    } catch (e2) {
      S.submitting = false;
      toast(e2.message, true); render(); return;
    }
  }

  // Optimistic: a placeholder card exists before the round trip returns, so
  // the click always has an immediate consequence.
  const temp = {
    id: 'local-' + Math.random().toString(36).slice(2),
    status: 'queued', position: null, params: { ...body }, progress: { value: 0, max: 0 },
    created_at: Date.now() / 1000,
  };
  S.jobs.unshift(temp);
  renderJobCards();

  try {
    const r = await api.post('/api/generate', body);
    const i = S.jobs.findIndex(j => j.id === temp.id);
    const real = { ...temp, id: r.job_id, position: r.position, params: r.params };
    if (i >= 0) S.jobs[i] = real; else S.jobs.unshift(real);
    if (!S.seedLocked) S.form.seed = '';
  } catch (e2) {
    S.jobs = S.jobs.filter(j => j.id !== temp.id);
    toast(e2.message, true);
  } finally {
    S.submitting = false;
    render();
  }
}

async function cancelJob(id) {
  if (id.startsWith('local-')) return;   // not yet acknowledged by the server
  try { await api.post(`/api/jobs/${id}/cancel`); }
  catch (e) { toast(e.message, true); }
}

async function delImage(id) {
  try {
    await api.del('/api/images/' + id);
    S.images = S.images.filter(i => i.id !== id);
    S.lightbox = null;
    render();
  } catch (e) { toast(e.message, true); }
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

  const wf = await api.get('/api/workflows');
  S.workflows = wf.workflows;
  S.resident = wf.resident_checkpoint;
  S.wf = S.workflows[0]?.id || null;
  S.mode = byKind('txt2img') ? 'txt2img' : (S.workflows[0]?.kind || 'txt2img');
  render();
  focusPrompt();

  await recoverState();
  connectEvents();

  // The elapsed clock on a running card has to tick on its own; nothing else
  // pushes an event between sampler steps.
  setInterval(() => { if (activeJobs().some(j => j.started_at)) renderJobCards(); }, 1000);
}

boot();
