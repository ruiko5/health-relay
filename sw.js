/* 健康日报 PWA Service Worker
 *
 * 关键目标：Render 免费实例休眠时，首页会直接返回 502 黑页（那段 HTML 在我们的
 * 应用启动之前就吐出来了，页面里的 JS 完全没机会跑）。所以这里在 SW 层拦截：
 *   - 导航请求：先走网络；失败或 5xx 就用缓存的上次页面，没缓存就显示自带等待页
 *   - /api/*：网络优先，失败用缓存
 *   - 静态资源：缓存优先
 */
const CACHE = 'health-report-v3';
const SHELL = ['/', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];

const WAKE_HTML = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>正在唤醒 · 健康日报</title>
<style>
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:linear-gradient(170deg,#f2f5fa,#e9eef7);color:#131a24}
.s{width:34px;height:34px;border-radius:50%;border:3px solid #dfe6f3;border-top-color:#6c7cf0;
animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
p{margin-top:18px;font-size:14px;color:#7b879a}
small{margin-top:6px;font-size:12px;color:#a6b0bf}
</style></head>
<body><div class="s"></div>
<p>服务正在唤醒，大约 30–60 秒</p><small>页面会自动重试</small>
<script>
let n = 0;
setTimeout(function again(){ location.reload(); }, 6000);
</script>
</body></html>`;

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function cachedShell(req) {
  const c = await caches.open(CACHE);
  return (await c.match(req, { ignoreSearch: true })) ||
         (await c.match('/', { ignoreSearch: true }));
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // 页面导航：服务没醒时用缓存/等待页，别让用户看到 Render 的 502
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.status < 500) {
          caches.open(CACHE).then(c => c.put(req, fresh.clone())).catch(() => {});
          return fresh;
        }
      } catch (err) { /* 网络失败，走下面的兜底 */ }
      const hit = await cachedShell(req);
      return hit || new Response(WAKE_HTML, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' }
      });
    })());
    return;
  }

  // 接口：网络优先，失败用缓存
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.ok) {
            caches.open(CACHE).then(c => c.put(req, res.clone())).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(req).then(hit => hit || new Response(
          JSON.stringify({ ok: false, offline: true, error: 'offline' }),
          { headers: { 'Content-Type': 'application/json' } })))
    );
    return;
  }

  // 静态资源：缓存优先
  e.respondWith(
    caches.match(req).then(hit => {
      if (hit) {
        fetch(req).then(res => {
          if (res && res.ok) caches.open(CACHE).then(c => c.put(req, res.clone())).catch(() => {});
        }).catch(() => {});
        return hit;
      }
      return fetch(req).then(res => {
        if (res && res.ok) {
          caches.open(CACHE).then(c => c.put(req, res.clone())).catch(() => {});
        }
        return res;
      });
    })
  );
});
