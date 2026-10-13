class StallControls {
  constructor(api, onSaved) {
    this.api = api;
    this.onSaved = onSaved;
    this.mode = document.getElementById('stallMode');
    this.attempts = document.getElementById('stallAttempts');
    this.button = document.getElementById('saveStall');
    this.status = document.getElementById('stallStatus');
    this.current = 0;
    this.dirty = false;
    this.saving = false;
    this.serverSaving = false;
    this.notice = '';
    this.mode.onchange = this.attempts.oninput = () => {
      this.dirty = true; this.notice = ''; this.render();
    };
    this.button.onclick = () => this.save();
    this.render();
  }

  update(state) {
    const value = state.stall_attempts ?? 0;
    this.serverSaving = !!state.stall_setting_saving || !!state.model_switching;
    if (!this.dirty && !this.saving) {
      if (value !== this.current) this.notice = '';
      this.mode.value = value ? 'auto' : 'manual';
      if (value) this.attempts.value = value;
    }
    this.current = value;
    this.render();
  }

  render() {
    const busy = this.saving || this.serverSaving;
    this.mode.disabled = busy;
    this.attempts.disabled = busy || this.mode.value !== 'auto';
    this.button.disabled = busy || !this.dirty;
    this.button.textContent = this.saving ? 'Saving…' : 'Apply stall controls';
    this.status.textContent = this.saving ? 'Saving stall controls…'
      : this.notice || (this.dirty ? 'Apply your changes to use them for the current and future tasks.'
        : this.current ? 'Automatic pause after ' + this.current + ' matching actions or errors.'
        : 'Manual control. Use Pause or Stop; repeated attempts keep running within the task budget.');
  }

  async save() {
    if (this.button.disabled) return;
    const value = this.mode.value === 'manual' ? 0 : Number(this.attempts.value);
    if (!Number.isInteger(value) || value < (this.mode.value === 'manual' ? 0 : 1) || value > 1000) {
      this.notice = 'Choose a whole number from 1 to 1000 for automatic pausing.';
      this.render(); return;
    }
    this.saving = true; this.notice = ''; this.render();
    let saved = false;
    try {
      const result = await this.api('/api/stall', {stall_attempts: value});
      if (result.stall_attempts !== value) throw new Error('The stall setting was not confirmed.');
      this.current = value;
      this.dirty = false;
      this.notice = 'Saved. ' + (value ? 'Automatic pause after ' + value + ' matching attempts.'
        : 'You control Pause and Stop. Press Resume if the task is already paused.');
      saved = true;
    } catch (failure) {
      this.notice = 'Could not save stall controls: ' + failure.message;
    } finally { this.saving = false; this.render(); }
    if (saved) await this.onSaved();
  }
}

globalThis.StallControls = StallControls;
