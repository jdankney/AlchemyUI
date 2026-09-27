// Executes a real SPA script against a real server, in node, with just enough
// DOM to boot. Prints one JSON line. A script that is syntactically valid but
// throws at load (v1's `const f = w =`) fails here; `node --check` never can.
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [BASE, SCRIPT, HASH = ''] = process.argv.slice(2);
const byId = new Map();
const requests = [];
let error = null;

const ctx2d = () => new Proxy({}, { get: () => () => ({}) });
function el(tag) {
  const t = {
    nodeType: 1, tagName: String(tag).toUpperCase(), childNodes: [], style: {}, dataset: {},
    className: '', id: '', value: '', textContent: '', innerHTML: '', checked: false,
    files: [], width: 512, height: 512, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute(k, v) { if (k === 'id') { t.id = v; byId.set(v, p); } },
    getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    append(...k) { t.childNodes.push(...k); }, appendChild(k) { t.childNodes.push(k); return k; },
    insertBefore(k) { t.childNodes.push(k); return k; },
    replaceChildren(...k) { t.childNodes = k.filter(x => x != null && x !== false); },
    remove() {}, replaceWith() {}, focus() {}, blur() {}, click() {}, scrollIntoView() {},
    setPointerCapture() {}, releasePointerCapture() {}, select() {}, setSelectionRange() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0 }),
    querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false,
    getContext: () => ctx2d(), toBlob(cb) { cb(new Blob([])); },
  };
  const p = new Proxy(t, { get: (o, k) => o[k], set: (o, k, v) => (o[k] = v, true) });
  return p;
}
const APP = el('div');
globalThis.document = {
  getElementById: id => (id === 'app' ? APP : (byId.get(id) || null)),
  createElement: el, createElementNS: (_ns, tag) => el(tag),
  createTextNode: s => ({ nodeType: 3, textContent: String(s) }),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  body: el('body'), documentElement: el('html'), head: el('head'),
  activeElement: null, hidden: false, visibilityState: 'visible', title: '',
};
globalThis.window = globalThis;
globalThis.location = { href: BASE + '/' + HASH, origin: BASE, pathname: '/', search: '', hash: HASH, reload() {} };
globalThis.innerWidth = 1280;
globalThis.innerHeight = 800;
globalThis.history = { replaceState() {}, pushState() {} };
Object.defineProperty(globalThis, 'navigator', {
  value: { maxTouchPoints: 0, userAgent: 'node', clipboard: { writeText: async () => {} } },
  configurable: true,
});
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} });
const store = new Map();
globalThis.localStorage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = f => setTimeout(f, 0);
globalThis.cancelAnimationFrame = () => {};
globalThis.EventSource = class { constructor(u) { requests.push(u); } addEventListener() {} close() {} };
globalThis.Image = class { set src(v) { this._s = v; } };
globalThis.FileReader = class { readAsDataURL() {} };
globalThis.URL.createObjectURL = () => 'blob:fake';
globalThis.URL.revokeObjectURL = () => {};
globalThis.scrollTo = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
const realFetch = globalThis.fetch;
globalThis.fetch = (u, o) => { requests.push(String(u)); return realFetch(String(u).startsWith('http') ? u : BASE + u, o); };

const done = () => {
  console.log(JSON.stringify({ error, rendered: APP.childNodes.length, requests }));
  process.exit(0);
};
process.on('unhandledRejection', e => { error = String(e && e.stack || e); done(); });
process.on('uncaughtException', e => { error = String(e && e.stack || e); done(); });

// Copied to .mjs so node evaluates it as a module, in strict mode -- the same
// semantics <script type="module"> gives it in a browser.
const tmp = join(tmpdir(), `alchemyui-boot-${process.pid}.mjs`);
writeFileSync(tmp, readFileSync(SCRIPT));
try { await import(pathToFileURL(tmp).href); } finally { rmSync(tmp, { force: true }); }
setTimeout(done, 3000);
