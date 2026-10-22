"use strict";
/* Static WebView accessibility contract, NOT a physical Telegram TalkBack test. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const html = fs.readFileSync("telegram_miniapp/web/index.html", "utf8");
const js = fs.readFileSync("telegram_miniapp/web/app.js", "utf8");
const required = [
  "auditSearch", "auditRiskFilter", "auditDecisionFilter", "healthPeriod",
  "healthComponent", "privacyResume", "privacyClear", "audioHealth",
  "approveAction", "denyAction", "fontToggle", "contrastToggle",
  "compactToggle", "micSelect", "outputSelect", "resumeSession",
];
for (const id of required) {
  assert.ok(html.includes('id="' + id + '"'), "Missing visible control " + id);
}
assert.match(html, /role="alertdialog"[^>]*aria-modal="true"/);
assert.match(html, /@media\(prefers-reduced-motion:reduce\)/);
assert.match(html, /:focus-visible/);
assert.match(html, /min-height:44px/);
assert.match(js, /denyButton\.focus\?\.\(\)/);
assert.match(js, /event\.key === "Escape"/);
assert.match(js, /privacyToggle\.checked/);
assert.match(js, /sessionStorage\.removeItem\(SESSION_KEY\)/);
assert.match(js, /source\?\.disconnect\(\)/);
const sourceChange = js.indexOf("const newSource = audio.createMediaStreamSource(next);");
const connectBeforeDisconnect = js.slice(sourceChange, sourceChange + 300);
assert.ok(connectBeforeDisconnect.indexOf("newSource.connect(analyser)") <
  connectBeforeDisconnect.indexOf("source?.disconnect()"), "Mic handoff must connect before disconnect");
console.log("WebView static QA: labels, keyboard deny, focus, reduced motion, audio hot-swap and privacy PASS");
