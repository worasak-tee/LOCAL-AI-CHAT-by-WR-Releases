(() => {
  const boot = window.WR_BOOT || {};
  const $ = (id) => document.getElementById(id);
  const form = $('setupForm');
  if (!form) return;

  const modeSelect = $('setupAuthMode');
  const testBtn = $('testConnectionBtn');
  const saveBtn = $('saveSetupBtn');
  const status = $('setupStatus');
  const csrf = boot.csrf_token || '';
  const bootAdmin = !!boot.can_manage_setup ||
    String(boot.ai_access_level || '').toLowerCase() === 'admin' ||
    String(boot.role || '').toLowerCase() === 'admin';

  let authState = {
    auth_mode: boot.auth_mode || 'none',
    client_id: boot.client_id || '',
    basic_username: boot.basic_username || '',
    has_secret: !!boot.has_secret,
    can_edit: bootAdmin,
  };

  function errorText(value) {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      return value.map((item) => item?.msg || item?.message || JSON.stringify(item)).join(' | ');
    }
    if (value && typeof value === 'object') {
      return value.msg || value.message || JSON.stringify(value);
    }
    return String(value || 'Unknown error');
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase()) && csrf) {
      headers.set('X-CSRF-Token', csrf);
    }
    const response = await fetch(url, { ...options, headers, cache: 'no-store' });
    if (response.status === 401) {
      location.href = '/login';
      throw new Error('Login required');
    }
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = errorText(data.detail ?? data.error ?? data);
      } catch (_) {}
      throw new Error(detail);
    }
    return response.json();
  }

  function setStatus(text, kind = '') {
    status.className = `status-line${kind ? ` ${kind}` : ''}`;
    status.textContent = errorText(text);
  }

  function authPayload() {
    return {
      auth_mode: modeSelect.value || 'none',
      client_id: $('setupCfClientId').value.trim(),
      client_secret: $('setupCfClientSecret').value,
      bearer_token: $('setupBearerToken').value,
      username: $('setupBasicUsername').value.trim(),
      password: $('setupBasicPassword').value,
    };
  }

  function fullPayload() {
    return {
      web_port: Number($('setupPortInput').value || 9780),
      ollama_url: $('setupOllamaInput').value.trim(),
      onsite_url: $('setupOnsiteInput').value.trim(),
      default_model: $('modelSelect')?.value || boot.default_model || '',
      ...authPayload(),
    };
  }

  function renderAuth() {
    const mode = modeSelect.value || 'none';
    $('authCloudflareFields').hidden = mode !== 'cloudflare';
    $('authBearerFields').hidden = mode !== 'bearer';
    $('authBasicFields').hidden = mode !== 'basic';

    const saved = $('authSavedNote');
    if (authState.has_secret && mode === authState.auth_mode && mode !== 'none') {
      saved.textContent = 'มี Secret บันทึกอยู่แล้ว · เว้นช่อง Secret/Password ว่างเพื่อใช้ค่าเดิม';
    } else if (mode === 'none') {
      saved.textContent = 'ไม่ใช้ Authentication — เหมาะกับ Ollama ที่อยู่ในเครื่อง/เครือข่ายที่ไว้ใจได้';
    } else {
      saved.textContent = 'กรอก Credential สำหรับการเชื่อมต่อ Ollama';
    }

    const editable = !!authState.can_edit;
    [
      'setupAuthMode', 'setupCfClientId', 'setupCfClientSecret', 'setupBearerToken',
      'setupBasicUsername', 'setupBasicPassword'
    ].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = !editable;
    });
  }

  function applySetup(data) {
    authState = {
      auth_mode: data.auth_mode || 'none',
      client_id: data.client_id || '',
      basic_username: data.basic_username || '',
      has_secret: !!data.has_secret,
      can_edit: data.can_edit === true || bootAdmin,
    };
    modeSelect.value = authState.auth_mode;
    $('setupCfClientId').value = authState.client_id;
    $('setupBasicUsername').value = authState.basic_username;
    $('setupCfClientSecret').value = '';
    $('setupBearerToken').value = '';
    $('setupBasicPassword').value = '';
    $('setupCfClientSecret').placeholder = authState.has_secret && authState.auth_mode === 'cloudflare' ? 'Saved — leave blank to keep' : '';
    $('setupBearerToken').placeholder = authState.has_secret && authState.auth_mode === 'bearer' ? 'Saved — leave blank to keep' : '';
    $('setupBasicPassword').placeholder = authState.has_secret && authState.auth_mode === 'basic' ? 'Saved — leave blank to keep' : '';
    renderAuth();
  }

  async function loadAuthSetup() {
    try {
      const data = await api('/api/setup');
      applySetup(data);
    } catch (e) {
      authState.can_edit = bootAdmin;
      renderAuth();
      setStatus(e?.message || e, 'offline');
    }
  }

  modeSelect.addEventListener('change', (event) => {
    event.stopImmediatePropagation();
    renderAuth();
  }, true);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!authState.can_edit) return setStatus('Admin permission is required', 'offline');
    saveBtn.disabled = true;
    setStatus('Saving Setup and Ollama Authentication...');
    try {
      const data = await api('/api/setup', {
        method: 'POST',
        body: JSON.stringify(fullPayload()),
      });
      applySetup({ ...data, can_edit: true });
      boot.ollama_url = data.ollama_url;
      boot.onsite_url = data.onsite_url;
      boot.web_port = data.web_port;
      boot.default_model = data.default_model;
      boot.auth_mode = data.auth_mode;
      boot.client_id = data.client_id || '';
      boot.basic_username = data.basic_username || '';
      boot.has_secret = !!data.has_secret;
      if (data.restart_required) {
        setStatus(`บันทึกแล้ว · Internal Web Port ใหม่ ${data.web_port} จะมีผลหลัง Restart Service`, 'warning');
      } else {
        setStatus(`Setup saved · Ollama Auth: ${data.auth_mode || 'none'}`, 'online');
      }
      const refreshBtn = $('refreshModelsBtn');
      if (refreshBtn) refreshBtn.click();
    } catch (e) {
      setStatus(e?.message || e, 'offline');
    } finally {
      saveBtn.disabled = !authState.can_edit;
    }
  }, true);

  testBtn.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!authState.can_edit) return setStatus('Admin permission is required', 'offline');
    testBtn.disabled = true;
    setStatus('Testing Ollama URL + Authentication...');
    try {
      const payload = {
        ollama_url: $('setupOllamaInput').value.trim(),
        ...authPayload(),
      };
      const data = await api('/api/setup/test', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (data.online) {
        setStatus(`Ollama online · Auth ${data.auth_mode || 'none'} · ${data.models.length} model(s)`, 'online');
      } else {
        setStatus(data.error || 'Ollama offline', 'offline');
      }
    } catch (e) {
      setStatus(e?.message || e, 'offline');
    } finally {
      testBtn.disabled = !authState.can_edit;
    }
  }, true);

  renderAuth();
  loadAuthSetup();
})();

// Beta 5 self-healing extension loader.
(() => {
  if (window.__WR_BETA5_EXTENSION_LOADER__) return;
  window.__WR_BETA5_EXTENSION_LOADER__ = true;
  const version = '2.0.0-beta5';

  function loadScript(src, marker, guardName) {
    if (guardName && window[guardName]) return;
    if (document.querySelector(`script[${marker}]`)) return;
    const script = document.createElement('script');
    script.src = `${src}?v=${encodeURIComponent(version)}`;
    script.async = false;
    script.defer = true;
    script.setAttribute(marker, '1');
    document.head.appendChild(script);
  }

  loadScript('/static/app-tools.js', 'data-wr-beta5-tools', '__WR_FULL_ATTACHMENTS_V4__');
  loadScript('/static/app-links.js', 'data-wr-beta5-links', '__WR_LINK_RENDERER_V2__');
  loadScript('/static/app-update.js', 'data-wr-beta5-update', '__WR_WEB_UPDATE_UI__');
})();
