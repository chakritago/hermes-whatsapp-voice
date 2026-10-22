"use strict";

/**
 * AudioWorklet microphone capture: streaming average-resampled mono PCM16 @16kHz.
 * Bounded ~40ms frames; avoids ScriptProcessor callbacks on the UI thread.
 * No microphone data is persisted and stop/mute messages immediately discard buffers.
 */
class HermesMicrophoneProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.buffer = new Int16Array(640);
    this.used = 0;
    this.enabled = true;
    this.port.onmessage = event => {
      if (event.data?.type === "enabled") {
        this.enabled = Boolean(event.data.value);
        if (!this.enabled) this.reset();
      } else if (event.data?.type === "reset") {
        this.reset();
      }
    };
  }
  reset() {
    this.used = 0;
    this.phase = 0;
    this.count = 0;
    this.sum = 0;
  }
  flush() {
    const packet = this.buffer.buffer;
    this.port.postMessage(packet, [packet]);
    this.buffer = new Int16Array(640);
    this.used = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples || !this.enabled) return true;
    for (let i = 0; i < samples.length; i++) {
      this.sum += samples[i];
      this.count++;
      this.phase++;
      if (this.phase >= this.ratio) {
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.buffer[this.used++] = value < 0 ? value * 32768 : value * 32767;
        this.sum = 0;
        this.count = 0;
        this.phase -= this.ratio;
        if (this.used === this.buffer.length) this.flush();
      }
    }
    return true;
  }
}
registerProcessor("hermes-mic", HermesMicrophoneProcessor);
