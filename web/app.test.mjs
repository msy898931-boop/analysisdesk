// Unit tests for the browser-side helpers, run by `node --test`.
//
// The card is half the product and none of the Python tests touch it. Two of
// the defects found in the last pass lived here (an undefined `Response.status`
// reference and a precision rule that disagreed with the server), so the pure
// helpers are now a module with a test that actually exercises them.
import test from "node:test";
import assert from "node:assert/strict";

import { esc, fmt, priceDecimals, basisPct, signedPct } from "./lib.js";

test("esc neutralises every character that can open a tag or attribute", () => {
  assert.equal(esc("<script>alert(1)</script>"),
               "&lt;script&gt;alert(1)&lt;/script&gt;");
  assert.equal(esc('a" onmouseover="x'), "a&quot; onmouseover=&quot;x");
  assert.equal(esc("it's"), "it&#39;s");
  assert.equal(esc("a & b"), "a &amp; b");
});

test("esc never lets a raw angle bracket through", () => {
  for (const payload of ["<img src=x onerror=alert(1)>", "<<>>", "<svg/onload=x>"]) {
    const out = esc(payload);
    assert.ok(!out.includes("<") && !out.includes(">"), `leaked markup: ${out}`);
  }
});

test("esc renders null and undefined as empty, not the word", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(0), "0");
});

test("priceDecimals scales with magnitude, matching the server", () => {
  assert.equal(priceDecimals(3982.69), 2);   // gold
  assert.equal(priceDecimals(123.4), 3);
  assert.equal(priceDecimals(57.2346), 4);
  assert.equal(priceDecimals(1.11857), 5);   // EUR/USD
  assert.equal(priceDecimals(-3982.69), 2);  // sign does not matter
});

test("fmt shows the same decimals the server would", () => {
  // The exact drift that was fixed: the card said 57.235, the API said 57.2346.
  assert.equal(fmt(57.2346), "57.2346");
  assert.equal(fmt(3982.69), "3,982.69");
  assert.equal(fmt(1.11857), "1.11857");
});

test("fmt shows a dash for a missing value, never NaN", () => {
  assert.equal(fmt(null), "—");
  assert.equal(fmt(undefined), "—");
  assert.equal(fmt(NaN), "—");
  assert.equal(fmt("not a number"), "—");
});

test("basisPct is null when there is no quote to compare against", () => {
  assert.equal(basisPct(4000, null), null);
  assert.equal(basisPct(4000, 0), null);
});

test("basisPct is signed and correct", () => {
  assert.equal(basisPct(101, 100), 1);
  assert.equal(basisPct(99, 100), -1);
  assert.equal(signedPct(basisPct(101, 100)), "+1.00%");
  assert.equal(signedPct(basisPct(99, 100)), "-1.00%");
});
