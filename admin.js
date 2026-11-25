(function () {
  const S = { res: [], sel: null, events: [], last: 0, running: false, busy: false, sound: true, first: true, listSig: '', ctlSig: '' };
  const map = makeMap('map');
  const layer = L.layerGroup().addTo(map);
  const routeLayer = L.layerGroup().addTo(map);
  const SIGNAL = { red: '赤', green: '青', unknown: '確認不可' };
  const live = (r) => ['en_route', 'arrived', 'unlock_pending', 'unlocked'].includes(r.status);
  const cur = () => S.res.find((r) => r.id === S.sel);

  // ---------- 操作 ----------
  async function act(id, action, body = {}) {
    try { await api(`/api/admin/reservations/${id}/${action}`, { method: 'POST', body }); }
    catch (ex) { toast(ex.message, 'warning'); }
    poll();
  }

  // ---------- ポーリング(走行シミュレーションは実行中のみ進む) ----------
  async function poll() {
    if (S.busy) return;
    S.busy = true;
    try {
      const mult = parseFloat(document.getElementById('mult').value) || 1;
      const d = await api('/api/admin/tick', { method: 'POST', body: { run: S.running, dt: mult, after: S.last, sel: S.sel || 0 } });
      S.res = d.reservations;
      handleEvents(d.events);
      render();
    } catch (_) {} finally { S.busy = false; }
  }

  function beep() {
    if (!S.sound) return;
    try {
      const c = new (window.AudioContext || window.webkitAudioContext)();
      const o = c.createOscillator(), g = c.createGain();
      o.connect(g); g.connect(c.destination); o.frequency.value = 880; g.gain.value = 0.08;
      o.start(); setTimeout(() => { o.stop(); c.close(); }, 250);
    } catch (_) {}
  }

  const speak = (text) => speakJa(text);  // 台車のスピーカーを模擬(男性の声を優先)

  function handleEvents(evs) {
    for (const e of evs) {
      if (e.id <= S.last) continue;
      S.last = e.id;
      S.events.push(e);
      if (S.first) continue;  // 初回読み込み分は通知しない
      if (e.kind === 'voice_prompt') speak(e.detail);  // 台車のスピーカーを模擬
      if (['critical', 'warning'].includes(e.severity)) { toast(`#${e.reservation_id ?? '-'} ${e.detail}`, e.severity); beep(); }
    }
    S.first = false;
    if (S.events.length > 500) S.events = S.events.slice(-500);
  }

  // ---------- 描画 ----------
  function render() {
    renderList(); renderMap(); renderDetail(); renderLog();
  }

  function renderList() {
    const sig = S.res.map((r) => [r.id, r.status, r.hold_reason, S.sel === r.id].join(':')).join('|');
    if (sig === S.listSig) return;
    S.listSig = sig;
    const box = document.getElementById('list');
    box.replaceChildren();
    if (!S.res.length) box.append(h('p', { class: 'muted' }, '予約はまだありません。'));
    for (const r of S.res) {
      box.append(h('div', { class: 'res' + (r.id === S.sel ? ' sel' : ''), onclick: () => select(r.id) },
        h('div', { class: 'row' }, h('strong', {}, `#${r.id} ${r.user_name}`), h('span', { class: 'chip st-' + r.status }, r.status_label),
          r.hold_reason ? h('span', { class: 'chip hold' }, '停止中') : null),
        h('div', { class: 'meta' }, `日時: ${fmtLocal(r.scheduled_at)}`),
        h('div', { class: 'meta' }, `出発地: ${r.origin_address || '-'}`),
        h('div', { class: 'meta' }, `配送先: ${r.address}`),
        h('div', { class: 'meta' }, `荷物: ${r.item_desc}`)));
    }
  }

  function select(id) {
    S.sel = id; S.listSig = ''; S.ctlSig = ''; render(); poll();
    const r = cur();
    if (r) map.setView(r.cart_lat != null ? [r.cart_lat, r.cart_lon] : [r.lat, r.lon], Math.max(map.getZoom(), 16));
  }

  const pin = (cls, t) => L.divIcon({ className: '', html: `<div class="pin ${cls}">${t}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] });
  const pinS = (cls, t) => L.divIcon({ className: '', html: `<div class="pin ${cls}">${t}</div>`, iconSize: [16, 16], iconAnchor: [8, 8] });

  function renderMap() {
    layer.clearLayers(); routeLayer.clearLayers();
    for (const r of S.res) {
      if (['cancelled', 'completed'].includes(r.status)) continue;
      if (r.origin_lat != null) L.marker([r.origin_lat, r.origin_lon], { icon: pin('o', '出') }).bindTooltip(`#${r.id} 出発地: ${r.origin_address || ''}`).on('click', () => select(r.id)).addTo(layer);
      L.marker([r.lat, r.lon], { icon: pin('d', '着') }).bindTooltip(`#${r.id} ${r.user_name} 配送先: ${r.address}`).on('click', () => select(r.id)).addTo(layer);
      if (r.cart_lat != null && live(r)) {
        L.circleMarker([r.cart_lat, r.cart_lon], { radius: 11, color: r.hold_reason ? '#b91c1c' : '#c2410c', fillColor: r.hold_reason ? '#ef4444' : '#f08a24', fillOpacity: 1 })
          .bindTooltip(`#${r.id} 台車${r.hold_reason ? '(停止中)' : ''}`).on('click', () => select(r.id)).addTo(layer);
      }
    }
    const r = cur();
    if (r && r.route_points) {
      const key = r.id + ':' + r.route_points.length;
      L.polyline(r.route_points, { color: '#22d3ee', weight: 5, opacity: 0.9 }).addTo(routeLayer);
      for (const [i, st] of r.stops.entries()) {
        L.marker([st.lat, st.lon], { icon: pinS((st.kind === 'corner' ? 'corner' : 'sig') + (i < r.stop_idx ? ' done' : ''), ''), zIndexOffset: -100 })
          .bindTooltip((st.kind === 'corner' ? '曲がり角' : '信号') + `(${Math.round(st.dist)}m地点)`).addTo(routeLayer);
      }
      if (S.fitKey !== key) { S.fitKey = key; map.fitBounds(L.latLngBounds(r.route_points).pad(0.2)); }
    }
  }

  function renderLog() {
    const box = document.getElementById('log');
    const only = document.getElementById('only-sel').checked && S.sel;
    const list = S.events.filter((e) => !only || e.reservation_id === S.sel).slice(-150).reverse();
    const sig = list.length + ':' + (list[0] ? list[0].id : 0) + ':' + only;
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.replaceChildren(...list.map((e) => h('div', { class: 'ev ' + e.severity },
      h('div', { class: 't' }, `${fmtUtc(e.ts)} ・ ${e.reservation_id ? '#' + e.reservation_id : '全体'} ・ ${e.actor}`),
      h('div', {}, e.detail ? `${e.kind}: ${e.detail}` : e.kind))));
  }

  // ---------- 詳細・制御パネル ----------
  const cam = { canvas: null, video: null, stream: null, t0: performance.now() };

  function renderDetail() {
    const r = cur();
    const box = document.getElementById('detail');
    if (!r) return;
    const sig = [r.id, r.status, r.hold_reason, r.locked, r.obstacle, r.comms_ok, r.sensor_ok, r.signal_state].join(':');
    if (sig !== S.ctlSig) { S.ctlSig = sig; buildDetail(box, r); }
    const p = document.getElementById('prog');
    if (p) p.textContent = r.total_m ? `${r.progress_m} / ${r.total_m} m(停止ポイント ${r.stop_idx}/${r.stop_count})` : '-';
    const u = document.getElementById('unlock-remain');
    if (u) u.textContent = r.unlock_remaining_s != null ? `${r.unlock_remaining_s}秒` : '-';
  }

  function toggle(r, label, key, checked, invert) {
    return h('label', {}, h('input', { type: 'checkbox', checked, onchange: (e) => act(r.id, 'sim', { [key]: invert ? !e.target.checked : e.target.checked }) }), label);
  }

  function buildDetail(box, r) {
    const kids = [];
    kids.push(h('h2', {}, `予約 #${r.id} / ${r.user_name}`));
    kids.push(h('div', { class: 'row' }, h('span', { class: 'chip st-' + r.status }, r.status_label),
      h('span', { class: 'chip ' + (r.locked ? 'st-completed' : 'hold') }, r.locked ? '🔒 施錠中' : '🔓 開錠中'),
      r.hold_reason ? h('span', { class: 'chip hold' }, r.hold_label) : null));
    kids.push(h('dl', { class: 'kv' },
      h('dt', {}, '希望日時'), h('dd', {}, fmtLocal(r.scheduled_at)),
      h('dt', {}, '出発地'), h('dd', {}, r.origin_address || '-'),
      h('dt', {}, '配送先'), h('dd', {}, r.address),
      h('dt', {}, '経路'), h('dd', {}, r.route_source === 'road' ? '道路に沿った経路' : r.route_source === 'fallback' ? '簡易経路(直線)' : '出発時に作成'),
      h('dt', {}, '荷物'), h('dd', {}, r.item_desc),
      h('dt', {}, '忘れ物'), h('dd', {}, r.forgotten_items || '-'),
      h('dt', {}, '注意事項'), h('dd', {}, r.notes || '-'),
      h('dt', {}, '現在位置'), h('dd', {}, r.cart_lat != null ? `${r.cart_lat.toFixed(5)}, ${r.cart_lon.toFixed(5)}` : '未出発'),
      h('dt', {}, '進行'), h('dd', { id: 'prog' }, '-'),
      h('dt', {}, '開錠残り'), h('dd', { id: 'unlock-remain' }, '-')));

    // カメラ(模擬映像。自分のWebカメラで代替表示も可能)
    cam.canvas = h('canvas', { class: 'cam', width: 640, height: 360 });
    cam.video = h('video', { autoplay: true, muted: true, playsinline: true, hidden: true });
    kids.push(h('h3', { style: 'margin-top:.8rem' }, '台車カメラ(シミュレーション映像)'));
    kids.push(h('div', { class: 'camwrap' }, cam.canvas, cam.video));
    kids.push(h('div', { class: 'row', style: 'margin-top:.4rem' },
      h('button', { class: 'btn small', onclick: toggleWebcam }, cam.stream ? 'Webカメラ停止' : 'Webカメラで試す'),
      h('button', { class: 'btn small', onclick: () => { if (!speak('カメラの前に顔を近づけてください。配送の確認を行います')) toast('この端末では音声を再生できません', 'warning'); } }, '🔊 音声テスト')));

    // 操作
    const ctl = [];
    if (r.status === 'reserved') ctl.push(h('button', { class: 'btn primary', onclick: () => act(r.id, 'dispatch') }, '配送を開始(シミュレーション)'));
    if (r.status === 'en_route') {
      if (r.hold_reason) ctl.push(h('button', { class: 'btn ok', onclick: () => {
        if (confirm('カメラ映像と位置情報で周囲の安全(人・車・障害物・信号)を確認しました。走行を再開しますか?')) act(r.id, 'resume', { confirmed: true });
      } }, '安全確認OK → 再開を許可'));
      ctl.push(h('button', { class: 'btn danger', onclick: () => act(r.id, 'estop') }, '⛔ 非常停止'));
    }
    if (r.status === 'arrived') {
      ctl.push(h('button', { class: 'btn warn', onclick: () => act(r.id, 'face-detected') }, '(模擬)利用者がカメラの前に来た'));
      ctl.push(h('button', { class: 'btn', onclick: () => act(r.id, 'complete') }, '開錠せず終了'));
    }
    if (r.status === 'unlock_pending') {
      ctl.push(h('div', { class: 'banner crit', style: 'flex-basis:100%' }, '開錠要求: 映像と配送情報を確認して判断してください'));
      ctl.push(h('button', { class: 'btn ok', onclick: () => {
        if (confirm(`予約 #${r.id}(${r.user_name})の収納部を開錠します。映像と配送情報を確認しましたか?`)) act(r.id, 'unlock', { decision: 'open' });
      } }, '🔓 開ける'));
      ctl.push(h('button', { class: 'btn danger', onclick: () => act(r.id, 'unlock', { decision: 'deny' }) }, '開けない'));
    }
    if (r.status === 'unlocked') ctl.push(h('button', { class: 'btn primary', onclick: () => act(r.id, 'complete') }, '🔒 施錠して完了'));
    if (r.status === 'denied') ctl.push(h('button', { class: 'btn', onclick: () => act(r.id, 'complete') }, '完了にする'));
    if (ctl.length) { kids.push(h('h3', { style: 'margin-top:.8rem' }, '操作')); kids.push(h('div', { class: 'row' }, ctl)); }

    // 模擬センサー入力
    if (['en_route', 'arrived', 'unlock_pending', 'unlocked', 'reserved'].includes(r.status)) {
      kids.push(h('h3', { style: 'margin-top:.8rem' }, '模擬センサー入力(テスト用)'));
      kids.push(h('div', { class: 'toggles' },
        toggle(r, '障害物を検知(人・車・物)', 'obstacle', r.obstacle, false),
        toggle(r, '通信が切断', 'comms_ok', !r.comms_ok, true),
        toggle(r, 'センサー異常', 'sensor_ok', !r.sensor_ok, true),
        h('label', {}, '信号', h('select', { onchange: (e) => act(r.id, 'sim', { signal_state: e.target.value }) },
          ...['red', 'green', 'unknown'].map((v) => h('option', { value: v, selected: r.signal_state === v }, SIGNAL[v]))))));
      kids.push(h('p', { class: 'hint' }, '障害物が消えても、青信号になっても、自動では再開しません。必ず上の「再開を許可」が必要です。'));
    }
    box.replaceChildren(...kids);
  }

  async function toggleWebcam() {
    if (cam.stream) { cam.stream.getTracks().forEach((t) => t.stop()); cam.stream = null; cam.video.hidden = true; S.ctlSig = ''; return; }
    try {
      cam.stream = await navigator.mediaDevices.getUserMedia({ video: true });
      cam.video.srcObject = cam.stream; cam.video.hidden = false; S.ctlSig = '';
    } catch (_) { toast('Webカメラを使用できませんでした', 'warning'); }
  }

  // 模擬カメラ映像
  function drawCam(now) {
    requestAnimationFrame(drawCam);
    const c = cam.canvas; if (!c || !c.isConnected) return;
    const r = cur(); if (!r) return;
    const g = c.getContext('2d'), W = c.width, H = c.height, t = (now - cam.t0) / 1000;
    g.fillStyle = '#0b1220'; g.fillRect(0, 0, W, H);
    // 路面
    g.fillStyle = '#1e293b'; g.beginPath(); g.moveTo(W * 0.35, H * 0.35); g.lineTo(W * 0.65, H * 0.35); g.lineTo(W, H); g.lineTo(0, H); g.fill();
    const moving = r.status === 'en_route' && !r.hold_reason;
    g.strokeStyle = '#94a3b8'; g.lineWidth = 3;
    for (let i = 0; i < 6; i++) {
      const k = ((i + (moving ? t * 0.8 : 0)) % 6) / 6, y = H * 0.35 + (H * 0.65) * k * k, w = 4 + k * 14;
      g.beginPath(); g.moveTo(W / 2, y); g.lineTo(W / 2, y + 10 + k * 40); g.lineWidth = w; g.stroke();
    }
    if (r.obstacle && live(r)) {  // 障害物
      g.fillStyle = '#f59e0b'; g.fillRect(W * 0.42, H * 0.5, W * 0.16, H * 0.3);
      g.fillStyle = '#000'; g.font = 'bold 16px sans-serif'; g.fillText('障害物', W * 0.455, H * 0.68);
    }
    if (r.status === 'arrived' || r.status === 'unlock_pending' || r.status === 'unlocked') {  // 利用者のシルエット
      const near = r.status !== 'arrived';
      g.fillStyle = near ? '#cbd5e1' : '#64748b';
      const s = near ? 1.5 : 0.8;
      g.beginPath(); g.arc(W / 2, H * 0.4, 36 * s, 0, 7); g.fill();
      g.fillRect(W / 2 - 70 * s, H * 0.4 + 40 * s, 140 * s, 140 * s);
    }
    // 信号(模擬)
    if (r.status === 'en_route') {
      g.fillStyle = { red: '#ef4444', green: '#22c55e', unknown: '#64748b' }[r.signal_state]; g.beginPath(); g.arc(W - 40, 40, 14, 0, 7); g.fill();
    }
    g.fillStyle = '#e2e8f0'; g.font = '14px monospace';
    g.fillText('CART-CAM 01  [SIMULATION]', 12, 22);
    g.fillText(new Date().toLocaleTimeString('ja-JP'), 12, H - 12);
    g.fillStyle = r.locked ? '#22c55e' : '#ef4444'; g.fillText(r.locked ? 'LOCKED' : 'UNLOCKED', W - 100, H - 12);
    if (r.hold_reason || !r.comms_ok || !r.sensor_ok) {
      g.strokeStyle = '#ef4444'; g.lineWidth = 6; g.strokeRect(3, 3, W - 6, H - 6);
      g.fillStyle = '#ef4444'; g.font = 'bold 18px sans-serif';
      g.fillText(!r.comms_ok ? '通信断' : !r.sensor_ok ? 'センサー異常' : (r.hold_label || '停止'), 12, 48);
    }
  }
  requestAnimationFrame(drawCam);

  // ---------- 初期化 ----------
  document.getElementById('run-btn').addEventListener('click', (e) => {
    S.running = !S.running;
    e.target.textContent = S.running ? '⏸ 走行シミュレーション停止' : '▶ 走行シミュレーション開始';
  });
  document.getElementById('sound-btn').addEventListener('click', (e) => {
    S.sound = !S.sound; e.target.textContent = S.sound ? '🔔 通知音 ON' : '🔕 通知音 OFF';
  });
  document.getElementById('only-sel').addEventListener('change', () => { document.getElementById('log').dataset.sig = ''; renderLog(); });
  fillVoiceSelect(document.getElementById('voice'));
  poll();
  setInterval(poll, 1000);
})();
