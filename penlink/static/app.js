(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const video = $('screen');
  const jpeg = $('jpeg');
  const jctx = jpeg.getContext('2d', { alpha: false });
  const statusEl = $('status');
  const cursorEl = $('cursor');
  const pairEl = $('pair');
  const pinEl = $('pin');
  const pairBtn = $('pairBtn');
  const pairMsg = $('pairMsg');
  const MediaSourceClass = window.ManagedMediaSource || window.MediaSource;
  const TOKEN_KEY = 'penlink-token';

  const HOVER = 0, CONTACT = 1, OUT = 2;

  let ws = null;
  let connected = false;
  let mode = 'mp4';
  let view = video;          // the element currently showing the screen
  let rtt = null;
  let frames = 0;
  let lagMs = null;
  let penDown = false;
  let lastError = '';
  let frameW = 16, frameH = 9;
  // mp4 mode
  let mediaSource = null, sourceBuffer = null, queue = [], lastTrim = 0;
  // jpeg mode
  let pending = null, decoding = false;

  const setStatus = (text) => { statusEl.textContent = text; };

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  // ---- token storage and pairing ---------------------------------------------------
  function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } }
  function setToken(t) {
    try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
  }

  function showPair(message) {
    pairMsg.textContent = message || '';
    pairBtn.disabled = false;
    pairEl.hidden = false;
    setStatus('not paired');
  }

  async function start() {
    const token = getToken();
    if (!token) { showPair(''); return; }
    let r;
    try {
      r = await fetch('/auth?token=' + encodeURIComponent(token), { cache: 'no-store' });
    } catch (e) {
      setStatus('PC not reachable, retrying…');
      setTimeout(start, 1500);
      return;
    }
    if (r.ok) { pairEl.hidden = true; connect(token); }
    else if (r.status === 403) { setToken(''); showPair('This iPad is not paired (or was forgotten on the PC).'); }
    else { setStatus('server error ' + r.status); setTimeout(start, 1500); }
  }

  async function submitPin() {
    const pin = pinEl.value.trim();
    if (!pin) return;
    pairBtn.disabled = true;
    pairMsg.textContent = '';
    try {
      const r = await fetch('/pair', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
      });
      if (r.ok) {
        const j = await r.json();
        setToken(j.token);
        pinEl.value = '';
        start();
        return;
      }
      pairMsg.textContent = r.status === 429 ? 'Too many wrong PINs. Restart PenLink on the PC.' : 'Wrong PIN, try again.';
    } catch (e) {
      pairMsg.textContent = 'Could not reach the PC.';
    }
    pairBtn.disabled = false;
  }
  pairBtn.addEventListener('click', submitPin);
  pinEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitPin(); });

  // ---- layout: fit the screen into the window, keeping its aspect ratio ----
  function layout() {
    const scale = Math.min(innerWidth / frameW, innerHeight / frameH);
    for (const el of [video, jpeg]) {
      el.style.width = Math.floor(frameW * scale) + 'px';
      el.style.height = Math.floor(frameH * scale) + 'px';
    }
  }
  addEventListener('resize', layout);
  addEventListener('orientationchange', layout);
  layout();

  function useMode(m) {
    mode = m === 'jpeg' ? 'jpeg' : 'mp4';
    view = mode === 'jpeg' ? jpeg : video;
    video.hidden = view !== video;
    jpeg.hidden = view !== jpeg;
  }

  // ---- connection ----------------------------------------------------------------
  function connect(token) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { connected = true; lastError = ''; setStatus('connected'); };
    ws.onclose = () => {
      connected = false;
      teardownMedia();
      setStatus(lastError || 'disconnected, retrying…');
      setTimeout(start, 1000);
    };
    ws.onmessage = onMessage;
  }

  function onMessage(ev) {
    if (typeof ev.data === 'string') {
      const m = JSON.parse(ev.data);
      if (m.t === 'hello') {
        frameW = m.fw; frameH = m.fh;
        useMode(m.mode);
        layout();
        if (mode === 'mp4') setupMedia(m.mime);
      } else if (m.t === 'error') {
        lastError = m.msg;
        setStatus(m.msg);
      } else if (m.t === 'pong' && typeof m.c === 'number') {
        rtt = performance.now() - m.c;
      }
      return;
    }
    if (mode === 'jpeg') {
      send({ t: 'ack' });   // lets the server send the next frame
      pending = ev.data;
      if (!decoding) drawPending();
    } else {
      queue.push(new Uint8Array(ev.data));   // first blob is the init segment, then one per frame
      pump();
    }
  }

  // ---- JPEG mode -------------------------------------------------------------------
  async function drawPending() {
    decoding = true;
    while (pending) {
      const data = pending;
      pending = null;
      try {
        const bitmap = await createImageBitmap(new Blob([data], { type: 'image/jpeg' }));
        if (bitmap.width !== jpeg.width || bitmap.height !== jpeg.height) {
          jpeg.width = bitmap.width;
          jpeg.height = bitmap.height;
          frameW = bitmap.width; frameH = bitmap.height;
          layout();
        }
        jctx.drawImage(bitmap, 0, 0);
        bitmap.close();
        frames++;
      } catch (e) { /* skip a bad frame */ }
    }
    decoding = false;
  }

  // ---- H.264/HEVC mode: fragmented MP4 played through Media Source --------------------
  function teardownMedia() {
    queue = [];
    sourceBuffer = null;
    if (mediaSource) {
      try { if (mediaSource.readyState === 'open') mediaSource.endOfStream(); } catch (e) { /* ignore */ }
      mediaSource = null;
    }
    video.removeAttribute('src');
    video.load();
  }

  function setupMedia(mime) {
    teardownMedia();
    if (!MediaSourceClass) { lastError = 'this browser cannot play the stream (needs iPadOS 17.1+)'; setStatus(lastError); return; }
    if (!MediaSourceClass.isTypeSupported(mime)) { lastError = 'this browser cannot decode ' + mime; setStatus(lastError); return; }
    mediaSource = new MediaSourceClass();
    video.disableRemotePlayback = true;   // required for ManagedMediaSource in Safari
    video.src = URL.createObjectURL(mediaSource);
    mediaSource.addEventListener('sourceopen', () => {
      sourceBuffer = mediaSource.addSourceBuffer(mime);
      sourceBuffer.addEventListener('updateend', pump);
      sourceBuffer.addEventListener('error', () => ws && ws.close());
      pump();
    }, { once: true });
  }

  function pump() {
    if (!sourceBuffer || sourceBuffer.updating) return;
    if (queue.length) {
      // append everything that piled up in one go
      let total = 0;
      for (const q of queue) total += q.length;
      const blob = new Uint8Array(total);
      let offset = 0;
      for (const q of queue) { blob.set(q, offset); offset += q.length; }
      frames += queue.length;
      queue = [];
      try { sourceBuffer.appendBuffer(blob); } catch (e) { if (ws) ws.close(); }
      return;
    }
    keepLive();
  }

  // Stay at the live edge: skip ahead if we fall behind, speed up slightly when a little behind,
  // and throw away video that has already been played.
  function keepLive() {
    const b = video.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    const lag = end - video.currentTime;
    lagMs = Math.max(0, Math.round(lag * 1000));
    if (video.paused) video.play().catch(() => {});
    if (lag > 0.25 || video.currentTime < b.start(0)) video.currentTime = Math.max(b.start(0), end - 0.04);
    video.playbackRate = lag > 0.1 ? 1.1 : 1;
    const now = performance.now();
    if (now - lastTrim > 3000 && video.currentTime - b.start(0) > 10) {
      lastTrim = now;
      try { sourceBuffer.remove(0, video.currentTime - 5); } catch (e) { /* ignore */ }
    }
  }

  setInterval(() => {
    if (!connected) return;
    const fps = frames;
    frames = 0;
    let text = 'live · ' + fps + ' fps';
    if (rtt !== null) text += ' · ping ' + Math.round(rtt) + ' ms';
    if (mode === 'mp4' && lagMs !== null) text += ' · buffer ' + lagMs + ' ms';
    setStatus(text);
    send({ t: 'ping', c: performance.now() });
  }, 1000);

  // ---- Apple Pencil input ----------------------------------------------------------
  // Each sample is [x, y, pressure, tiltX, tiltY, state] with x/y as 0..1 across the image.
  function samplesFrom(e, state) {
    const rect = view.getBoundingClientRect();
    let list = [];
    if (e.type === 'pointermove' && typeof e.getCoalescedEvents === 'function') {
      list = e.getCoalescedEvents();   // all the intermediate Pencil positions
    }
    if (!list.length) list = [e];
    return list.map((p) => [
      +((p.clientX - rect.left) / rect.width).toFixed(5),
      +((p.clientY - rect.top) / rect.height).toFixed(5),
      +(state === CONTACT ? p.pressure : 0).toFixed(3),
      Math.round(p.tiltX || 0),
      Math.round(p.tiltY || 0),
      state,
    ]);
  }

  function showCursor(e, state) {
    cursorEl.hidden = state === OUT;
    cursorEl.classList.toggle('down', state === CONTACT);
    cursorEl.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
  }

  function onPen(e, state) {
    if (e.pointerType !== 'pen') return;   // fingers and palms are ignored on purpose
    e.preventDefault();
    send({ t: 'pen', s: samplesFrom(e, state) });
    showCursor(e, state);
  }

  for (const el of [video, jpeg]) {
    el.addEventListener('pointerdown', (e) => {
      if (e.pointerType !== 'pen') return;
      penDown = true;
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      onPen(e, CONTACT);
    });
    el.addEventListener('pointermove', (e) => onPen(e, penDown ? CONTACT : HOVER));
    el.addEventListener('pointerup', (e) => {
      if (e.pointerType !== 'pen') return;
      penDown = false;
      onPen(e, HOVER);
    });
    el.addEventListener('pointercancel', (e) => {
      if (e.pointerType !== 'pen') return;
      penDown = false;
      onPen(e, OUT);
    });
    el.addEventListener('pointerleave', (e) => {
      if (e.pointerType === 'pen' && !penDown) onPen(e, OUT);
    });
  }

  // ---- stop Safari from reacting to gestures -------------------------------------
  ['gesturestart', 'gesturechange', 'gestureend'].forEach((type) =>
    document.addEventListener(type, (e) => e.preventDefault()));
  document.addEventListener('contextmenu', (e) => e.preventDefault());
  document.addEventListener('touchmove', (e) => e.preventDefault(), { passive: false });

  // ---- full screen button -------------------------------------------------------
  $('fs').addEventListener('click', () => {
    const el = document.documentElement;
    const isFull = document.fullscreenElement || document.webkitFullscreenElement;
    try {
      if (isFull) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      else (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
    } catch (err) { /* not supported here: use Add to Home Screen instead */ }
  });

  start();
})();
