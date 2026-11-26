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
    return { lat: parseFloat(d.depotLat) || 35.68, lon: parseFloat(d.depotLon) || 139.76 };
  })();

  // 地図: 既定は国土地理院の航空写真(右上で標準地図・OpenStreetMapに切り替え可)
  w.makeMap = function (id) {
    const gsi = '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">国土地理院</a>';
    const photo = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg',
      { maxNativeZoom: 18, maxZoom: 19, attribution: gsi + '(航空写真)' });
    const std = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',
      { maxNativeZoom: 18, maxZoom: 19, attribution: gsi + '(地理院地図)' });
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      { maxZoom: 19, attribution: '© OpenStreetMap contributors' });
    const m = L.map(id, { layers: [photo] }).setView([CFG.lat, CFG.lon], 15);
    L.control.layers({ '航空写真(国土地理院)': photo, '地図(国土地理院)': std, 'OpenStreetMap': osm }, null, { collapsed: true }).addTo(m);
    L.control.scale({ imperial: false }).addTo(m);
    return m;
  };

  // 場所検索ボックス。入力に合わせて候補を表示し、選ぶと onPick({name, lat, lon}) を呼ぶ
  w.makeSearch = function (placeholder, onPick) {
    const input = h('input', { type: 'search', placeholder, autocomplete: 'off', 'aria-label': placeholder, maxlength: 60 });
    const status = h('p', { class: 'hint search-status' });
    const list = h('ul', { class: 'search-results' });
    let timer = null, token = 0;
    async function run() {
      const q = input.value.trim();
      list.replaceChildren();
      if (q.length < 2) { status.textContent = ''; return; }
      const my = ++token;
      status.textContent = '検索中…';
      try {
        const r = await api('/api/search?q=' + encodeURIComponent(q));
        if (my !== token) return;
        status.textContent = r.results.length ? `${r.results.length}件見つかりました。選んでください` : '見つかりませんでした。別の言葉で試すか、地図をタップしてください';
        for (const p of r.results) {
          list.append(h('li', {}, h('button', { type: 'button', class: 'search-item', onclick: () => {
            list.replaceChildren(); status.textContent = ''; input.value = p.name; onPick(p);
          } }, h('span', {}, p.name), h('small', {}, p.source))));
        }
      } catch (ex) { if (my === token) status.textContent = ex.message; }
    }
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 700); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(); } });
    return h('div', { class: 'search' }, input, status, list);
  };

  // 音声: 端末にある日本語の音声から、男性の声を優先して選ぶ(選択は記憶する)
  const MALE = /(ichiro|otoya|takumi|keita|naoki|kenji|hattori|yuma|daichi|male|男)/i;
  const FEMALE = /(female|haruka|ayumi|sayaka|nanami|aoi|mayu|shiori|kyoko|hikari|o-ren|女)/i;
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} } };
  w.jaVoices = function () {
    if (!('speechSynthesis' in window)) return [];
    return speechSynthesis.getVoices().filter((v) => (v.lang || '').toLowerCase().startsWith('ja'));
  };
  function score(v) {
    let sc = 0;
    if (MALE.test(v.name)) sc += 10;
    if (FEMALE.test(v.name)) sc -= 10;
    if (/natural|online/i.test(v.name)) sc += 4;
    if (!v.localService) sc += 1;
    return sc;
  }
  w.pickVoice = function () {
    const vs = jaVoices();
    const saved = store.get('voiceName');
    return vs.find((v) => v.name === saved) || vs.slice().sort((a, b) => score(b) - score(a))[0] || null;
  };
  w.speakJa = function (text) {
    if (!('speechSynthesis' in window)) return false;
    try {
      const u = new SpeechSynthesisUtterance(text);
      const v = pickVoice();
      u.lang = 'ja-JP';
      if (v) { u.voice = v; if (!MALE.test(v.name)) u.pitch = 0.7; }  // 男性名の声が無いときは低めにする
      u.rate = 0.95;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
      return true;
    } catch (_) { return false; }
  };
  w.fillVoiceSelect = function (sel) {
    const build = () => {
      const vs = jaVoices(), cur = pickVoice();
      sel.replaceChildren(...(vs.length ? vs : [null]).map((v) => v
        ? h('option', { value: v.name, selected: cur && cur.name === v.name }, v.name.replace(/^Microsoft /, ''))
        : h('option', { value: '' }, '日本語の音声なし')));
    };
    build();
    if ('speechSynthesis' in window) speechSynthesis.addEventListener('voiceschanged', build);
    sel.addEventListener('change', () => store.set('voiceName', sel.value));
  };

  w.logout = async function () {
    try { await api('/api/logout', { method: 'POST', body: {} }); } catch (_) {}
    location.href = '/login';
  };
  // テーマ(色の切り替え)。選択はこのブラウザに記憶する
  const THEMES = [['teal', 'ティール(標準)'], ['blue', 'ブルー'], ['sunset', 'サンセット'], ['dark', 'ダーク']];
  const themeStore = { get() { try { return localStorage.getItem('theme'); } catch (_) { return null; } }, set(v) { try { localStorage.setItem('theme', v); } catch (_) {} } };
  function applyTheme(t) {
    if (!THEMES.some((x) => x[0] === t)) t = 'teal';
    if (t === 'teal') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t);
    return t;
  }
  const cur = applyTheme(themeStore.get());
  const sel = h('select', { class: 'theme-select', 'aria-label': 'テーマ(色)' }, ...THEMES.map(([v, n]) => h('option', { value: v, selected: v === cur }, '色: ' + n)));
  sel.addEventListener('change', () => { themeStore.set(applyTheme(sel.value)); });
  const bar = document.querySelector('.topbar');
  if (bar) { const out = bar.querySelector('[data-logout]'); bar.insertBefore(sel, out || null); } else { sel.classList.add('theme-float'); document.body.append(sel); }

  document.querySelectorAll('[data-logout]').forEach((b) => b.addEventListener('click', () => w.logout()));
})(window);
