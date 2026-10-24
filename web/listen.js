// Listen mode: streams the mic to the server (Deepgram via /ws/listen) or uses the
// browser's own speech recognition and posts final text to /api/listen/text.
//
// iOS notes: the page loses the mic when the screen locks or the app is backgrounded,
// and another app (e.g. Wispr Flow) can take the mic. We tear down on hide and try
// to restart when visible again; if iOS blocks the restart without a tap, we resume
// on the next tap anywhere.

const WORKLET_SRC = `
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.target = o.targetRate || 16000;
    this.ratio = Math.max(1, sampleRate / this.target);
    this.frameLen = Math.round(this.target * (o.frameMs || 100) / 1000);
    this.out = new Int16Array(this.frameLen);
    this.n = 0; this.acc = 0; this.cnt = 0; this.t = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || !input[0]) return true;
    const a = input[0], b = input[1];
    for (let i = 0; i < a.length; i++) {
      const x = b ? (a[i] + b[i]) * 0.5 : a[i];
      this.acc += x; this.cnt++; this.t += 1;
      if (this.t >= this.ratio) {
        this.t -= this.ratio;
        let s = this.acc / this.cnt;
        this.acc = 0; this.cnt = 0;
        s = s < -1 ? -1 : (s > 1 ? 1 : s);
        this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.frameLen) {
          const buf = this.out.slice().buffer;
          this.port.postMessage(buf, [buf]);
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm16-downsampler', Pcm16Downsampler);
`;

let workletUrl = null;
function getWorkletUrl() {
  if (!workletUrl) workletUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
  return workletUrl;
}

const MAX_BUFFERED_FRAMES = 50; // ~5 s of audio kept while the socket reconnects

export class Listener {
  constructor({ onTranscript, onStatus, onStopped, postText }) {
    this.onTranscript = onTranscript || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onStopped = onStopped || (() => {});
    this.postText = postText || (async () => {});
    this.wanted = false;
    this.mode = null;
    this.ctx = null; this.stream = null; this.node = null; this.src = null; this.sink = null;
    this.ws = null; this.wsBackoff = 1000; this.wsTimer = null;
    this.frames = [];
    this.rec = null; this.recRestartTimer = null;
    this.wakeLock = null;
    this.pausedHidden = false;
    this.micRetry = 0; this.micTimer = null;
    this._onVis = () => this._visibilityChanged();
    this._resumeOnTap = () => this._tapResume();
  }

  get active() { return this.wanted; }

  // Must be called directly from a user gesture (toggle tap) so iOS allows audio.
  start(mode) {
    this.wanted = true;
    this.mode = mode === "browser" ? "browser" : "deepgram";
    this.pausedHidden = false;
    document.addEventListener("visibilitychange", this._onVis);
    this._wake();
    if (this.mode === "deepgram") {
      // Create the AudioContext synchronously inside the gesture.
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        this.ctx = new AC();
      } catch (e) {
        this._fail("This browser can't record audio.");
        return;
      }
      this._status("connecting", "Starting mic…");
      this._startAudio().catch((e) => this._audioError(e));
      this._openSocket();
    } else {
      this._startBrowser();
    }
  }

  stop(reasonText) {
    this.wanted = false;
    document.removeEventListener("visibilitychange", this._onVis);
    document.removeEventListener("pointerdown", this._resumeOnTap, true);
    clearTimeout(this.micTimer);
    this._teardownAudio(true);
    this._closeSocket();
    this._stopBrowser();
    this._releaseWake();
    this.frames = [];
    this._status("off", reasonText || "Off");
  }

  // ---------- Deepgram path ----------

  async _startAudio() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw Object.assign(new Error("Microphone access isn't available here. Use https or the home-screen app."), { name: "NotSupportedError" });
    }
    if (!this.ctx || this.ctx.state === "closed") {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    if (!this.wanted) { stream.getTracks().forEach((t) => t.stop()); return; }
    this.stream = stream;
    const track = stream.getAudioTracks()[0];
    if (track) {
      track.addEventListener("ended", () => this._micLost());
      track.addEventListener("mute", () => { if (this.wanted) this._status("paused", "Mic paused. Another app is using it."); });
      track.addEventListener("unmute", () => { if (this.wanted) this._status("listening", "Listening"); });
    }
    await this.ctx.audioWorklet.addModule(getWorkletUrl());
    if (!this.wanted) { this._teardownAudio(true); return; }
    this.src = this.ctx.createMediaStreamSource(stream);
    this.node = new AudioWorkletNode(this.ctx, "pcm16-downsampler", { processorOptions: { targetRate: 16000, frameMs: 100 } });
    this.node.port.onmessage = (e) => this._frame(e.data);
    // Route through a muted gain node so the graph is pulled without playing audio.
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.src.connect(this.node);
    this.node.connect(this.sink);
    this.sink.connect(this.ctx.destination);
    try { await this.ctx.resume(); } catch {}
    if (this.ctx.state !== "running") {
      this._status("paused", "Tap anywhere to resume listening");
      document.addEventListener("pointerdown", this._resumeOnTap, true);
    } else {
      this.micRetry = 0;
      this._status(this.ws && this.ws.readyState === WebSocket.OPEN ? "listening" : "connecting",
        this.ws && this.ws.readyState === WebSocket.OPEN ? "Listening" : "Connecting…");
    }
  }

  _tapResume() {
    document.removeEventListener("pointerdown", this._resumeOnTap, true);
    if (!this.wanted || !this.ctx) return;
    this.ctx.resume().then(() => {
      if (this.ctx && this.ctx.state === "running") this._status("listening", "Listening");
    }).catch(() => {});
  }

  _audioError(e) {
    const name = e && e.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      this._fail("Microphone permission denied. Allow it in Settings › Safari › Microphone.");
    } else if (name === "NotSupportedError") {
      this._fail(e.message);
    } else if (name === "NotFoundError") {
      this._fail("No microphone found.");
    } else {
      // Usually the mic is busy (another app). Retry with backoff.
      this._micLost(e && e.message);
    }
  }

  _micLost(detail) {
    if (!this.wanted || document.hidden) return;
    this._teardownAudio(false);
    this.micRetry += 1;
    if (this.micRetry > 6) {
      this._fail("Lost the microphone. Another app may be using it. Turn Listen on again when it's free.");
      return;
    }
    const wait = Math.min(15000, 1000 * 2 ** (this.micRetry - 1));
    this._status("paused", `Mic unavailable (another app may have it). Retrying in ${Math.round(wait / 1000)}s…`);
    clearTimeout(this.micTimer);
    this.micTimer = setTimeout(() => {
      if (this.wanted && !document.hidden) this._startAudio().catch((e) => this._audioError(e));
    }, wait);
  }

  _teardownAudio(closeCtx) {
    try { this.node && this.node.port && (this.node.port.onmessage = null); } catch {}
    try { this.src && this.src.disconnect(); } catch {}
    try { this.node && this.node.disconnect(); } catch {}
    try { this.sink && this.sink.disconnect(); } catch {}
    if (this.stream) this.stream.getTracks().forEach((t) => { try { t.stop(); } catch {} });
    this.stream = null; this.src = null; this.node = null; this.sink = null;
    if (closeCtx && this.ctx) { try { this.ctx.close(); } catch {} this.ctx = null; }
  }

  _frame(buf) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      while (this.frames.length) this.ws.send(this.frames.shift());
      this.ws.send(buf);
    } else {
      this.frames.push(buf);
      if (this.frames.length > MAX_BUFFERED_FRAMES) this.frames.shift();
    }
  }

  _openSocket() {
    clearTimeout(this.wsTimer);
    if (!this.wanted) return;
    const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/listen`;
    let ws;
    try { ws = new WebSocket(url); } catch (e) { this._scheduleSocket(); return; }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.wsBackoff = 1000;
      if (this.ctx && this.ctx.state === "running" && this.stream) this._status("listening", "Listening");
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "transcript") this.onTranscript(msg);
      else if (msg.type === "status") {
        if (msg.state === "upstream_error") this._status("error", `Transcription error${msg.detail ? `: ${msg.detail}` : ""}. Reconnecting…`);
        else if (msg.state === "connected" && this.stream) this._status("listening", "Listening");
      }
    };
    ws.onclose = (e) => {
      if (this.ws === ws) this.ws = null;
      if (!this.wanted || document.hidden) return;
      if (e.code === 4401 || e.code === 1008) { this._fail("Not signed in. Sign in again, then turn Listen on."); return; }
      this._status("connecting", "Reconnecting…");
      this._scheduleSocket();
    };
    ws.onerror = () => {};
  }

  _scheduleSocket() {
    clearTimeout(this.wsTimer);
    const wait = this.wsBackoff;
    this.wsBackoff = Math.min(15000, this.wsBackoff * 2);
    this.wsTimer = setTimeout(() => this._openSocket(), wait);
  }

  _closeSocket() {
    clearTimeout(this.wsTimer);
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" })); } catch {}
      try { ws.close(1000); } catch {}
    }
  }

  // ---------- Browser speech recognition path ----------

  _startBrowser() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { this._fail("Speech recognition isn't available in this browser."); return; }
    const rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    rec.onstart = () => this._status("listening", "Listening (on-device)");
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const text = (r[0] && r[0].transcript || "").trim();
        if (!text) continue;
        const at = new Date().toISOString();
        if (r.isFinal) {
          this.onTranscript({ type: "transcript", text, speaker: null, is_final: true, at });
          this.postText({ text, is_final: true, at });
        } else {
          this.onTranscript({ type: "transcript", text, speaker: null, is_final: false, at });
        }
      }
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        this._fail("Speech recognition permission denied.");
      } else if (e.error === "audio-capture") {
        this._status("paused", "Mic unavailable. Another app may be using it.");
      }
      // no-speech / aborted / network: onend restarts.
    };
    rec.onend = () => {
      if (this.rec !== rec) return;
      if (this.wanted && !document.hidden) {
        clearTimeout(this.recRestartTimer);
        this.recRestartTimer = setTimeout(() => {
          if (this.rec === rec && this.wanted && !document.hidden) {
            try { rec.start(); } catch {}
          }
        }, 400);
      }
    };
    this.rec = rec;
    try { rec.start(); } catch (e) { this._fail("Couldn't start speech recognition."); }
  }

  _stopBrowser() {
    clearTimeout(this.recRestartTimer);
    const rec = this.rec;
    this.rec = null;
    if (rec) { rec.onend = null; try { rec.abort(); } catch {} }
  }

  // ---------- Visibility, wake lock, status ----------

  _visibilityChanged() {
    if (!this.wanted) return;
    if (document.hidden) {
      this.pausedHidden = true;
      clearTimeout(this.micTimer);
      if (this.mode === "deepgram") { this._teardownAudio(true); this._closeSocket(); }
      else this._stopBrowser();
      this._releaseWake();
      this._status("paused", "Paused while the app was in the background");
    } else if (this.pausedHidden) {
      this.pausedHidden = false;
      this._wake();
      this.onStatus({ state: "resumed", text: "Resuming…" });
      if (this.mode === "deepgram") {
        this.micRetry = 0;
        this._startAudio().catch((e) => this._audioError(e));
        this._openSocket();
      } else {
        this._startBrowser();
      }
    }
  }

  async _wake() {
    if (!("wakeLock" in navigator) || document.hidden) return;
    try {
      this.wakeLock = await navigator.wakeLock.request("screen");
      this.wakeLock.addEventListener("release", () => { this.wakeLock = null; });
    } catch { /* not allowed (e.g. low power); fine */ }
  }

  _releaseWake() {
    const w = this.wakeLock;
    this.wakeLock = null;
    if (w) w.release().catch(() => {});
  }

  _status(state, text) { this.onStatus({ state, text }); }

  _fail(text) {
    this.stop(text);
    this.onStatus({ state: "error", text });
    this.onStopped(text);
  }
}
