class ClickControls {
  constructor(api, onSaved) {
    this.api = api;
    this.onSaved = onSaved;
    this.mode = document.getElementById('clickMode');
    this.attempts = document.getElementById('clickAttempts');
    this.button = document.getElementById('saveClick');
    this.status = document.getElementById('clickGuardStatus');
    this.current = 2;
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
    const value = state.click_repeat_limit ?? 2;
    this.serverSaving = !!state.stall_setting_saving || !!state.model_switching;
    if (!this.dirty && !this.saving) {
      if (value !== this.current) this.notice = '';
      this.mode.value = value ? 'on' : 'off';
      if (value) this.attempts.value = value;
    }
    this.current = value;
    this.render();
  }

  render() {
    const busy = this.saving || this.serverSaving;
    this.mode.disabled = busy;
    this.attempts.disabled = busy || this.mode.value !== 'on';
    this.button.disabled = busy || !this.dirty;
    this.button.textContent = this.saving ? 'Saving…' : 'Apply click controls';
    this.status.textContent = this.saving ? 'Saving click controls…'
      : this.notice || (this.dirty ? 'Apply to the current and future tasks.'
        : this.current ? 'Allow ' + this.current + ' matching click attempts per similar desktop scene, then withhold repeats.'
        : 'Click guard off. The model can repeat clicks within the task budget.');
  }

  async save() {
    if (this.button.disabled) return;
    const value = this.mode.value === 'off' ? 0 : Number(this.attempts.value);
    if (!Number.isInteger(value) || value < (this.mode.value === 'off' ? 0 : 1) || value > 1000) {
      this.notice = 'Choose a whole number from 1 to 1000, or turn the guard off.';
      this.render(); return;
    }
    this.saving = true; this.notice = ''; this.render();
    let saved = false;
    try {
      const result = await this.api('/api/click-guard', {click_repeat_limit: value});
      if (result.click_repeat_limit !== value) throw new Error('The click setting was not confirmed.');
      this.current = value;
      this.dirty = false;
      this.notice = 'Saved. ' + (value ? 'Allow ' + value + ' matching click attempts per scene. Click history was reset.'
        : 'Click guard off. Pause/Stop remain under your control.');
      saved = true;
    } catch (failure) {
      this.notice = 'Could not save click controls: ' + failure.message;
    } finally { this.saving = false; this.render(); }
    if (saved) await this.onSaved();
  }
}

globalThis.ClickControls = ClickControls;
