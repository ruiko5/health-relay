/* 健康日报 PWA Service Worker
 *
 * 目标：**打开就是页面**，绝不白屏、绝不反复刷新。
 *
 * 做法：网络优先 + 2.5 秒超时。
 *   - 服务醒着：正常拉最新（并写回缓存）
 *   - 服务睡着：2.5 秒内没响应就用缓存页面立即显示，网络请求继续在后台跑，回来就更新缓存
 *   - 连缓存都没有：显示自带的「正在唤醒」页，并自动重试
 */
const CACHE = 'health-report-v4';
const SHELL = ['/', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];
const NET_TIMEOUT = 2500;

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
button{margin-top:16px;padding:8px 16px;border:none;border-radius:10px;background:#007AFF;color:#fff;font-size:14px}
</style></head>
<body><div class="s"></div>
<p>服务正在唤醒，大约 30 秒</p><small>页面会自动重试</small>
<button onclick="location.reload()">立即重试</button>
<script>setTimeout(function(){ location.reload(); }, 8000);</script>
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

const withTimeout = (p, ms) =>
  Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))]);

async function cachedShell(req) {
  const c = await caches.open(CACHE);
  return (await c.match(req, { ignoreSearch: true })) || (await c.match('/', { ignoreSearch: true }));
}

function fetchAndCache(req, cache) {
  return fetch(req).then(res => {
    if (res && res.status < 500) {
      cache.put(req, res.clone()).catch(() => {});
      return res;
    }
    throw new Error('bad status ' + (res && res.status));
  });
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // ---------- 页面导航：缓存优先兜底，网络 2.5 秒没回来就先给缓存 ----------
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cachedShell(req);
      const net = fetchAndCache(req, cache);

      if (!cached) {
        try {
          return await net;
        } catch (err) {
          return new Response(WAKE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
        }
      }
      try {
        return await withTimeout(net, NET_TIMEOUT);
      } catch (err) {
        return cached;   // 秒开缓存，网络在后台继续（成功后自动更新缓存）
      }
    })());
    return;
  }

  // ---------- 接口：网络优先，2.5 秒超时后退回缓存 ----------
  if (url.pathname.startsWith('/api/')) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(req);
      const net = fetchAndCache(req, cache);
      if (!cached) {
        try {
          return await net;
        } catch (err) {
          return new Response(JSON.stringify({ ok: false, offline: true, error: 'offline' }),
            { headers: { 'Content-Type': 'application/json' } });
        }
      }
      try {
        return await withTimeout(net, NET_TIMEOUT);
      } catch (err) {
        return cached;
      }
    })());
    return;
  }

  // ---------- 静态资源：缓存优先 ----------
  e.respondWith(
    caches.match(req).then(hit => {
      if (hit) {
        fetch(req).then(res => {
          if (res && res.ok) caches.open(CACHE).then(c => c.put(req, res.clone())).catch(() => {});
        }).catch(() => {});
        return hit;
      }
      return fetch(req).then(res => {
        if (res && res.ok) caches.open(CACHE).then(c => c.put(req, res.clone())).catch(() => {});
        return res;
      });
    })
  );
});
