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
const hermesCanvas = byId("hermesWaveform"), hermesStatus = byId("hermesStatus");
const userStatus = byId("userStatus"), preflightButton = byId("preflight");
const preflightResult = byId("preflightResult"), themeButton = byId("themeToggle");
const diagRtt = byId("diagRtt"), diagBuffer = byId("diagBuffer"), diagMic = byId("diagMic");
const diagQuality = byId("diagQuality"), diagDrop = byId("diagDrop"), approvalPanel = byId("approvalPanel");
const approvalText = byId("approvalText"), approveButton = byId("approveAction");
const denyButton = byId("denyAction"), moodText = byId("moodText");
const reconnectInfo = byId("reconnectInfo");
const speakerAudio = byId("speakerAudio");
const ctx2d = canvas.getContext("2d");
const assistantCtx = hermesCanvas.getContext("2d");
let playbackAnalyser = null, workletNode = null, preflighting = false;
const themes = ["auto", "dark", "light"];
let currentTheme = "auto";
try { const stored = localStorage.getItem("hermes.voice.theme");
  if (themes.includes(stored)) currentTheme = stored; } catch { /* private browser */ }
function applyTheme() {
  if (currentTheme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = currentTheme;
  themeButton.textContent = currentTheme === "auto" ? "◐ อัตโนมัติ" : currentTheme === "dark" ? "☾ มืด" : "☀ สว่าง";
  themeButton.setAttribute("aria-label", "ธีม: " + themeButton.textContent + " กดเพื่อสลับ");
}
themeButton.addEventListener("click", () => {
  currentTheme = themes[(themes.indexOf(currentTheme) + 1) % themes.length];
  try { localStorage.setItem("hermes.voice.theme", currentTheme); } catch { /* private browser */ }
  applyTheme();
});
applyTheme();
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let socket = null, stream = null, audio = null, processor = null, source = null;
let analyser = null, silentGain = null, outputGain = null, outputStream = null;
let active = false, desired = false, muted = false, audioInitializing = false;
let playAt = 0, playing = [], reconnectCount = 0, reconnectTimer = null;
let resumeTicket = null, waveFrame = null, waveMode = "bars", connectionSequence = 0;
let readyAt = 0, diagnosticsTimer = null, nextPing = 0, currentApproval = null;
let recoveringMic = false, lastMicLevel = 0;
const bufferController = new window.HermesAdaptiveBuffer();
function clearApproval() {
  currentApproval = null;
  approvalPanel.hidden = true;
  approveButton.disabled = denyButton.disabled = false;
}
function respondApproval(allow) {
  if (!currentApproval) return;
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({
    type: "approval", id: currentApproval, allow
  }));
  clearApproval();
  if (!allow) description.textContent = "ปฏิเสธคำสั่ง Hermes แล้ว";
}
approveButton.addEventListener("click", () => respondApproval(true));
denyButton.addEventListener("click", () => respondApproval(false));
function startDiagnostics() {
  clearInterval(diagnosticsTimer);
  diagnosticsTimer = setInterval(() => {
    if (!desired || !active || socket?.readyState !== WebSocket.OPEN) return;
    const id = (++nextPing) % 2147483647;
    if (pendingPings.size > 4) pendingPings.clear();
    pendingPings.set(id, performance.now());
    socket.send(JSON.stringify({type:"ping", id}));
    diagRtt.textContent = Math.round(bufferController.rtt) + " ms";
    diagBuffer.textContent = Math.round(Math.max(0, playAt - (audio?.currentTime || 0)) * 1000) + " ms";
    diagMic.textContent = Math.round(lastMicLevel) + "%";
    diagQuality.textContent = bufferController.rtt < 200 ? "ดี" :
      bufferController.rtt < 600 ? "ปานกลาง" : "เครือข่ายช้า";
    diagDrop.textContent = String(bufferController.underruns + bufferController.drops);
  }, 1500);
}
const pendingPings = new Map();
async function recoverAudioDevice() {
  if (!active || !desired || recoveringMic || !audio) return;
  const tracks = stream?.getAudioTracks() || [];
  const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
  const current = tracks[0];
  const missing = !current || current.readyState === "ended" ||
    (current.getSettings()?.deviceId &&
     !devices.some(d => d.kind === "audioinput" && d.deviceId === current.getSettings().deviceId));
  if (missing) {
    recoveringMic = true;
    try {
      const next = await navigator.mediaDevices.getUserMedia({audio:{
        echoCancellation:true, noiseSuppression:true, autoGainControl:true
      }, video:false});
      const newSource = audio.createMediaStreamSource(next);
      source?.disconnect();
      newSource.connect(analyser);
      stream?.getTracks().forEach(t => t.stop());
      stream = next; source = newSource;
      stream.getAudioTracks().forEach(track => { track.onended = recoverAudioDevice; });
      syncMute();
      description.textContent = "สลับไมโครโฟนสำเร็จ";
    } catch {
      muted = true; syncMute();
      description.textContent = "ไมโครโฟนหลุดและเชื่อมใหม่ไม่ได้ โปรดเลือกไมค์หรืออนุญาตสิทธิ์";
    } finally { recoveringMic = false; }
  }
  const oldSpeaker = outputSelect.value;
  await refreshOutputs();
  if (!([...outputSelect.options].some(option => option.value === oldSpeaker))) {
    outputSelect.value = "default";
    if ("setSinkId" in speakerAudio) {
      try { await speakerAudio.setSinkId("default"); }
      catch { description.textContent = "โปรดตรวจสอบลำโพงเริ่มต้นของเครื่อง"; }
    }
  }
}
navigator.mediaDevices?.addEventListener?.("devicechange", () => { recoverAudioDevice(); });
const MAX_BROWSER_RETRIES = 5;

function state(message, live = false, phase = "") {
  statusText.textContent = message;
  status.classList.toggle("live", live);
  orb.classList.toggle("active", live);
  status.dataset.phase = phase;
  if (orb.dataset.speaking !== "true") orb.dataset.mood = phase === "reconnecting" ? "reconnecting" :
    phase === "connected" ? "listening" : phase === "approval" ? "approval" : "idle";
  moodText.textContent = phase === "reconnecting" ? "กำลังเชื่อมต่อใหม่" :
    phase === "connected" ? "กำลังฟัง" : phase === "approval" ? "รอคำอนุมัติ" : "พร้อมสนทนา";
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
  hermesStatus.textContent = "Hermes พร้อมตอบ";
  orb.dataset.speaking = "false";
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
  hermesStatus.textContent = "Hermes กำลังพูด";
  orb.dataset.speaking = "true";
  moodText.textContent = "กำลังพูด";
  node.onended = () => {
    playing = playing.filter(x => x !== node);
    if (!playing.length) {
      hermesStatus.textContent = "Hermes พร้อมตอบ";
      orb.dataset.speaking = "false";
      moodText.textContent = "กำลังฟัง";
    }
  };
  const schedule = bufferController.nextTime(audio.currentTime, playAt, buffer.duration);
  if (schedule.drop) { node.disconnect(); return; }
  const when = schedule.when;
  node.start(when);
  playAt = when + buffer.duration;
  playing.push(node);

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
    silentGain = audio.createGain(); silentGain.gain.value = 0;
    // AudioWorklet capture uses 40ms PCM packets off the main thread.
    if (audio.audioWorklet && typeof AudioWorkletNode !== "undefined") {
      try {
        await audio.audioWorklet.addModule("/audio-worklet.js");
        workletNode = new AudioWorkletNode(audio, "hermes-mic", {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
        workletNode.port.onmessage = event => {
          if (!muted && active && desired && socket?.readyState === WebSocket.OPEN &&
              socket.bufferedAmount < 262144 && event.data instanceof ArrayBuffer) {
            socket.send(event.data);
          }
        };
        analyser.connect(workletNode);
        workletNode.connect(silentGain);
      } catch { workletNode = null; }
    }
    if (!workletNode) {
      processor = audio.createScriptProcessor(2048, 1, 1);
      analyser.connect(processor);
      processor.connect(silentGain);
      processor.onaudioprocess = event => {
        if (!muted && desired && active && socket?.readyState === WebSocket.OPEN &&
            socket.bufferedAmount < 262144) {
          socket.send(toPcm16(event.inputBuffer.getChannelData(0), audio.sampleRate));
        }
      };
    }
    silentGain.connect(audio.destination);
    outputGain = audio.createGain();
    playbackAnalyser = audio.createAnalyser();
    playbackAnalyser.fftSize = 256;
    outputGain.connect(playbackAnalyser);
    outputStream = audio.createMediaStreamDestination();
    // A media element supports setSinkId in eligible desktop browsers.
    if ("setSinkId" in speakerAudio) {
      playbackAnalyser.connect(outputStream);
      speakerAudio.srcObject = outputStream.stream;
      try { await speakerAudio.play(); }
      catch { playbackAnalyser.disconnect(); playbackAnalyser.connect(audio.destination); }
    } else {
      playbackAnalyser.connect(audio.destination);
    }
    stream.getAudioTracks().forEach(track => { track.enabled = false; });
    await refreshOutputs();
    stream.getAudioTracks().forEach(track => { track.onended = recoverAudioDevice; });
    drawWave();
  } finally { audioInitializing = false; }
}
function drawWave() {
  if (!ctx2d) return;
  cancelAnimationFrame(waveFrame);
  const samples = new Uint8Array(analyser?.fftSize || 256);
  const bars = 42, w = canvas.width, h = canvas.height;
  const assistantSamples = new Uint8Array(playbackAnalyser?.fftSize || 256);
  const frame = () => {
    ctx2d.clearRect(0, 0, w, h);
    assistantCtx.clearRect(0, 0, hermesCanvas.width, hermesCanvas.height);
    if (playbackAnalyser && active && playing.length) playbackAnalyser.getByteTimeDomainData(assistantSamples);
    else assistantSamples.fill(128);
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
    // The assistant waveform follows actual playback RMS, not microphone input.
    const width = hermesCanvas.width, height = hermesCanvas.height;
    assistantCtx.strokeStyle = "#af91ff"; assistantCtx.fillStyle = "#af91ff";
    const stride = width / bars;
    for (let n = 0; n < bars; n++) {
      const v = Math.abs((assistantSamples[Math.floor(n * assistantSamples.length / bars)] - 128) / 128);
      const size = Math.max(5, v * (height - 10) * 1.6);
      assistantCtx.fillRect(n * stride + 2, (height - size) / 2, Math.max(2, stride - 4), size);
    }
    if (analyser && active && !muted) {
      const magnitude = samples.reduce((acc, sample) => acc + Math.abs(sample - 128), 0) / samples.length;
      lastMicLevel = Math.min(100, Math.round(magnitude / 128 * 500));
      userStatus.textContent = magnitude > 2 ? "คุณกำลังพูด" : "กำลังฟังไมค์";
    } else {
      userStatus.textContent = muted ? "ไมค์ปิดอยู่" : "ไมค์พร้อมใช้งาน";
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
  workletNode?.port.postMessage({type: "enabled", value: desired && active && !muted});
  if (muted) userStatus.textContent = "ไมค์ปิดอยู่";
}
mute.addEventListener("click", () => { muted = !muted; syncMute(); });
async function disposeAudio() {
  clearInterval(diagnosticsTimer); diagnosticsTimer = null;
  pendingPings.clear();
  clearApproval();
  cancelPlayback();
  cancelAnimationFrame(waveFrame); waveFrame = null;
  if (processor) { processor.onaudioprocess = null; processor.disconnect(); processor = null; }
  if (workletNode) { workletNode.port.onmessage = null; workletNode.disconnect(); workletNode = null; }
  for (const node of [source, analyser, silentGain, outputGain, playbackAnalyser, outputStream]) {
    try { node?.disconnect(); } catch { /* disconnected */ }
  }
  source = analyser = silentGain = outputGain = playbackAnalyser = outputStream = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  speakerAudio.pause(); speakerAudio.srcObject = null;
  if (audio) await audio.close().catch(() => {});
  audio = null;
}
function scheduleReconnect() {
  if (readyAt && Date.now() - readyAt > 15000) reconnectCount = 0;
  readyAt = 0;
  active = false;
  clearApproval();
  cancelPlayback();
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
        readyAt = Date.now(); active = true; syncMute();
        start.disabled = true; end.disabled = false; mute.disabled = false;
        state(msg.restored ? "กู้คืนการสนทนาสำเร็จ" : "กำลังสนทนา", true, "connected");
        description.textContent = "พูดได้เลย Hermes กำลังฟังคุณอยู่";
        drawWave();
        startDiagnostics();
      } catch {
        description.textContent = "ไม่สามารถเปิดไมโครโฟนได้";
        stop();
      }
    } else if (msg.type === "status") {
      if (msg.phase === "reconnecting") cancelPlayback();
      state(msg.message || "กำลังเชื่อมต่อ...", msg.phase === "restored", msg.phase);
    } else if (msg.type === "interrupted") {
      cancelPlayback();
    } else if (msg.type === "pong") {
      const started = pendingPings.get(msg.id);
      if (started !== undefined) {
        pendingPings.delete(msg.id);
        bufferController.observeRtt(performance.now() - started);
      }
    } else if (msg.type === "approval_request") {
      if (typeof msg.id === "string" && typeof msg.request === "string") {
        clearApproval();
        currentApproval = msg.id;
        approvalText.textContent = msg.request;
        approvalPanel.hidden = false;
        state("รอคำอนุมัติ", false, "approval");
        // Approval is never triggered by speech, only a real UI click.
      }
    } else if (msg.type === "transcript") {
      append(msg.role, msg.text);
    } else if (msg.type === "notice") {
      description.textContent = msg.message;
    } else if (msg.type === "error") {
      description.textContent = msg.message || "ไม่สามารถเชื่อมต่อได้";
      // Authentication or competing session errors must not retry indefinitely.
      if (/login|expired/.test(msg.message || "")) stop(false);
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
  clearInterval(diagnosticsTimer); diagnosticsTimer = null;
  clearApproval();
  userStatus.textContent = "ไมค์พร้อมใช้งาน";
  hermesStatus.textContent = "Hermes พร้อมตอบ";
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
async function audioPreflight() {
  if (preflighting || desired) return;
  preflighting = true; preflightButton.disabled = true;
  preflightResult.textContent = "กำลังตรวจสอบไมโครโฟน...";
  let testingStream, context;
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("unsupported");
    testingStream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    context = new (window.AudioContext || window.webkitAudioContext)();
    await context.resume();
    const sourceNode = context.createMediaStreamSource(testingStream);
    const detector = context.createAnalyser(); detector.fftSize = 256;
    sourceNode.connect(detector);
    const data = new Uint8Array(detector.fftSize);
    let peak = 0;
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 75));
      detector.getByteTimeDomainData(data);
      const amplitude = Math.max(...data.map(x => Math.abs(x - 128)));
      peak = Math.max(peak, amplitude);
    }
    await refreshOutputs();
    preflightResult.textContent = peak > 2 ?
      "✓ ไมโครโฟนรับเสียงแล้ว · กำลังทดสอบลำโพง" :
      "ไมโครโฟนเชื่อมต่อแล้ว แต่ยังตรวจไม่พบเสียงพูด · กำลังทดสอบลำโพง";
    // Low-volume, short local beep; never sent to the server.
    const oscillator = context.createOscillator(), gain = context.createGain();
    oscillator.type = "sine"; oscillator.frequency.value = 440;
    gain.gain.value = 0.015;
    oscillator.connect(gain); gain.connect(context.destination);
    oscillator.start(); oscillator.stop(context.currentTime + .18);
    await new Promise(resolve => setTimeout(resolve, 230));
    preflightResult.textContent = (peak > 2 ? "✓ ตรวจพบเสียงไมค์" : "⚠ ไม่พบเสียงไมค์") +
      " · เล่นเสียงทดสอบแล้ว หากไม่ได้ยินให้ตรวจสอบระดับเสียง/ลำโพง";
  } catch (error) {
    preflightResult.textContent = "ไม่สามารถตรวจสอบเสียงได้: โปรดอนุญาตไมโครโฟนและตรวจสอบการตั้งค่าอุปกรณ์";
  } finally {
    testingStream?.getTracks().forEach(track => track.stop());
    if (context) await context.close().catch(() => {});
    preflightButton.disabled = false; preflighting = false;
  }
}
preflightButton.addEventListener("click", audioPreflight);

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
