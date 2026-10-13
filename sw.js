// Service worker: app shell on install, everything else cached as it is
// visited, plus an on-demand "download the whole wiki" job driven from
// index.html (the full site is ~70 MB, so it is not forced on install).
const CACHE = 'gk-wiki-v3';
const SHELL = [
  './', 'index.html', 'cart.html', 'style.css', 'search.js', 'app.js', 'cart.js',
  'data.js', 'search-titles.js', 'search-text.js', 'manifest.webmanifest',
  'images/ItemSprite.png', 'icons/icon-192.png', 'icons/logo.png',
];
const scoped = p => new URL(p, self.registration.scope).href;

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL.map(p => new Request(scoped(p), { cache: 'reload' }))))
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

async function fromNetwork(request, cache) {
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== location.origin) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(request, { ignoreSearch: true });
    if (url.pathname.includes('/images/')) {
      // Images never change between builds: cache first.
      return cached || fromNetwork(request, cache);
    }
    // Pages, scripts, data: serve the cached copy at once and refresh it in
    // the background so a rebuilt site shows up on the next visit.
    const refresh = fromNetwork(request, cache).catch(() => null);
    if (cached) {
      event.waitUntil(refresh);
      return cached;
    }
    const response = await refresh;
    if (response) return response;
    if (request.mode === 'navigate') {
      return new Response(
        '<!doctype html><meta name="viewport" content="width=device-width"><title>Offline</title>' +
        '<body style="font:16px system-ui;padding:24px">This page is not saved for offline use yet. ' +
        `<a href="${scoped('index.html')}">Back to the wiki</a> and use “Download everything for offline”.`,
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
    return Response.error();
  })());
});

// ---------- full offline download ----------
let job = null;

async function fileList() {
  const cache = await caches.open(CACHE);
  try {
    return await (await fromNetwork(scoped('offline-files.json'), cache)).json();
  } catch (e) {
    const cached = await cache.match(scoped('offline-files.json'));
    return cached ? cached.json() : { files: [] };
  }
}

async function status() {
  const [{ files }, cache] = await Promise.all([fileList(), caches.open(CACHE)]);
  const have = new Set((await cache.keys()).map(r => r.url));
  return { type: 'offline-status', total: files.length, cached: files.filter(f => have.has(scoped(f))).length };
}

async function broadcast(msg) {
  for (const client of await self.clients.matchAll()) client.postMessage(msg);
}

async function downloadAll(refresh) {
  const [{ files }, cache] = await Promise.all([fileList(), caches.open(CACHE)]);
  const have = new Set((await cache.keys()).map(r => r.url));
  // Images are immutable, so a refresh only re-fetches pages and data.
  const todo = files.filter(f => !have.has(scoped(f)) || (refresh && !f.startsWith('images/')));
  let done = files.length - todo.length, failed = 0, next = 0;
  const worker = async () => {
    while (next < todo.length) {
      const url = scoped(todo[next++]);
      // GitHub Pages and similar hosts answer 503/429 under bursts: retry with backoff.
      let ok = false;
      for (let attempt = 0; attempt < 4 && !ok; attempt++) {
        if (attempt) await new Promise(r => setTimeout(r, 1000 * 2 ** attempt));
        try {
          const r = await fetch(url, { cache: 'no-cache' });
          if (!r.ok) throw new Error(r.status);
          await cache.put(url, r);
          ok = true;
        } catch (e) { /* retry */ }
      }
      if (!ok) failed++;
      done++;
      if (done % 25 === 0) broadcast({ type: 'offline-progress', done, total: files.length, failed });
    }
  };
  await Promise.all(Array.from({ length: 4 }, worker));
  broadcast({ type: 'offline-done', failed, total: files.length });
}

self.addEventListener('message', event => {
  const { type, refresh } = event.data || {};
  if (type === 'offline-status') {
    event.waitUntil(status().then(s => event.source.postMessage(s)));
  } else if (type === 'offline-download') {
    if (!job) job = downloadAll(refresh).finally(() => { job = null; });
    event.waitUntil(job);
  }
});
