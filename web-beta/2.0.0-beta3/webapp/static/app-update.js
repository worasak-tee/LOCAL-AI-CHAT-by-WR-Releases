(() => {
  if (window.__WR_WEB_UPDATE_UI__) return;
  window.__WR_WEB_UPDATE_UI__ = true;

  const boot = window.WR_BOOT || {};
  if (String(boot.role || '').trim().toLowerCase() !== 'admin') return;

  const setupPage = document.getElementById('setupPage');
  const setupForm = document.getElementById('setupForm');
  if (!setupPage || !setupForm) return;

  const csrf = String(boot.csrf_token || '');
  let current = String(boot.version || '');
  let latest = '';
  let activeJob = '';
  let pollTimer = null;

  const style = document.createElement('style');
  style.textContent = `
    .software-update-card{margin-top:16px}.software-update-grid{display:grid;grid-template-columns:minmax(150px,190px) 1fr;gap:0;border-top:1px solid #e6e9ee}
    .software-update-row{display:contents}.software-update-row>span,.software-update-row>strong{padding:11px 0;border-bottom:1px solid #e6e9ee}.software-update-row>strong{font-weight:600;overflow-wrap:anywhere}
    .software-update-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:14px;flex-wrap:wrap}.software-update-notes{margin:12px 0 0;padding:10px 12px;border:1px solid #e5e7eb;border-radius:10px;background:#f8fafc;font-size:12px;line-height:1.55}.software-update-notes:empty{display:none}
    .software-update-progress{height:7px;border-radius:999px;background:#e8edf3;overflow:hidden;margin-top:10px}.software-update-progress>i{display:block;height:100%;width:0;background:currentColor;transition:width .25s ease}
    .update-ok{color:#087a3c}.update-warn{color:#9a6500}.update-error{color:#b42318}
    @media(max-width:640px){.software-update-grid{grid-template-columns:1fr}.software-update-row{display:block;border-bottom:1px solid #e6e9ee;padding:8px 0}.software-update-row>span,.software-update-row>strong{display:block;border:0;padding:2px 0}.software-update-actions>*{flex:1}}
  `;
  document.head.appendChild(style);

  const card = document.createElement('div');
  card.className = 'panel-card software-update-card';
  card.innerHTML = `
    <div class="panel-title"><div><h2>Software Update</h2><p>เฉพาะ System Administrator · Update จาก GitHub Release Channel</p></div></div>
    <div class="software-update-grid">
      <div class="software-update-row"><span>Current Version</span><strong id="wrUpdateCurrent">${escapeHtml(current || '-')}</strong></div>
      <div class="software-update-row"><span>Latest Version</span><strong id="wrUpdateLatest">-</strong></div>
      <div class="software-update-row"><span>Channel</span><strong id="wrUpdateChannel">Beta</strong></div>
      <div class="software-update-row"><span>Status</span><strong id="wrUpdateStatus">ยังไม่ได้ตรวจสอบ</strong></div>
    </div>
    <div id="wrUpdateNotes" class="software-update-notes"></div>
    <div class="software-update-progress"><i id="wrUpdateProgress"></i></div>
    <div class="software-update-actions">
      <button id="wrCheckUpdate" class="secondary-btn" type="button">Check Update</button>
      <button id="wrInstallUpdate" class="primary-inline" type="button" disabled>Update Now</button>
    </div>
    <p class="field-note">ระบบจะ Download → Git Integrity Verify → Backup → Stop Service → Update → Start → Health Check และ Rollback อัตโนมัติถ้าไม่ผ่าน</p>
  `;
  setupForm.insertAdjacentElement('afterend', card);

  const $ = (id) => document.getElementById(id);
  const currentEl = $('wrUpdateCurrent');
  const latestEl = $('wrUpdateLatest');
  const channelEl = $('wrUpdateChannel');
  const statusEl = $('wrUpdateStatus');
  const notesEl = $('wrUpdateNotes');
  const progressEl = $('wrUpdateProgress');
  const checkBtn = $('wrCheckUpdate');
  const installBtn = $('wrInstallUpdate');

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  }

  function setStatus(text, kind = '') {
    statusEl.textContent = String(text || '');
    statusEl.className = kind ? `update-${kind}` : '';
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (csrf && !['GET', 'HEAD'].includes(String(options.method || 'GET').toUpperCase())) headers.set('X-CSRF-Token', csrf);
    const response = await fetch(url, { ...options, headers, cache: 'no-store' });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(String(data.detail || data.error || `HTTP ${response.status}`));
    return data;
  }

  function renderNotes(notes) {
    notesEl.textContent = '';
    if (!Array.isArray(notes) || !notes.length) return;
    notes.forEach((item) => {
      const row = document.createElement('div');
      row.textContent = `• ${String(item || '')}`;
      notesEl.appendChild(row);
    });
  }

  async function checkUpdate(silent = false) {
    if (!silent) setStatus('กำลังตรวจสอบ...', 'warn');
    checkBtn.disabled = true;
    try {
      const data = await api('/api/update/check');
      current = String(data.current_version || current || '');
      latest = String(data.latest_version || '');
      currentEl.textContent = current || '-';
      latestEl.textContent = latest || '-';
      channelEl.textContent = String(data.channel || 'beta');
      renderNotes(data.notes);
      installBtn.disabled = !data.update_available;
      progressEl.style.width = '0%';
      if (data.update_available) setStatus(`มีเวอร์ชันใหม่ ${latest}`, 'warn');
      else setStatus('เป็นเวอร์ชันล่าสุดแล้ว', 'ok');
      return data;
    } catch (error) {
      installBtn.disabled = true;
      setStatus(`ตรวจสอบ Update ไม่สำเร็จ: ${error.message}`, 'error');
      return null;
    } finally {
      checkBtn.disabled = false;
    }
  }

  function schedulePoll(delay = 1800) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollJob, delay);
  }

  async function pollJob() {
    if (!activeJob) return;
    try {
      const data = await api(`/api/update/status/${encodeURIComponent(activeJob)}`);
      const progress = Math.max(0, Math.min(100, Number(data.progress || 0)));
      progressEl.style.width = `${progress}%`;
      const stage = String(data.stage || data.status || 'Updating');
      const message = String(data.message || '');
      setStatus(`${stage}${message ? ` · ${message}` : ''}`, data.status === 'failed' ? 'error' : data.status === 'success' ? 'ok' : 'warn');
      if (data.status === 'success') {
        current = String(data.current_version || data.target_version || latest || current);
        currentEl.textContent = current;
        latestEl.textContent = current;
        installBtn.disabled = true;
        progressEl.style.width = '100%';
        setTimeout(() => location.reload(), 1600);
        return;
      }
      if (data.status === 'failed') {
        installBtn.disabled = false;
        return;
      }
      schedulePoll(1700);
    } catch (_) {
      setStatus('Web Service กำลัง Restart... รอเชื่อมต่อกลับ', 'warn');
      schedulePoll(2200);
    }
  }

  checkBtn.addEventListener('click', () => checkUpdate(false));
  installBtn.addEventListener('click', async () => {
    if (!latest) return;
    const yes = window.confirm(`Update LOCAL AI Web จาก ${current || '-'} เป็น ${latest}?\n\nระบบจะ Backup และ Rollback อัตโนมัติหาก Health Check ไม่ผ่าน`);
    if (!yes) return;
    installBtn.disabled = true;
    checkBtn.disabled = true;
    progressEl.style.width = '2%';
    setStatus('กำลังเริ่ม Updater Helper...', 'warn');
    try {
      const data = await api('/api/update/install', { method: 'POST' });
      if (!data.started) {
        setStatus(String(data.message || 'เป็นเวอร์ชันล่าสุดแล้ว'), 'ok');
        checkBtn.disabled = false;
        return;
      }
      activeJob = String(data.job_id || '');
      if (!activeJob) throw new Error('Updater did not return a job id');
      schedulePoll(900);
    } catch (error) {
      installBtn.disabled = false;
      checkBtn.disabled = false;
      setStatus(`เริ่ม Update ไม่สำเร็จ: ${error.message}`, 'error');
    }
  });

  setTimeout(() => checkUpdate(true), 1000);
})();
