// Guards the DOM wiring in app.js, which the pure-helper tests never touched.
//
// The defect this file exists for: app.js is a module, so it runs in strict
// mode, and it referred to its elements by bare name (window's named access).
// `status` is a reserved window property -- the status-bar string -- so a bare
// `status` was a string, `status.textContent=...` threw a TypeError before the
// try block, and the analyze button stayed disabled with nothing shown. `sym`
// and `iv` did not resolve at all. No test caught it because the suite only
// exercised lib.js.
//
// This runs app.js for real in a sandbox with a minimal DOM, no browser, and
// asserts that a click renders a card. The stub is the environment, not the
// code under test: app.js runs unmodified.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));

function makeEl(id = "") {
  const el = {
    id, innerHTML: "", value: "", className: "", disabled: false,
    children: [], attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = v; },
    getContext() { return new Proxy({}, { get: () => () => {} }); },
    getBoundingClientRect() { return { width: 300, height: 150 }; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._t || ""; }, set(v) { this._t = String(v); },
  });
  return el;
}

const SYMBOLS = {
  symbols: [{ symbol: "EURUSD=X", name_ar: "يورو/دولار", name_en: "EUR/USD", class: "forex" }],
  intervals: { "1h": { ar: "ساعة", en: "hour" } },
};
const READ = {
  symbol: "EURUSD=X", interval: "1h",
  indicators: { price: 1.12, support: 1.11, resistance: 1.13, change_pct: 0.1,
                rsi: 55, macd: 0.001, atr_pct: 0.1, ema200: 1.115,
                boll_lo: 1.11, boll_hi: 1.13, stoch: 50 },
  signal: { direction: "BUY", confidence: 60, score: 2, rules: [], warnings: [] },
  candles: [{ t: 1, o: 1, h: 1, l: 1, c: 1 }],
  narrative: { source: "deterministic", text: "قراءة محسوبة", model: null, note: null },
  instrument: { kind: "spot" }, interval_label: "1h",
};

// A spot-gold read: the charted token was rescaled onto the broker quote, so the
// card must say so. A silent rescale is the same hidden substitution the desk
// refuses everywhere else, so the anchoring is asserted, not assumed.
const GOLD = {
  ...READ, symbol: "XAUUSD",
  instrument: { kind: "spot", name: "Gold Spot (XAU/USD)", exchange: "Kraken PAXG/USD", provider: "Kraken PAXG/USD" },
  spot: { metal: "XAU", price: 3910.08, source: "Swissquote" },
  anchor: { ok: true, factor: 0.99817, pct: -0.183, source: "Swissquote" },
};

// Load app.js in a sandbox whose global is a small window. A bare name that is
// not a binding and not on this object throws a ReferenceError -- exactly what
// a browser does for `sym`, and the TypeError it did for `status`.
async function boot({ fetchImpl, read } = {}) {
  const html = await readFile(join(here, "index.html"), "utf8");
  const ids = [...html.matchAll(/id="([\w-]+)"/g)].map(m => m[1]);
  const els = Object.fromEntries(ids.map(id => [id, makeEl(id)]));
  const document = {
    documentElement: { lang: "", dir: "" },
    getElementById: id => els[id] ?? null,
    createElement: () => makeEl(),
  };
  const lib = await import("./lib.js");
  const src = (await readFile(join(here, "app.js"), "utf8"))
    .replace(/^import .*?;$/m, "");
  const readBody = read || READ;
  const win = {
    document, els,
    fetch: fetchImpl || (async (u) => ({
      ok: true, status: 200,
      json: async () => (String(u).includes("symbols") ? SYMBOLS : readBody),
    })),
    setTimeout, clearTimeout, console,
    esc: lib.esc, fmt: lib.fmt, basisPct: lib.basisPct, signedPct: lib.signedPct,
  };
  win.window = win;
  vm.runInNewContext(src, win, { filename: "app.js" });
  // boot() is async and kicked off at load; let its microtasks drain.
  await new Promise(r => setTimeout(r, 0));
  return { els, win };
}

test("every element app.js binds exists in index.html", async () => {
  const html = await readFile(join(here, "index.html"), "utf8");
  const src = await readFile(join(here, "app.js"), "utf8");
  const ids = new Set([...html.matchAll(/id="([\w-]+)"/g)].map(m => m[1]));
  const bound = [...src.matchAll(/\$\("([\w-]+)"\)/g)].map(m => m[1]);
  assert.ok(bound.length >= 8, "expected the module to bind its elements");
  assert.deepEqual(bound.filter(id => !ids.has(id)), [],
                   "app.js binds ids that are absent from index.html");
});

test("a click renders a card instead of dying on a bare element name", async () => {
  const { els } = await boot();
  // boot() runs once on load; a second click is the user pressing Analyze.
  els.go.onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.match(els.out.innerHTML, /قراءة السوق/, "the panel should render the read");
  assert.doesNotMatch(els.out.innerHTML, /تعذّر قراءة السوق/, "not the error card");
  assert.equal(els.go.disabled, false, "the button must be re-enabled");
  assert.equal(els.status.textContent, "", "the loading note must be cleared");
});

test("a failed read shows the error in the panel, not in a silent button", async () => {
  const { els } = await boot({
    fetchImpl: async (u) => String(u).includes("symbols")
      ? { ok: true, status: 200, json: async () => SYMBOLS }
      : { ok: false, status: 502, json: async () => ({ detail: "feed unreachable" }) },
  });
  els.go.onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.match(els.out.innerHTML, /feed unreachable/);
  assert.equal(els.go.disabled, false, "the button must not stay stuck");
});

test("the card discloses that a proxy was rescaled onto the live spot", async () => {
  // The whole point of anchoring: the levels are the broker's, and the card says
  // the chart was shifted to get there. Silence here would be a hidden
  // substitution -- exactly what this desk refuses.
  const { els } = await boot({ read: GOLD });
  els.go.onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.match(els.out.innerHTML, /الإرساء على السبوت/);
  assert.match(els.out.innerHTML, /-0\.18%/);          // the shift, signed
  assert.doesNotMatch(els.out.innerHTML, /غير مُرسّى/);  // not the unanchored note
});

test("the footer names the venue that actually supplied the candles", async () => {
  // A fixed "via Yahoo Finance" was a label that lied on spot gold, which comes
  // from Kraken. The footer must name the real source, like every other figure.
  const { els } = await boot({ read: GOLD });
  els.go.onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.match(els.footline.textContent, /Kraken PAXG\/USD/);
  assert.doesNotMatch(els.footline.textContent, /Yahoo/);
});
