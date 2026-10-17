/* Authenticated live display of the latest Ollama response. */
class ModelOutputView {
  constructor(getToken, getSelectedRun) {
    this.getToken = getToken;
    this.getSelectedRun = getSelectedRun;
    this.controller = null;
    this.retry = null;
    this.state = null;
    this.enabled = true;
    this.box = document.getElementById('modelOutput');
    this.status = document.getElementById('modelOutputStatus');
    this.autoScroll = document.getElementById('modelOutputFollow');
    this.copyButton = document.getElementById('modelOutputCopy');
    this.copyButton.addEventListener('click', () => navigator.clipboard.writeText(this.box.textContent));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) this.cancel();
      else this.start();
    });
    window.addEventListener('pagehide', () => this.cancel());
  }

  showSelected() {
    const state = this.state, selected = this.getSelectedRun();
    if (!state || !selected || state.run_id !== selected) {
      this.box.textContent = 'Live output is available for the current task.';
      this.status.textContent = 'Waiting for current task';
      return;
    }
    const follow = this.autoScroll.checked &&
      this.box.scrollHeight - this.box.scrollTop - this.box.clientHeight < 100;
    this.box.textContent = state.text || (state.status === 'generating'
      ? 'Model is processing the prompt…' : 'No generated text yet.');
    this.status.textContent = 'Step ' + state.step + ' · ' + state.status;
    if (follow) this.box.scrollTop = this.box.scrollHeight;
  }

  cancel() {
    clearTimeout(this.retry);
    this.retry = null;
    if (this.controller) this.controller.abort();
    this.controller = null;
  }

  start() {
    if (!this.enabled || document.hidden || !this.getToken() || this.controller || this.retry) return;
    const controller = new AbortController();
    this.controller = controller;
    this.consume(controller);
  }

  async consume(controller) {
    let reader;
    try {
      const response = await fetch('/api/model/output/stream', {
        headers: {Authorization: 'Bearer ' + this.getToken()},
        signal: controller.signal
      });
      if (!response.ok || !response.headers.get('Content-Type')?.includes('text/event-stream'))
        throw new Error('Model output stream HTTP ' + response.status);
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!controller.signal.aborted) {
        const {done, value} = await reader.read();
        if (done) throw new Error('Model output stream disconnected');
        buffer += decoder.decode(value, {stream: true});
        if (buffer.length > 250000) throw new Error('Model output event too large');
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const event = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const line = event.split('\n').find(line => line.startsWith('data: '));
          if (!line) continue;
          const data = JSON.parse(line.slice(6));
          if (this.controller !== controller) return;
          this.state = data;
          this.showSelected();
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) this.status.textContent = 'Reconnecting live output…';
    } finally {
      if (reader) { try { await reader.cancel(); } catch {} }
      if (this.controller === controller) {
        this.controller = null;
        if (this.enabled && !document.hidden && this.getToken())
          this.retry = setTimeout(() => { this.retry = null; this.start(); }, 1500);
      }
    }
  }
}
globalThis.ModelOutputView = ModelOutputView;
