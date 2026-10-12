class ModelSelector {
  constructor(api, onChanged) {
    this.api = api;
    this.onChanged = onChanged;
    this.select = document.getElementById('modelSelect');
    this.use = document.getElementById('useModel');
    this.refreshButton = document.getElementById('refreshModels');
    this.status = document.getElementById('modelStatus');
    this.current = '';
    this.models = [];
    this.active = false;
    this.serverChanging = false;
    this.loading = false;
    this.changing = false;
    this.notice = '';
    this.select.onchange = () => { this.notice = ''; this.render(); };
    this.use.onclick = () => this.apply();
    this.refreshButton.onclick = () => this.refresh();
    this.render();
  }

  populate(preferred = this.select.value) {
    this.select.replaceChildren();
    for (const name of this.models) {
      const option = document.createElement('option');
      option.value = name; option.textContent = name;
      this.select.append(option);
    }
    if (this.current && !this.models.includes(this.current)) {
      const option = document.createElement('option');
      option.value = this.current; option.textContent = this.current + ' (unavailable)'; option.disabled = true;
      this.select.append(option);
    }
    if (!this.models.length && !this.current) {
      const option = document.createElement('option');
      option.value = ''; option.textContent = 'No models available'; option.disabled = true;
      this.select.append(option);
    }
    this.select.value = this.models.includes(preferred) ? preferred : this.current || this.models[0] || '';
  }

  update(state) {
    this.active = state.task_active ?? !!(state.current && ['running', 'pausing', 'paused', 'stopping'].includes(state.current.status));
    this.serverChanging = !!state.model_switching;
    if (state.model !== this.current) {
      this.current = state.model;
      this.notice = '';
      this.populate(this.current);
    }
    this.render();
  }

  render() {
    const busy = this.loading || this.changing || this.serverChanging;
    this.select.disabled = this.active || busy || !this.models.length;
    this.use.disabled = this.select.disabled || !this.models.includes(this.select.value) || this.select.value === this.current;
    this.refreshButton.disabled = busy;
    document.getElementById('start').disabled = this.active || this.changing || this.serverChanging;
    this.use.textContent = this.changing || this.serverChanging ? 'Switching…' : 'Use model';
    this.refreshButton.textContent = this.loading ? 'Refreshing…' : 'Refresh models';
    this.status.textContent = this.changing || this.serverChanging ? 'Checking and saving the selected model…'
      : this.loading ? 'Loading installed models…'
      : this.active ? 'Finish or stop the current task before switching models.'
      : this.notice || (!this.models.length ? 'No models available. Install a model on the host and refresh.'
        : !this.models.includes(this.current) ? 'The current model is unavailable. Choose an installed model.'
        : this.select.value !== this.current ? 'Choose Use model to apply your selection.'
        : 'Next task uses ' + this.current + '.');
  }

  async refresh() {
    if (this.loading || this.changing || this.serverChanging) return;
    this.loading = true; this.notice = ''; this.render();
    try {
      const result = await this.api('/api/models');
      if (!Array.isArray(result.models) || !result.models.every(name => typeof name === 'string') || typeof result.model !== 'string') {
        throw new Error('Invalid model list received.');
      }
      this.models = result.models;
      this.current = result.model;
      this.populate();
      if (!this.models.length) this.notice = 'No models available. Install a model on the host and refresh.';
    } catch (failure) {
      this.notice = 'Could not refresh models: ' + failure.message;
    } finally { this.loading = false; this.render(); }
  }

  async apply() {
    if (this.use.disabled) return;
    const name = this.select.value;
    this.changing = true; this.notice = ''; this.render();
    let changed = false;
    try {
      const result = await this.api('/api/model', {model: name});
      if (result.model !== name) throw new Error('The selected model was not confirmed. Refresh the model list.');
      this.current = result.model;
      this.populate(this.current);
      this.notice = 'Saved. New tasks use ' + this.current + '.';
      changed = true;
    } catch (failure) {
      this.notice = 'Could not switch models: ' + failure.message;
    } finally { this.changing = false; this.render(); }
    if (changed) await this.onChanged();
  }
}

globalThis.ModelSelector = ModelSelector;
