// Header search for the offline wiki. Works over file:// — indexes are loaded
// as <script> files (fetch() is blocked for local files in most browsers).
(function () {
  const base = document.currentScript.src.replace(/[^/]*$/, ''); // site root
  const input = document.getElementById('search');
  const box = document.getElementById('search-results');
  let texts = null, loading = false, active = -1, hits = [];
  let trackTimer, lastTracked = '';

  // Report what people search for once they stop typing; empty results are
  // reported separately as they show what the wiki is missing.
  function trackQuery(q, count) {
    clearTimeout(trackTimer);
    if (q.length < 3) return;
    trackTimer = setTimeout(() => {
      if (q === lastTracked || !window.gkTrack) return;
      lastTracked = q;
      window.gkTrack(`search/${q}`, `Search: ${q}`);
      if (!count && texts) window.gkTrack(`search-empty/${q}`, `Search, nothing found: ${q}`);
    }, 1500);
  }

  function loadScript(src, cb) {
    const s = document.createElement('script');
    s.src = base + src;
    s.onload = cb;
    document.head.appendChild(s);
  }
  function loadText() {
    if (texts || loading) return;
    loading = true;
    loadScript('search-text.js', () => {
      texts = window.GK_TEXT.map(p => [p[0], p[1], p[2], p[2].toLowerCase()]);
      if (input.value.trim()) run();
    });
  }

  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  function mark(s, words) {
    let out = esc(s);
    for (const w of words) {
      if (w.length < 2) continue;
      out = out.replace(new RegExp(esc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`);
    }
    return out;
  }

  function run() {
    const q = input.value.trim().toLowerCase();
    if (!q) { box.hidden = true; return; }
    const words = q.split(/\s+/);
    const scored = new Map();
    for (const [title, href] of window.GK_TITLES) {
      const t = title.toLowerCase();
      if (!words.every(w => t.includes(w))) continue;
      const score = t === q ? 1000 : t.startsWith(q) ? 500 : t.includes(q) ? 300 : 200;
      const prev = scored.get(href);
      // Several titles (redirects) may point at the same page; keep the best.
      if (!prev || prev.score < score) scored.set(href, { title, href, score, snippet: '' });
    }
    if (texts) {
      for (const [title, href, text, lower] of texts) {
        if (!words.every(w => lower.includes(w))) continue;
        const i = lower.indexOf(q) >= 0 ? lower.indexOf(q) : lower.indexOf(words[0]);
        const start = Math.max(0, i - 50);
        const snippet = (start ? '…' : '') + text.slice(start, i + 110) + '…';
        let count = 0;
        for (const w of words) count += lower.split(w).length - 1;
        const hit = scored.get(href);
        if (hit) { hit.score += 50; hit.snippet = hit.snippet || snippet; }
        else scored.set(href, { title, href, score: Math.min(count, 100), snippet });
      }
    }
    hits = [...scored.values()].sort((a, b) => b.score - a.score || a.title.localeCompare(b.title)).slice(0, 40);
    active = hits.length ? 0 : -1;
    box.innerHTML = hits.length
      ? hits.map((h, i) => `<a href="${base}wiki/${h.href}" class="${i === active ? 'active' : ''}">
          <b>${mark(h.title, words)}</b>${h.snippet ? `<small>${mark(h.snippet, words)}</small>` : ''}</a>`).join('')
        + (texts ? '' : '<div class="hint">Loading full-text index…</div>')
      : `<div class="hint">${texts ? 'Nothing found' : 'Loading full-text index…'}</div>`;
    box.hidden = false;
    trackQuery(q, hits.length);
  }

  function move(d) {
    const links = box.querySelectorAll('a');
    if (!links.length) return;
    links[active]?.classList.remove('active');
    active = (active + d + links.length) % links.length;
    links[active].classList.add('active');
    links[active].scrollIntoView({ block: 'nearest' });
  }

  loadScript('search-titles.js', () => { if (input.value.trim()) run(); });
  input.addEventListener('focus', () => { loadText(); if (input.value.trim()) run(); });
  input.addEventListener('input', run);
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter' && active >= 0) { location.href = box.querySelectorAll('a')[active].href; }
    else if (e.key === 'Escape') { box.hidden = true; input.blur(); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== input) { e.preventDefault(); input.focus(); input.select(); }
  });
  document.addEventListener('click', e => { if (!e.target.closest('.search')) box.hidden = true; });
})();
