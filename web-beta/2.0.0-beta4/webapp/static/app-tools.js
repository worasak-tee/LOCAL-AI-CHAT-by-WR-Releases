(() => {
  if (window.__WR_FULL_ATTACHMENTS_V4__) return;
  window.__WR_FULL_ATTACHMENTS_V4__ = true;

  const $ = (id) => document.getElementById(id);
  const composer = $('composer');
  const messageInput = $('messageInput');
  const sourceSelect = $('sourceSelect');
  const chatPage = $('chatPage');
  if (!composer || !messageInput || !sourceSelect || !chatPage) return;

  const boot = window.WR_BOOT || {};
  const csrf = String(boot.csrf_token || '');
  const nativeFetch = window.fetch.bind(window);
  const MAX_FILES = 5;
  const MAX_FILE_BYTES = 50 * 1024 * 1024;
  let pending = [];
  let recognition = null;
  let listening = false;
  let staging = false;

  const style = document.createElement('style');
  style.textContent = `
    .composer-tool{width:38px;height:38px;flex:0 0 38px;border:1px solid #dfe3e8;border-radius:12px;background:#f6f7f9;color:#1f2937;display:grid;place-items:center;cursor:pointer;font-size:18px}
    .composer-tool:hover{background:#eceff3}.composer-tool:disabled{opacity:.42;cursor:not-allowed}.composer-tool.listening{background:#fee2e2;border-color:#fca5a5;color:#b91c1c;box-shadow:0 0 0 3px rgba(239,68,68,.10)}
    .pending-files{width:100%;display:flex;flex-wrap:wrap;gap:7px;margin:0 0 8px;padding:0 2px}.pending-files:empty{display:none}
    .pending-file{display:flex;align-items:center;gap:7px;max-width:360px;border:1px solid #dfe3e8;border-radius:10px;background:#f8fafc;padding:6px 8px;font-size:11px;color:#334155}
    .pending-file-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:190px}.pending-file button,.pending-file a{border:0;background:transparent;color:#2563eb;cursor:pointer;padding:1px 3px;font-size:11px;text-decoration:none}.pending-file .remove-file{color:#64748b;font-size:15px}
    .chat-page.wr-drag-active::after{content:'วางไฟล์เพื่อแนบ';position:absolute;inset:12px;border:2px dashed #7c8da8;border-radius:16px;background:rgba(248,250,252,.94);display:grid;place-items:center;z-index:40;font-weight:700;color:#334155;pointer-events:none}
    .attachment-preview{display:inline-flex;align-items:center;justify-content:center;min-height:30px;padding:5px 9px;border:1px solid #d7dee8;border-radius:8px;text-decoration:none;color:#253650;font-size:11px;margin-left:auto}.attachment-preview+.attachment-download{margin-left:6px}
    @media(max-width:640px){.composer-tool{width:34px;height:34px;flex-basis:34px}.pending-file{max-width:92vw}.pending-file-name{max-width:42vw}}
  `;
  document.head.appendChild(style);

  const wrap = composer.parentElement;
  const fileList = document.createElement('div');
  fileList.id = 'pendingFiles';
  fileList.className = 'pending-files';
  wrap.insertBefore(fileList, composer);

  const attachBtn = document.createElement('button');
  attachBtn.type = 'button';
  attachBtn.id = 'attachFileBtn';
  attachBtn.className = 'composer-tool';
  attachBtn.textContent = '＋';
  attachBtn.title = 'แนบไฟล์';
  attachBtn.setAttribute('aria-label', 'แนบไฟล์');

  const micBtn = document.createElement('button');
  micBtn.type = 'button';
  micBtn.id = 'voiceInputBtn';
  micBtn.className = 'composer-tool';
  micBtn.textContent = '🎤';
  micBtn.title = 'พิมพ์ด้วยเสียง';
  micBtn.setAttribute('aria-label', 'พิมพ์ด้วยเสียง');

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.multiple = true;
  fileInput.hidden = true;
  fileInput.id = 'chatFileInput';

  composer.insertBefore(attachBtn, messageInput);
  composer.insertBefore(micBtn, messageInput);
  composer.appendChild(fileInput);

  function toast(text) {
    const old = document.querySelector('.toast');
    if (old) old.remove();
    const div = document.createElement('div');
    div.className = 'toast';
    div.textContent = String(text || '');
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 4200);
  }

  function formatBytes(n) {
    const size = Number(n || 0);
    if (size < 1024) return `${size} B`;
    if (size < 1048576) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1048576).toFixed(1)} MB`;
  }

  function iconFor(file) {
    const type = String(file.content_type || file.type || '').toLowerCase();
    const name = String(file.name || '').toLowerCase();
    if (type.startsWith('image/')) return '🖼️';
    if (type === 'application/pdf' || name.endsWith('.pdf')) return '📄';
    if (name.endsWith('.zip')) return '🗜️';
    return '📎';
  }

  function tokens() {
    return pending.map((item) => item.token).filter(Boolean);
  }

  function renderFiles() {
    fileList.innerHTML = '';
    pending.forEach((item) => {
      const chip = document.createElement('div');
      chip.className = 'pending-file';
      const icon = document.createElement('b');
      icon.textContent = iconFor(item);
      const name = document.createElement('span');
      name.className = 'pending-file-name';
      name.textContent = `${item.name} · ${formatBytes(item.size)}`;
      name.title = item.name;
      const view = document.createElement('a');
      view.href = item.preview_url;
      view.target = '_blank';
      view.rel = 'noopener noreferrer';
      view.textContent = 'View';
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove-file';
      remove.textContent = '×';
      remove.title = 'Remove';
      remove.addEventListener('click', async () => {
        try {
          await nativeFetch(`/api/attachments/stage/${encodeURIComponent(item.token)}`, {
            method: 'DELETE',
            headers: csrf ? { 'X-CSRF-Token': csrf } : {},
            cache: 'no-store',
          });
        } catch (_) {}
        pending = pending.filter((row) => row.token !== item.token);
        renderFiles();
      });
      chip.append(icon, name, view, remove);
      fileList.appendChild(chip);
    });
  }

  async function stageFiles(files) {
    const incoming = Array.from(files || []).filter(Boolean);
    if (!incoming.length || staging) return;
    const slots = Math.max(0, MAX_FILES - pending.length);
    if (!slots) return toast(`แนบได้สูงสุด ${MAX_FILES} ไฟล์ต่อข้อความ`);
    const selected = incoming.slice(0, slots);
    for (const file of selected) {
      if (Number(file.size || 0) > MAX_FILE_BYTES) return toast(`${file.name || 'ไฟล์'}: ขนาดไฟล์สูงสุด 50 MB`);
    }
    staging = true;
    attachBtn.disabled = true;
    try {
      const form = new FormData();
      selected.forEach((file) => form.append('files', file, file.name || 'attachment.bin'));
      const response = await nativeFetch('/api/attachments/stage', {
        method: 'POST',
        headers: csrf ? { 'X-CSRF-Token': csrf } : {},
        body: form,
        cache: 'no-store',
      });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      if (response.status === 401) {
        location.href = '/login';
        return;
      }
      if (!response.ok) throw new Error(String(data.detail || data.error || `HTTP ${response.status}`));
      pending.push(...(Array.isArray(data.files) ? data.files : []));
      renderFiles();
      if (incoming.length > selected.length) toast(`รับ ${selected.length} ไฟล์ · สูงสุด ${MAX_FILES} ไฟล์ต่อข้อความ`);
    } catch (error) {
      toast(`แนบไฟล์ไม่สำเร็จ: ${error.message}`);
    } finally {
      staging = false;
      attachBtn.disabled = false;
      fileInput.value = '';
    }
  }

  function clipboardFiles(event) {
    const data = event.clipboardData;
    if (!data) return [];
    const result = [];
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const files = Array.from(data.files || []);
    if (files.length) return files;
    for (const item of Array.from(data.items || [])) {
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (!file) continue;
      if (file.name && file.name !== 'image.png') result.push(file);
      else {
        const ext = String(file.type || '').includes('jpeg') ? 'jpg' : String(file.type || '').split('/')[1] || 'bin';
        result.push(new File([file], `clipboard-${stamp}.${ext}`, { type: file.type || 'application/octet-stream' }));
      }
    }
    return result;
  }

  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => stageFiles(fileInput.files));
  document.addEventListener('paste', (event) => {
    const files = clipboardFiles(event);
    if (!files.length) return;
    event.preventDefault();
    stageFiles(files);
  });

  ['dragenter', 'dragover'].forEach((name) => {
    chatPage.addEventListener(name, (event) => {
      if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes('Files')) return;
      event.preventDefault();
      chatPage.classList.add('wr-drag-active');
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    });
  });
  ['dragleave', 'dragend'].forEach((name) => chatPage.addEventListener(name, () => chatPage.classList.remove('wr-drag-active')));
  chatPage.addEventListener('drop', (event) => {
    chatPage.classList.remove('wr-drag-active');
    if (!event.dataTransfer?.files?.length) return;
    event.preventDefault();
    stageFiles(event.dataTransfer.files);
  });

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.lang = 'th-TH';
    recognition.continuous = false;
    recognition.interimResults = true;
    let baseText = '';
    recognition.onstart = () => {
      listening = true;
      baseText = messageInput.value.trim();
      micBtn.classList.add('listening');
      micBtn.textContent = '⏹';
      micBtn.title = 'หยุดพิมพ์ด้วยเสียง';
    };
    recognition.onresult = (event) => {
      let transcript = '';
      for (let i = event.resultIndex; i < event.results.length; i++) transcript += event.results[i][0].transcript;
      messageInput.value = [baseText, transcript.trim()].filter(Boolean).join(baseText ? ' ' : '');
      messageInput.dispatchEvent(new Event('input', { bubbles: true }));
    };
    recognition.onerror = (event) => { if (event.error !== 'aborted') toast(`Voice typing: ${event.error}`); };
    recognition.onend = () => {
      listening = false;
      micBtn.classList.remove('listening');
      micBtn.textContent = '🎤';
      micBtn.title = 'พิมพ์ด้วยเสียง';
      messageInput.focus();
    };
    micBtn.addEventListener('click', () => {
      try { if (listening) recognition.stop(); else recognition.start(); } catch (_) {}
    });
  } else {
    micBtn.disabled = true;
    micBtn.title = 'Browser นี้ไม่รองรับ Voice Typing';
  }

  function ensureAttachmentOnlyText() {
    if (pending.length && !messageInput.value.trim()) {
      messageInput.value = `📎 ${pending.map((item) => item.name).join(', ')}`.slice(0, 10000);
      messageInput.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
  composer.addEventListener('submit', ensureAttachmentOnlyText, true);
  messageInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) ensureAttachmentOnlyText();
  }, true);

  async function commitFiles(data, sendTokens) {
    if (!data?.chat_id || !data?.user_created_at || !sendTokens.length) return [];
    const response = await nativeFetch('/api/attachments/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
      body: JSON.stringify({ chat_id: data.chat_id, user_created_at: data.user_created_at, tokens: sendTokens }),
      cache: 'no-store',
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(String(payload.detail || payload.error || `HTTP ${response.status}`));
    return Array.isArray(payload.attachments) ? payload.attachments : [];
  }

  function addViewButton(card) {
    if (!card || card.querySelector('.attachment-preview')) return;
    const download = card.querySelector('a.attachment-download[href*="/api/attachments/"]');
    if (!download) return;
    const match = String(download.getAttribute('href') || '').match(/\/api\/attachments\/(\d+)\/download/);
    if (!match) return;
    const view = document.createElement('a');
    view.className = 'attachment-preview';
    view.href = `/api/attachments/${match[1]}/preview`;
    view.target = '_blank';
    view.rel = 'noopener noreferrer';
    view.textContent = 'View';
    card.insertBefore(view, download);
  }

  function scanAttachmentCards() {
    document.querySelectorAll('.attachment-card').forEach(addViewButton);
  }

  function decorateLatestUserBubble(attachments) {
    if (!Array.isArray(attachments) || !attachments.length) return;
    const bubbles = document.querySelectorAll('.message-row.user .bubble');
    const bubble = bubbles[bubbles.length - 1];
    if (!bubble) return;
    const old = bubble.querySelector('.attachment-list');
    if (old) old.remove();
    const box = document.createElement('div');
    box.className = 'attachment-list';
    attachments.forEach((att) => {
      const card = document.createElement('div');
      card.className = 'attachment-card';
      const info = document.createElement('div');
      info.className = 'attachment-info';
      const icon = document.createElement('span');
      icon.className = 'attachment-icon';
      icon.textContent = iconFor(att);
      const text = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = att.name || 'Attachment';
      const meta = document.createElement('small');
      meta.textContent = [formatBytes(att.size), att.source || 'User Upload'].filter(Boolean).join(' · ');
      text.append(name, meta);
      info.append(icon, text);
      const view = document.createElement('a');
      view.className = 'attachment-preview';
      view.href = att.preview_url || String(att.download_url || '').replace(/\/download$/, '/preview');
      view.target = '_blank';
      view.rel = 'noopener noreferrer';
      view.textContent = 'View';
      const download = document.createElement('a');
      download.className = 'attachment-download';
      download.href = att.download_url || '#';
      download.download = att.name || '';
      download.textContent = 'Download';
      card.append(info, view, download);
      box.appendChild(card);
    });
    bubble.appendChild(box);
  }

  window.fetch = async function(input, init = {}) {
    const url = typeof input === 'string' ? input : String(input?.url || '');
    const method = String(init.method || 'GET').toUpperCase();
    const isChatSend = url.endsWith('/api/chat/send') && method === 'POST';
    const sendTokens = isChatSend ? tokens() : [];
    const response = await nativeFetch(input, init);
    if (isChatSend && response.ok && sendTokens.length) {
      try {
        const data = await response.clone().json();
        const attachments = await commitFiles(data, sendTokens);
        decorateLatestUserBubble(attachments);
        pending = pending.filter((item) => !sendTokens.includes(item.token));
        renderFiles();
      } catch (error) {
        toast(`ส่งข้อความสำเร็จ แต่ผูกไฟล์กับ Chat ไม่สำเร็จ: ${error.message}`);
      }
    }
    return response;
  };

  const observer = new MutationObserver(scanAttachmentCards);
  observer.observe(document.body, { childList: true, subtree: true });
  scanAttachmentCards();

  window.WR_ATTACHMENTS = {
    pending: () => pending.slice(),
    tokens,
    hasPending: () => pending.length > 0,
    stageFiles,
  };

  if (!window.__WR_PROFILE_RENAME_LOADED__ && !document.querySelector('script[data-wr-profile-rename]')) {
    const profileScript = document.createElement('script');
    profileScript.src = '/static/app-profile-rename.js?v=beta4-attachments';
    profileScript.defer = true;
    profileScript.dataset.wrProfileRename = '1';
    document.head.appendChild(profileScript);
  }
})();
