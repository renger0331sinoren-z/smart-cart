(function (w) {
  w.api = async function (path, opts = {}) {
    const r = await fetch(path, {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    });
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) {
      if (r.status === 401 && !location.pathname.startsWith('/login')) { location.href = '/login'; }
      throw new Error((data && data.error) || '通信エラーが発生しました');
    }
    return data;
  };

  // 安全なDOM生成(innerHTMLは使わない)
  w.h = function (tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') e.className = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) {
      if (c == null || c === false) continue;
      e.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return e;
  };

  w.toast = function (msg, level = 'info') {
    const box = document.getElementById('toasts');
    const t = h('div', { class: 'toast ' + level }, msg);
    box.append(t);
    setTimeout(() => t.remove(), level === 'critical' ? 12000 : 5000);
  };

  w.fmtUtc = function (iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    return d.toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  };
  w.fmtLocal = function (s) { return (s || '').replace('T', ' '); };

  w.CFG = (function () {
    const d = document.body.dataset;
    return { lat: parseFloat(d.depotLat) || 35.68, lon: parseFloat(d.depotLon) || 139.76, radius: parseFloat(d.radius) || 3000 };
  })();

  w.makeMap = function (id) {
    const m = L.map(id).setView([CFG.lat, CFG.lon], 15);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(m);
    L.circle([CFG.lat, CFG.lon], { radius: CFG.radius, color: '#0e7c86', weight: 1, fillOpacity: 0.04, interactive: false }).addTo(m);
    L.circleMarker([CFG.lat, CFG.lon], { radius: 8, color: '#475569', fillColor: '#94a3b8', fillOpacity: 1 }).bindTooltip('出発地(デポ)').addTo(m);
    return m;
  };

  w.logout = async function () {
    try { await api('/api/logout', { method: 'POST', body: {} }); } catch (_) {}
    location.href = '/login';
  };
})(window);
