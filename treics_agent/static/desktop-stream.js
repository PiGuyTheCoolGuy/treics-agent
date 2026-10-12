class DesktopFrameParser {
  constructor() {
    this.buffer = new Uint8Array();
    this.part = null;
  }

  push(chunk) {
    if (this.buffer.length + chunk.length > 8 * 1024 * 1024) throw new Error('Desktop stream frame is too large.');
    const joined = new Uint8Array(this.buffer.length + chunk.length);
    joined.set(this.buffer); joined.set(chunk, this.buffer.length); this.buffer = joined;
    const parts = [];
    while (true) {
      if (!this.part) {
        let end = -1;
        for (let i = 0; i + 3 < this.buffer.length; i++) {
          if (this.buffer[i] === 13 && this.buffer[i+1] === 10 && this.buffer[i+2] === 13 && this.buffer[i+3] === 10) { end = i; break; }
        }
        if (end < 0) {
          if (this.buffer.length > 8192) throw new Error('Invalid desktop stream headers.');
          break;
        }
        if (end > 8192) throw new Error('Invalid desktop stream headers.');
        const text = new TextDecoder().decode(this.buffer.subarray(0, end));
        if (!text.trimStart().startsWith('--treic-frame\r\n')) throw new Error('Invalid desktop stream boundary.');
        const headers = {};
        for (const line of text.split('\r\n')) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon+1).trim();
        }
        const length = Number(headers['content-length']);
        if (!Number.isInteger(length) || length < 1 || length > 4 * 1024 * 1024) throw new Error('Invalid desktop stream frame length.');
        this.part = {headers, length};
        this.buffer = this.buffer.slice(end + 4);
      }
      if (this.buffer.length < this.part.length) break;
      parts.push({headers: this.part.headers, bytes: this.buffer.slice(0, this.part.length)});
      this.buffer = this.buffer.slice(this.part.length);
      this.part = null;
    }
    return parts;
  }
}

class DesktopLiveView {
  constructor(getToken, getState) {
    this.getToken = getToken;
    this.getState = getState;
    this.enabled = true;
    this.controller = null;
    this.retry = null;
    this.canvas = document.getElementById('liveScreen');
    this.caption = document.getElementById('screenTime');
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { this.cancel(); this.caption.textContent = 'Live view paused while this tab is hidden.'; }
      else if (this.enabled) this.start();
    });
    window.addEventListener('pagehide', () => this.cancel());
  }

  cancel() {
    clearTimeout(this.retry); this.retry = null;
    const controller = this.controller; this.controller = null;
    if (controller) controller.abort();
  }

  pause() {
    this.enabled = false; this.cancel();
    this.caption.textContent = 'Live view paused. The task continues running.';
  }

  resume() { this.enabled = true; this.start(); }

  start() {
    if (!this.enabled || document.hidden || !this.getToken() || this.controller || this.retry) return;
    const controller = new AbortController();
    this.controller = controller;
    this.caption.textContent = 'Connecting to live desktop…';
    this.consume(controller);
  }

  async paint(part, controller) {
    if (part.headers['content-type'] === 'application/json') {
      const status = JSON.parse(new TextDecoder().decode(part.bytes));
      if (this.controller === controller) this.caption.textContent = status.message || 'Waiting for desktop…';
      return;
    }
    if (part.headers['content-type'] !== 'image/jpeg') throw new Error('Invalid desktop stream content.');
    const dw = Number(part.headers['x-desktop-width']), dh = Number(part.headers['x-desktop-height']);
    const captured = Number(part.headers['x-captured-at']), fps = Number(part.headers['x-frame-rate']);
    if (![dw, dh].every(n => Number.isInteger(n) && n > 0 && n <= 16384) || !Number.isFinite(captured)
        || !Number.isInteger(fps) || fps < 1 || fps > 15) {
      throw new Error('Invalid desktop stream dimensions.');
    }
    const bitmap = await createImageBitmap(new Blob([part.bytes], {type: 'image/jpeg'}));
    try {
      if (controller.signal.aborted || this.controller !== controller) return;
      if (this.canvas.width !== bitmap.width || this.canvas.height !== bitmap.height) {
        this.canvas.width = bitmap.width; this.canvas.height = bitmap.height;
      }
      const context = this.canvas.getContext('2d');
      context.drawImage(bitmap, 0, 0);
      // The native marker is already in the stream. Draw a fallback if it failed.
      const state = this.getState(), click = state?.last_click;
      if (click && state.click_marker_error) {
        const x = click.x * bitmap.width / dw, y = click.y * bitmap.height / dh;
        for (const [color, width] of [['white', 4], ['#ff4040', 2]]) {
          context.strokeStyle = color; context.lineWidth = width; context.beginPath();
          context.arc(x, y, 10, 0, Math.PI * 2);
          for (const line of [[x-20,y,x-14,y], [x+14,y,x+20,y], [x,y-20,x,y-14], [x,y+14,x,y+20]]) {
            context.moveTo(line[0], line[1]); context.lineTo(line[2], line[3]);
          }
          context.stroke();
        }
      }
      this.canvas.hidden = false;
      document.getElementById('screen').hidden = true;
      document.getElementById('screenPlaceholder').hidden = true;
      this.caption.textContent = 'Live · '+fps+' FPS target · '+new Date(captured*1000).toLocaleTimeString();
    } finally { bitmap.close(); }
  }

  async consume(controller) {
    let reader, watchdog;
    const resetDeadline = () => { clearTimeout(watchdog); watchdog = setTimeout(() => controller.abort(), 10000); };
    try {
      resetDeadline();
      const response = await fetch('/api/desktop/stream', {
        headers: {Authorization: 'Bearer '+this.getToken()}, signal: controller.signal,
      });
      if (!response.ok) throw new Error('Desktop stream HTTP '+response.status+'.');
      if (!response.headers.get('Content-Type')?.includes('multipart/x-mixed-replace')) throw new Error('Invalid desktop stream response.');
      reader = response.body.getReader();
      const parser = new DesktopFrameParser();
      while (!controller.signal.aborted) {
        resetDeadline();
        const {done, value} = await reader.read();
        if (done) throw new Error('Desktop stream disconnected.');
        for (const part of parser.push(value)) await this.paint(part, controller);
      }
    } catch (failure) {
      if (this.controller === controller && this.enabled && !document.hidden) {
        this.caption.textContent = 'Live view reconnecting · '+(failure.name === 'AbortError' ? 'stream timed out' : failure.message);
      }
    } finally {
      clearTimeout(watchdog);
      if (reader) { try { await reader.cancel(); } catch {} }
      if (this.controller === controller) {
        this.controller = null;
        if (this.enabled && !document.hidden && this.getToken()) {
          this.retry = setTimeout(() => { this.retry = null; this.start(); }, 1500);
        }
      }
    }
  }
}

globalThis.DesktopFrameParser = DesktopFrameParser;
globalThis.DesktopLiveView = DesktopLiveView;
