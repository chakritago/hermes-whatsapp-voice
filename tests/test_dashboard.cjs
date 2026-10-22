"use strict";
/* Offline dashboard boot smoke test; no Telegram login, audio permissions or network. */
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const html = fs.readFileSync("telegram_miniapp/web/index.html", "utf8");
const source = fs.readFileSync("telegram_miniapp/web/app.js", "utf8");
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(x => x[1]);
assert.equal(new Set(ids).size, ids.length, "duplicate HTML IDs");
for (const match of source.matchAll(/byId\("([^"]+)"\)/g)) {
  assert.ok(ids.includes(match[1]), "missing HTML id: " + match[1]);
}
const elements = new Map(ids.map(id => [id, {
  id, textContent:"", disabled:false, hidden:false, dataset:{}, value:"default",
  classList:{add(){},remove(){},toggle(){}}, style:{},
  setAttribute(){}, addEventListener(){}, querySelector(){return null;},
  getContext(){return {clearRect(){},createLinearGradient(){return {addColorStop(){}};},
                        beginPath(){},moveTo(){},lineTo(){},stroke(){},fillRect(){}}},
  get options(){return [{value:"default"}];}, replaceChildren(){}, add(){}
}]));
const document = {
  getElementById(id){return elements.get(id);},
  documentElement:{removeAttribute(){},dataset:{}}
};
const sandbox = {
  window:{Telegram:null,matchMedia(){return {matches:false};},addEventListener(){}},
  document,
  navigator:{},
  location:{host:"voice.example.org"},
  localStorage:{getItem(){return null;},setItem(){}},
  clearInterval(){},clearTimeout(){},setInterval(){},setTimeout(){},
  cancelAnimationFrame(){},requestAnimationFrame(){},
  performance:{now(){return 0;}},
  WebSocket:{OPEN:1},
  URL, Map, Math, ArrayBuffer, DataView,
  Uint8Array, Float32Array, Int16Array,
};
sandbox.window.HermesAdaptiveBuffer = require("../telegram_miniapp/web/audio-buffer.js").AdaptiveAudioBuffer;
vm.runInNewContext(source, sandbox, {filename:"app.js"});
assert.equal(elements.get("start").disabled,true,"not in Telegram must disable start");
assert.equal(elements.get("approvalPanel").hidden,false,"HTML hidden attribute requires CSS");
assert.ok(html.includes(".approval[hidden]{display:none}"),"approval must be hidden by default");
console.log("Dashboard: DOM contract and safe unauthenticated boot PASS");
