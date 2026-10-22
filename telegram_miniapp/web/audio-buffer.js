"use strict";
/* Pure adaptive jitter-buffer controller; no device/browser privileges. */
(function (root) {
  class AdaptiveAudioBuffer {
    constructor() {
      this.rtt = 120;
      this.jitter = 0;
      this.underruns = 0;
      this.drops = 0;
    }
    observeRtt(ms) {
      if (!Number.isFinite(ms) || ms < 0 || ms > 30000) return;
      const delta = Math.abs(ms - this.rtt);
      this.jitter = this.jitter * 0.8 + delta * 0.2;
      this.rtt = this.rtt * 0.8 + ms * 0.2;
    }
    delaySeconds() {
      return Math.max(0.045, Math.min(0.25, (this.jitter * 1.5 + this.rtt * 0.13 + 35) / 1000));
    }
    nextTime(now, playAt, duration) {
      const wanted = this.delaySeconds();
      const wasUnderrun = playAt > 0 && playAt < now - 0.015;
      if (wasUnderrun) this.underruns++;
      // Drop stale playback (>1 second backlog), do not repeat delayed speech.
      if (playAt > now + 1.0) {
        this.drops++;
        return {drop: true, when: playAt};
      }
      return {drop: false, when: Math.max(now + wanted, playAt, now + 0.01)};
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {AdaptiveAudioBuffer};
  else root.HermesAdaptiveBuffer = AdaptiveAudioBuffer;
})(typeof window !== "undefined" ? window : globalThis);
