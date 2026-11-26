// アプリ用のservice worker。
// ログイン後の画面や予約データは、他の人に見えたり古くなったりしないよう、保存(キャッシュ)しません。
// 通信できないときだけ、「つながっていません」の画面を出します。
const OFFLINE_HTML = '<!doctype html><html lang="ja"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1"><title>つながっていません</title>' +
  '<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f3f6f8;color:#1f2937;text-align:center;padding:16px}' +
  'button{font:inherit;padding:.7rem 1.4rem;border:0;border-radius:10px;background:#0e7c86;color:#fff;font-weight:700}</style></head>' +
  '<body><div><h1>つながっていません</h1><p>インターネットにつながってから、もう一度ひらいてください。</p>' +
  '<p><button onclick="location.reload()">もういちど読み込む</button></p></div></body></html>';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (e) => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
  }
});
