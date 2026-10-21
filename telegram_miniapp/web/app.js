"use strict";

// Telegram-only. The server verifies signed initData on *every* reconnect.
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const byId = id => document.getElementById(id);
const start = byId("start"), end = byId("end"), mute = byId("mute");
const status = byId("status"), statusText = byId("statusText");
const description = byId("description"), transcript = byId("transcript");
const orb = byId("orb"), outputSelect = byId("outputSelect"), outputRow = byId("outputRow");
const visualizer = byId("visualizer"), canvas = byId("waveform");
const reconnectInfo = byId("reconnectInfo");
const speakerAudio = byId("speakerAudio");
const ctx2d = canvas.getContext("2d");
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let socket = null, stream = null, audio = null, processor = null, source = null;
let analyser = null, silentGain = null, outputGain = null, outputStream = null;
let active = false, desired = false, muted = false, audioInitializing = false;
let playAt = 0, playing = [], reconnectCount = 0, reconnectTimer = null;
let resumeTicket = null, waveFrame = null, waveMode = "bars", connectionSequence = 0;
const MAX_BROWSER_RETRIES = 5;

function state(message, live = false, phase = "") {
  statusText.textContent = message;
  status.classList.toggle("live", live);
  orb.classList.toggle("active", live);
  status.dataset.phase = phase;
  reconnectInfo.textContent = phase === "reconnecting" ? message : "";
}
function append(role, text) {
  if (!text || !text.trim()) return;
  transcript.querySelector(".empty")?.remove();
  const node = document.createElement("div");
  node.className = "message " + (role === "user" ? "user" : "assistant");
  node.textContent = text;
  transcript.appendChild(node);
  transcript.scrollTop = transcript.scrollHeight;
  while (transcript.children.length > 40) transcript.firstChild.remove();
}
function cancelPlayback() {
  for (const node of playing) { try { node.stop(); } catch { /* ended */ } }
  playing = []; playAt = 0;
}
function playback(bytes) {
  if (!audio || !active || !outputGain || bytes.byteLength % 2) return;
  const pcm = new DataView(bytes);
  const buffer = audio.createBuffer(1, pcm.byteLength / 2, 24000);
  const channel = buffer.getChannelData(0);
  for (let n = 0; n < channel.length; n++) channel[n] = pcm.getInt16(n * 2, true) / 32768;
  const node = audio.createBufferSource();
  node.buffer = buffer;
  node.connect(outputGain);
  const when = Math.max(audio.currentTime + 0.025, playAt);
  node.start(when);
  playAt = when + buffer.duration;
  playing.push(node);
  node.onended = () => { playing = playing.filter(x => x !== node); };
}
function toPcm16(samples, sampleRate) {
  const ratio = sampleRate / 16000;
  const count = Math.floor(samples.length / ratio);
  const bytes = new ArrayBuffer(count * 2);
  const view = new DataView(bytes);
  for (let n = 0; n < count; n++) {
    const from = Math.floor(n * ratio);
    const to = Math.max(from + 1, Math.min(samples.length, Math.floor((n + 1) * ratio)));
    let sum = 0;
    for (let j = from; j < to; j++) sum += samples[j];
    const value = Math.max(-1, Math.min(1, sum / (to - from)));
    view.setInt16(n * 2, value < 0 ? value * 32768 : value * 32767, true);
  }
  return bytes;
}
async function refreshOutputs() {
  if (!("setSinkId" in speakerAudio) || !navigator.mediaDevices.enumerateDevices) {
    outputRow.hidden = true; return;
  }
  try {
    const options = await navigator.mediaDevices.enumerateDevices();
    const devices = options.filter(device => device.kind === "audiooutput");
    if (!devices.length) { outputRow.hidden = true; return; }
    const prior = outputSelect.value;
    outputSelect.replaceChildren();
    const automatic = new Option("ลำโพงเริ่มต้น", "default");
    outputSelect.add(automatic);
    for (const device of devices) {
      if (device.deviceId === "default") continue;
      outputSelect.add(new Option(device.label || "อุปกรณ์เสียง", device.deviceId));
    }
    if ([...outputSelect.options].some(option => option.value === prior)) outputSelect.value = prior;
    outputRow.hidden = false;
  } catch { outputRow.hidden = true; }
}
outputSelect.addEventListener("change", async () => {
  try {
    await speakerAudio.setSinkId(outputSelect.value);
    await speakerAudio.play();
    description.textContent = "เปลี่ยนอุปกรณ์เสียงแล้ว";
  } catch {
    description.textContent = "Telegram บนอุปกรณ์นี้ยังไม่อนุญาตให้เลือกลำโพง";
  }
});
async function initializeAudio() {
  if (audioInitializing || stream) return;
  audioInitializing = true;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
      video: false
    });
    audio = new (window.AudioContext || window.webkitAudioContext)();
    await audio.resume();
    source = audio.createMediaStreamSource(stream);
    analyser = audio.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    processor = audio.createScriptProcessor(4096, 1, 1);
    silentGain = audio.createGain(); silentGain.gain.value = 0;
    analyser.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(audio.destination);
    outputGain = audio.createGain();
    outputStream = audio.createMediaStreamDestination();
    // A media element supports setSinkId in eligible desktop browsers.
    if ("setSinkId" in speakerAudio) {
      outputGain.connect(outputStream);
      speakerAudio.srcObject = outputStream.stream;
      try { await speakerAudio.play(); }
      catch { outputGain.disconnect(); outputGain.connect(audio.destination); }
    } else {
      outputGain.connect(audio.destination);
    }
    processor.onaudioprocess = event => {
      if (!muted && desired && active && socket?.readyState === WebSocket.OPEN &&
          socket.bufferedAmount < 262144) {
        socket.send(toPcm16(event.inputBuffer.getChannelData(0), audio.sampleRate));
      }
    };
    stream.getAudioTracks().forEach(track => { track.enabled = false; });
    await refreshOutputs();
    drawWave();
  } finally { audioInitializing = false; }
}
function drawWave() {
  if (!ctx2d) return;
  cancelAnimationFrame(waveFrame);
  const samples = new Uint8Array(analyser?.fftSize || 256);
  const bars = 42, w = canvas.width, h = canvas.height;
  const frame = () => {
    ctx2d.clearRect(0, 0, w, h);
    if (analyser && active && !muted) analyser.getByteTimeDomainData(samples);
    else samples.fill(128);
    const gradient = ctx2d.createLinearGradient(0, 0, w, 0);
    gradient.addColorStop(0, "#548ce8");
    gradient.addColorStop(.5, "#75e0f9");
    gradient.addColorStop(1, "#4bdbab");
    ctx2d.strokeStyle = gradient; ctx2d.fillStyle = gradient;
    if (waveMode === "line") {
      ctx2d.beginPath(); ctx2d.lineWidth = 3;
      for (let i = 0; i < samples.length; i++) {
        const y = (samples[i] - 128) / 128 * h * .33 + h / 2;
        const x = i / (samples.length - 1) * w;
        if (i === 0) ctx2d.moveTo(x, y); else ctx2d.lineTo(x, y);
      }
      ctx2d.stroke();
    } else {
      const unit = w / bars;
      for (let n = 0; n < bars; n++) {
        const v = Math.abs((samples[Math.floor(n * samples.length / bars)] - 128) / 128);
        const height = Math.max(5, v * (h - 10) * 1.6);
        ctx2d.fillRect(n * unit + 2, (h - height) / 2, Math.max(2, unit - 4), height);
      }
    }
    if (desired && !prefersReducedMotion.matches) waveFrame = requestAnimationFrame(frame);
  };
  frame();
}
visualizer.addEventListener("click", () => {
  waveMode = waveMode === "bars" ? "line" : "bars";
  visualizer.setAttribute("aria-label", "แสดงคลื่นเสียงแบบ" + (waveMode === "bars" ? "แท่ง" : "เส้น") + " กดเพื่อเปลี่ยน");
  drawWave();
});
function syncMute() {
  for (const track of stream?.getAudioTracks() || []) track.enabled = desired && active && !muted;
  mute.textContent = muted ? "🔇 เปิดไมค์" : "🎙 ปิดไมค์";
  mute.setAttribute("aria-pressed", String(muted));
  mute.classList.toggle("muted", muted);
}
mute.addEventListener("click", () => { muted = !muted; syncMute(); });
async function disposeAudio() {
  cancelPlayback();
  cancelAnimationFrame(waveFrame); waveFrame = null;
  if (processor) { processor.onaudioprocess = null; processor.disconnect(); processor = null; }
  for (const node of [source, analyser, silentGain, outputGain, outputStream]) {
    try { node?.disconnect(); } catch { /* disconnected */ }
  }
  source = analyser = silentGain = outputGain = outputStream = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  speakerAudio.pause(); speakerAudio.srcObject = null;
  if (audio) await audio.close().catch(() => {});
  audio = null;
}
function scheduleReconnect() {
  active = false;
  syncMute();
  if (!desired) return;
  if (reconnectCount >= MAX_BROWSER_RETRIES) {
    state("เชื่อมต่อไม่สำเร็จ", false, "failed");
    description.textContent = "เชื่อมต่อใหม่ไม่สำเร็จ กรุณากดเริ่มสนทนา";
    stop(false);
    return;
  }
  reconnectCount++;
  const seconds = Math.min(16, 2 ** (reconnectCount - 1));
  state("กำลังเชื่อมต่อใหม่ (" + reconnectCount + "/" + MAX_BROWSER_RETRIES + ")", false, "reconnecting");
  reconnectTimer = setTimeout(connectSocket, seconds * 1000);
}
function connectSocket() {
  if (!desired) return;
  const generation = ++connectionSequence;
  const ws = new WebSocket("wss://" + location.host + "/ws");
  socket = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (socket === ws && desired) ws.send(JSON.stringify({
      type: "auth", init_data: tg.initData, resume_ticket: resumeTicket
    }));
  };
  ws.onmessage = async event => {
    if (socket !== ws || !desired) return;
    if (typeof event.data !== "string") { playback(event.data); return; }
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "ready") {
      try {
        // Microphone permission is requested in the original button press, never silently.
        if (!stream) throw new Error("no microphone permission");
        if (generation !== connectionSequence || !desired) return;
        resumeTicket = msg.resume_ticket || null;
        reconnectCount = 0; active = true; syncMute();
        start.disabled = true; end.disabled = false; mute.disabled = false;
        state(msg.restored ? "กู้คืนการสนทนาสำเร็จ" : "กำลังสนทนา", true, "connected");
        description.textContent = "พูดได้เลย Hermes กำลังฟังคุณอยู่";
        drawWave();
      } catch {
        description.textContent = "ไม่สามารถเปิดไมโครโฟนได้";
        stop();
      }
    } else if (msg.type === "status") {
      state(msg.message || "กำลังเชื่อมต่อ...", msg.phase === "restored", msg.phase);
    } else if (msg.type === "interrupted") {
      cancelPlayback();
    } else if (msg.type === "transcript") {
      append(msg.role, msg.text);
    } else if (msg.type === "notice") {
      description.textContent = msg.message;
    } else if (msg.type === "error") {
      description.textContent = msg.message || "ไม่สามารถเชื่อมต่อได้";
      // Authentication or competing session errors must not retry indefinitely.
      if (/login|expired|Another voice session/.test(msg.message || "")) stop(false);
    }
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    socket = null;
    scheduleReconnect();
  };
  ws.onerror = () => { state("การเชื่อมต่อขัดข้อง", false, "reconnecting"); };
}
async function stop(explicit = true) {
  desired = false; active = false; ++connectionSequence;
  clearTimeout(reconnectTimer); reconnectTimer = null;
  syncMute();
  const old = socket; socket = null;
  if (old?.readyState === WebSocket.OPEN && explicit) old.send(JSON.stringify({type: "stop"}));
  if (old && old.readyState <= WebSocket.OPEN) old.close();
  if (explicit) resumeTicket = null;
  await disposeAudio();
  start.disabled = !(tg?.initData && window.isSecureContext);
  end.disabled = true; mute.disabled = true;
  state(explicit ? "สนทนาสิ้นสุด" : "หยุดการเชื่อมต่อ", false);
  if (explicit) description.textContent = "คุณสามารถเริ่มสนทนาใหม่ได้ทุกเมื่อ";
}
start.addEventListener("click", async () => {
  if (!tg?.initData || !window.isSecureContext || desired) return;
  desired = true; reconnectCount = 0; resumeTicket = null;
  start.disabled = true; end.disabled = false; mute.disabled = false;
  state("ขออนุญาตไมโครโฟน...", false, "connecting");
  try {
    await initializeAudio();
    if (!desired) return;
    state("กำลังเชื่อมต่อ Hermes...", false, "connecting");
    connectSocket();
  } catch {
    await stop(false);
    description.textContent = "โปรดอนุญาตไมโครโฟนใน Telegram แล้วลองอีกครั้ง";
  }
});
end.addEventListener("click", () => { stop(true); });
window.addEventListener("pagehide", () => { stop(true); });
if (!tg?.initData || !window.isSecureContext) {
  start.disabled = true;
  description.textContent = "เปิดจากเมนู Telegram Bot ผ่าน HTTPS เพื่อเริ่มสนทนา";
  state("ต้องเปิดผ่าน Telegram", false);
}
