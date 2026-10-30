// Pure helpers with no DOM access, so the browser page and node's test runner
// can share one copy. Anything that touches `document` stays in app.js; these
// are the parts a test can actually pin down, and the parts that were wrong
// before (escaping, precision) are exactly the ones worth pinning.

// Precision mirrors indicators.price_decimals on the server, so the card and
// the API agree on how many decimals a number of this size deserves. They
// drifted before: the server showed 57.2346 while the card showed 57.235.
export function priceDecimals(v) {
  const a = Math.abs(Number(v));
  if (a >= 1000) return 2;
  if (a >= 100) return 3;
  if (a >= 10) return 4;
  return 5;
}

export function fmt(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const p = priceDecimals(n);
  return Number(n).toLocaleString("en-US", {
    minimumFractionDigits: p,
    maximumFractionDigits: p,
  });
}

// Every server-supplied string goes through here before it is placed in the
// template. The panel is built with innerHTML, so an unescaped value from the
// API -- the model's prose above all -- would be parsed as markup. The number
// guard on the server checks figures, not tags; escaping is what stops a tag.
export function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// The gap between the charted market and the live broker quote, in percent.
// On a futures contract this is the basis; on the spot instrument it is how
// closely the charted token tracks the quote beside it.
export function basisPct(price, spot) {
  if (!spot) return null;
  return ((price - spot) / spot) * 100;
}

export function signedPct(n, digits = 2) {
  return (n >= 0 ? "+" : "") + n.toFixed(digits) + "%";
}
