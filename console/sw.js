/* Service worker — shell only, and network-first for the shell.
 *
 * The first version was cache-first, which meant an edited file kept serving the
 * old bytes to the very person who just changed it (measured: /armor preview
 * reported "unknown" because the cached app.js still asked the wrong path).
 * Network-first with a cache fallback keeps the installable/offline property
 * without freezing the build.
 *
 * API calls are never cached. A stale chat answer is worse than an error.
 */
const CACHE = 'forge-shell-v32';
/* Shell files are cached by the URL that was actually fetched, query string
 * included. index.html loads app.js?v=..., so precaching the bare name caches a
 * URL nothing ever requests — and the old build survives forever. Network-first,
 * cache fallback; the versioned URL is what forces the new bytes in. */
const SHELL = ['index.html', 'manifest.webmanifest', 'icon.svg'];
const STATIC = /\.(js|css|html|webmanifest|svg)$/;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const isShell = url.origin === location.origin &&
                  (SHELL.some(f => url.pathname.endsWith(f)) || STATIC.test(url.pathname));
  if (!isShell) return;                       // everything else: plain network

  e.respondWith((async () => {
    try {
      const fresh = await fetch(e.request, { cache: 'no-store' });
      if (fresh && fresh.ok) {
        const c = await caches.open(CACHE);
        c.put(e.request, fresh.clone());
      }
      return fresh;
    } catch {
      const hit = await caches.match(e.request);
      return hit || new Response('offline', { status: 503 });
    }
  })());
});
