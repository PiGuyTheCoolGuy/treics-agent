/* Authenticated live display of Ollama response chunks and inference progress. */
class ModelOutputView {
  constructor(getToken, getSelectedRun) {
    this.getToken = getToken;
    this.getSelectedRun = getSelectedRun;
    this.controller = null;
    this.retry = null;
    this.ticker = null;
    this.state = null;
    this.enabled = true;
    this.connected = false;
    this.connectionError = '';
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
      this.status.textContent = this.connectionError || (this.connected
        ? 'Connected · Waiting for current task' : 'Connecting to model output…');
      return;
    }
    const now = Date.now() / 1000;
    const elapsed = state.started_at ? Math.max(0, Math.floor(now - state.started_at)) : 0;
    const sinceLast = state.last_token_at ? Math.max(0, Math.floor(now - state.last_token_at)) : null;
    const receiving = state.chunks || state.thinking_chunks;
    const active = ['generating', 'streaming', 'thinking'].includes(state.status);
    const follow = this.autoScroll.checked &&
      this.box.scrollHeight - this.box.scrollTop - this.box.clientHeight < 100;

    let output;
    if (state.thinking && state.text)
      output = '[Model-provided thinking]\n' + state.thinking +
               '\n\n[Generated response]\n' + state.text;
    else if (state.thinking)
      output = '[Model-provided thinking]\n' + state.thinking;
    else if (state.text)
      output = state.text;
    else if (state.status === 'generating')
      output = 'Waiting for first token. Ollama may be loading the model, processing the screenshot, or evaluating the prompt.\n' +
               'Prompt processing does not emit partial response text.';
    else if (state.status === 'error')
      output = state.error || 'Model request failed before returning response text.';
    else
      output = 'No generated response text yet.';

    if (this.box.textContent !== output) {
      this.box.textContent = output;
      if (follow) this.box.scrollTop = this.box.scrollHeight;
    }
    const age = active ? ' · ' + elapsed + 's elapsed' : '';
    const tokens = receiving ? ' · ' + (state.chunks || 0) + ' text chunks' +
      (state.thinking_chunks ? ' · ' + state.thinking_chunks + ' thinking chunks' : '') : '';
    const inactivity = active && sinceLast !== null ? ' · Last chunk ' + sinceLast + 's ago' : '';
    let phase = state.status;
    if (state.status === 'generating' && !receiving) phase = 'Waiting for first token';
    if (state.status === 'thinking') phase = 'Model-provided thinking';
    if (state.status === 'streaming') phase = 'Receiving text';
    if (state.status === 'error') phase = 'Error: ' + (state.error || 'Model inference failed');
    this.status.textContent = this.connectionError ||
      ('Step ' + state.step + ' · ' + phase + age + tokens + inactivity +
       (this.connected ? '' : ' · Reconnecting dashboard stream…'));
  }

  cancel() {
    clearTimeout(this.retry); this.retry = null;
    clearInterval(this.ticker); this.ticker = null;
    if (this.controller) this.controller.abort();
    this.controller = null;
    this.connected = false;
  }

  start() {
    if (!this.enabled || document.hidden || !this.getToken() || this.controller || this.retry) return;
    const controller = new AbortController();
    this.controller = controller;
    if (!this.ticker) this.ticker = setInterval(() => this.showSelected(), 1000);
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
      this.connected = true;
      this.connectionError = '';
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
      if (!controller.signal.aborted) {
        this.connected = false;
        this.connectionError = 'Live output disconnected: ' + error.message + ' · Reconnecting…';
        this.showSelected();
      }
    } finally {
      if (reader) { try { await reader.cancel(); } catch {} }
      if (this.controller === controller) {
        this.controller = null;
        this.connected = false;
        if (this.enabled && !document.hidden && this.getToken())
          this.retry = setTimeout(() => { this.retry = null; this.start(); }, 1500);
      }
    }
  }
}
globalThis.ModelOutputView = ModelOutputView;
