"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("telegram_miniapp/web/audio-worklet.js", "utf8");
const sent = [];
let Worklet;
class FakeAudioWorkletProcessor {
  constructor() {
    this.port = {onmessage: null, postMessage(packet) {sent.push(packet);}};
  }
}
const context = {
  sampleRate: 48000,
  AudioWorkletProcessor: FakeAudioWorkletProcessor,
  registerProcessor(name, klass) {assert.equal(name, "hermes-mic"); Worklet = klass;},
  Int16Array, Math, Boolean
};
vm.runInNewContext(source, context, {filename: "audio-worklet.js"});
const processor = new Worklet();
assert.ok(processor);
for (let i=0;i<15;i++) {
  processor.process([[new Float32Array(128).fill(0.25)]]);
}
assert.equal(sent.length, 1, "40 ms packet at 16k output");
assert.equal(sent[0].byteLength, 1280);
const pcm = new Int16Array(sent[0]);
assert.ok(pcm.every(v => v > 7000 && v < 9000), "scaled PCM amplitude");
processor.port.onmessage({data:{type:"enabled",value:false}});
for(let i=0;i<15;i++) processor.process([[new Float32Array(128).fill(1)]]);
assert.equal(sent.length, 1, "muted microphone must not emit packets");
processor.port.onmessage({data:{type:"enabled",value:true}});
for(let i=0;i<15;i++) processor.process([[new Float32Array(128).fill(-0.25)]]);
assert.equal(sent.length, 2);
assert.ok(new Int16Array(sent[1]).every(v => v < -7000 && v > -9000));
console.log("AudioWorklet: PCM16 resampling, 40ms frame and mute checks PASS");
