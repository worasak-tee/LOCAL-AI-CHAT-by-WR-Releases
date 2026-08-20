(() => {
  if (window.__WR_LINK_ACTIONS_V2_LOADED__) return;
  window.__WR_LINK_ACTIONS_V2_LOADED__ = true;

  const messages = document.getElementById('messages');
  if (!messages) return;

  const style = document.createElement('style');
  style.textContent = `
    .bubble-body a{color:#0b63ce;text-decoration:underline;text-underline-offset:2px;overflow-wrap:anywhere}
    .bubble-body .wr-md-heading{display:block;font-weight:700;margin:7px 0 3px}
    .bubble-body .wr-md-line{min-height:1em}
    .link-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
    .link-action{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:7px 11px;border:1px solid #cfd7e3;border-radius:10px;background:#fff;color:#172033;text-decoration:none!important;font-size:12px;font-weight:600;line-height:1.2;cursor:pointer}
    .link-action:hover{background:#f5f8fc;border-color:#aebdd0}
    @media(max-width:640px){.link-action{min-height:32px;padding:6px 9px;font-size:11px}}
  `;
  document.head.appendChild(style);

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || '').trim());
      if (!['http:', 'https:'].includes(url.protocol)) return '';
      return url.href;
    } catch (_) {
      return '';
    }
  }

  function trimUrlPunctuation(value) {
    return String(value || '').replace(/[),.;!?\]\u0E2F\u0E46]+$/u, '');
  }

  function appendTextWithBold(parent, text) {
    const value = String(text || '');
    const boldRe = /\*\*([^*\n]+?)\*\*/g;
    let last = 0;
    let match;
    while ((match = boldRe.exec(value)) !== null) {
      if (match.index > last) parent.appendChild(document.createTextNode(value.slice(last, match.index)));
      const strong = document.createElement('strong');
      strong.textContent = match[1];
      parent.appendChild(strong);
      last = match.index + match[0].length;
    }
    if (last < value.length) parent.appendChild(document.createTextNode(value.slice(last)));
  }

  function appendInline(parent, text) {
    const value = String(text || '');
    const tokenRe = /\[([^\]\n]{1,120})\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"']+)/gi;
    let last = 0;
    let match;
    while ((match = tokenRe.exec(value)) !== null) {
      if (match.index > last) appendTextWithBold(parent, value.slice(last, match.index));
      const markdownLabel = match[1] || '';
      const rawUrl = trimUrlPunctuation(match[2] || match[3] || '');
      const href = safeHttpUrl(rawUrl);
      if (!href) {
        appendTextWithBold(parent, match[0]);
      } else {
        const link = document.createElement('a');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = markdownLabel || rawUrl;
        if (markdownLabel) link.dataset.wrLabel = markdownLabel;
        parent.appendChild(link);
        const rawToken = match[0];
        if (!markdownLabel && rawUrl.length < rawToken.length) {
          parent.appendChild(document.createTextNode(rawToken.slice(rawUrl.length)));
        }
      }
      last = match.index + match[0].length;
    }
    if (last < value.length) appendTextWithBold(parent, value.slice(last));
  }

  function renderMarkdown(body) {
    if (!body || body.dataset.wrMarkdownV2 === '1') return;
    body.dataset.wrMarkdownV2 = '1';
    const text = body.textContent || '';
    const fragment = document.createDocumentFragment();
    const lines = text.split(/\r?\n/);
    lines.forEach((raw) => {
      const line = document.createElement('div');
      line.className = 'wr-md-line';
      const heading = raw.match(/^\s*#{1,4}\s+(.+)$/);
      if (heading) {
        line.classList.add('wr-md-heading');
        appendInline(line, heading[1]);
      } else {
        appendInline(line, raw);
      }
      fragment.appendChild(line);
    });
    body.textContent = '';
    body.appendChild(fragment);
  }

  function cleanPlace(value) {
    return String(value || '')
      .replace(/^[\s\-*•]+/, '')
      .replace(/^[🗺️📍🚗🚙]+\s*/u, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function routeFromText(text) {
    const lines = String(text || '').split(/\r?\n/);
    for (const rawLine of lines.slice(0, 10)) {
      const line = rawLine.trim();
      if (!line || line.length > 260 || /https?:\/\//i.test(line)) continue;
      const match = line.match(/^(?:[🗺️📍🚗🚙]\s*)?(.+?)\s*(?:→|➡️|->)\s*(.+?)$/u);
      if (!match) continue;
      const origin = cleanPlace(match[1]);
      const destination = cleanPlace(match[2]);
      if (!origin || !destination || origin.length > 120 || destination.length > 120) continue;
      return { origin, destination };
    }
    return null;
  }

  function googleMapsDirections(origin, destination) {
    const url = new URL('https://www.google.com/maps/dir/');
    url.searchParams.set('api', '1');
    url.searchParams.set('origin', origin);
    url.searchParams.set('destination', destination);
    url.searchParams.set('travelmode', 'driving');
    return url.href;
  }

  function genericLabelForUrl(href) {
    try {
      const url = new URL(href);
      const host = url.hostname.toLowerCase();
      if ((host.includes('google.') && url.pathname.toLowerCase().includes('/maps')) || host === 'maps.app.goo.gl' || host.endsWith('.maps.app.goo.gl')) return '🗺️ เปิด Google Maps';
      if (host.includes('google.') && /search|images|imgres/.test(url.pathname + url.search)) return '🔎 Google รูปภาพ';
      if (host.endsWith('unsplash.com')) return '🖼️ Unsplash';
      if (host.endsWith('pexels.com')) return '🖼️ Pexels';
      return `🔗 ${url.hostname.replace(/^www\./, '')}`;
    } catch (_) {}
    return '🔗 เปิดลิงก์';
  }

  function actionLabel(anchor) {
    const explicit = String(anchor.dataset.wrLabel || '').trim();
    if (explicit) return `🔗 ${explicit}`;
    return genericLabelForUrl(anchor.href);
  }

  function addAction(box, href, label, seen) {
    const safe = safeHttpUrl(href);
    if (!safe || seen.has(safe) || seen.size >= 6) return;
    seen.add(safe);
    const link = document.createElement('a');
    link.className = 'link-action';
    link.href = safe;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = label;
    box.appendChild(link);
  }

  function processBubble(bubble) {
    if (!bubble || bubble.dataset.wrActionsV2 === '1') return;
    bubble.dataset.wrActionsV2 = '1';
    const body = bubble.querySelector('.bubble-body');
    if (!body) return;
    const originalText = body.textContent || '';
    renderMarkdown(body);

    const oldActions = bubble.querySelector('.link-actions');
    if (oldActions) oldActions.remove();

    const box = document.createElement('div');
    box.className = 'link-actions';
    const seen = new Set();

    body.querySelectorAll('a[href]').forEach((anchor) => {
      addAction(box, anchor.href, actionLabel(anchor), seen);
    });

    const route = routeFromText(originalText);
    if (route) {
      addAction(box, googleMapsDirections(route.origin, route.destination), '🗺️ เปิด Google Maps', seen);
    }

    if (box.children.length) bubble.appendChild(box);
  }

  function scan() {
    messages.querySelectorAll('.message-row.assistant .bubble').forEach(processBubble);
  }

  const observer = new MutationObserver(scan);
  observer.observe(messages, { childList: true, subtree: true });
  scan();
})();
