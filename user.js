(function () {
  const pickMap = makeMap('pick-map');
  const liveMap = makeMap('live-map');
  const pin = (cls, t) => L.divIcon({ className: '', html: `<div class="pin ${cls}">${t}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] });
  let dest = null, destMarker = null, cartMarker = null, cartPos = null, selected = null;
  const layers = L.layerGroup().addTo(liveMap);
  let cache = [];

  // 日時の初期値(現在+1時間)と最小値
  const f = document.getElementById('res-form');
  const pad = (n) => String(n).padStart(2, '0');
  const toLocal = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  f.scheduled_at.value = toLocal(new Date(Date.now() + 3600e3));
  f.scheduled_at.min = toLocal(new Date());

  const destInfo = document.getElementById('dest-info');

  function setDest(lat, lon, name, fly) {
    dest = { lat, lon };
    const ll = [lat, lon];
    if (destMarker) destMarker.setLatLng(ll); else destMarker = L.marker(ll, { icon: pin('d', '着') }).addTo(pickMap);
    destInfo.textContent = `選択済み(${lat.toFixed(5)}, ${lon.toFixed(5)})`;
    if (name && !f.address.value.trim()) f.address.value = name;
    if (fly || !cartPos) pickMap.setView(ll, Math.max(pickMap.getZoom(), 16));
    else pickMap.fitBounds(L.latLngBounds([[cartPos.lat, cartPos.lon], ll]).pad(0.3));
  }

  document.getElementById('dest-search').append(makeSearch('配送先を検索(例: みやざき、宮崎市役所)', (p) => {
    f.address.value = p.name; setDest(p.lat, p.lon, p.name, true);
  }));
  pickMap.on('click', (e) => setDest(e.latlng.lat, e.latlng.lng, '', false));

  // 台車の現在地(最初は基地。配送のあとは最後にいた場所)
  let firstCart = true;
  async function loadCart() {
    const box = document.getElementById('cart-info');
    try {
      const c = await api('/api/cart');
      cartPos = { lat: c.lat, lon: c.lon };
      box.textContent = '台車は現在、' + (c.at_base ? c.label : '配送のあとの場所(' + c.lat.toFixed(4) + ', ' + c.lon.toFixed(4) + ')') + ' にいます。ここから向かいます。';
      if (cartMarker) cartMarker.setLatLng([c.lat, c.lon]); else cartMarker = L.marker([c.lat, c.lon], { icon: pin('o', '台') }).addTo(pickMap);
      if (firstCart) { firstCart = false; pickMap.setView([c.lat, c.lon], 16); }
    } catch (ex) { box.textContent = '台車の現在地を確認できません: ' + ex.message; }
  }
  loadCart();
  setInterval(loadCart, 5000);

  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('res-error');
    errEl.textContent = '';
    if (!dest) { errEl.textContent = '配送先を検索するか、地図をタップして選んでください'; return; }
    const btn = f.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = '道路経路を確認しています…';
    try {
      await api('/api/reservations', {
        method: 'POST',
        body: {
          scheduled_at: f.scheduled_at.value, address: f.address.value, lat: dest.lat, lon: dest.lon,
          item_desc: f.item_desc.value, forgotten_items: f.forgotten_items.value, notes: f.notes.value,
        },
      });
      toast('予約を受け付けました', 'notice');
      f.address.value = f.item_desc.value = f.forgotten_items.value = f.notes.value = '';
      if (destMarker) { pickMap.removeLayer(destMarker); destMarker = null; }
      dest = null; destInfo.textContent = '未選択(検索するか、下の地図をタップ)';
      refresh();
    } catch (ex) { errEl.textContent = ex.message; }
    finally { btn.disabled = false; btn.textContent = 'この内容で予約する'; }
  });

  function banner(r) {
    if (r.status === 'cancelled' || (r.status === 'completed' && r.outcome !== 'denied')) return null;
    if (r.outcome === 'denied') return h('div', { class: 'banner crit' }, '今回は収納部を開けませんでした(荷物は渡していません)。' + (r.status === 'returning' ? '台車は基地へ戻ります。' : '') + 'ご不明な点はお問い合わせください。');
    if (r.status === 'returning') return h('div', { class: 'banner info' }, 'お届けが完了しました。台車は基地へ戻っています。');
    if (r.status === 'unlocked') return h('div', { class: 'banner ok' }, `収納部が開きました。荷物をお受け取りください(あと${r.unlock_remaining_s ?? 0}秒で自動的に施錠されます)`);
    if (r.status === 'arrived') return h('div', { class: 'banner warn' }, '台車が到着しました。カメラの前に顔を近づけてください。');
    if (r.status === 'unlock_pending') return h('div', { class: 'banner warn' }, '確認中です。そのままお待ちください。収納部は施錠されています。');
    if (r.status === 'denied') return h('div', { class: 'banner crit' }, '今回は収納部を開けませんでした。お問い合わせください。');
    if (r.paused) return h('div', { class: 'banner info' }, '安全確認のため一時停止中です。しばらくお待ちください。');
    if (r.status === 'en_route') return h('div', { class: 'banner info' }, '台車が向かっています。');
    return null;
  }

  function render() {
    const box = document.getElementById('res-list');
    box.replaceChildren();
    if (!cache.length) box.append(h('p', { class: 'muted' }, 'まだ予約はありません。'));
    for (const r of cache) {
      const card = h('div', { class: 'res' + (r.id === selected ? ' sel' : ''), onclick: () => { selected = r.id; render(); focus(r); } },
        h('div', { class: 'row' },
          h('strong', {}, `予約 #${r.id}`),
          h('span', { class: 'chip st-' + r.status }, r.status_label)),
        h('div', { class: 'meta' }, `日時: ${fmtLocal(r.scheduled_at)}`),
        h('div', { class: 'meta' }, `出発する場所: ${r.origin_address || '-'}`),
        h('div', { class: 'meta' }, `配送先: ${r.address}`),
        h('div', { class: 'meta' }, `荷物: ${r.item_desc}`),
        r.forgotten_items ? h('div', { class: 'meta' }, `忘れ物: ${r.forgotten_items}`) : null,
        r.notes ? h('div', { class: 'meta' }, `注意事項: ${r.notes}`) : null,
        banner(r),
        r.status === 'reserved' ? h('button', { class: 'btn small', onclick: (ev) => { ev.stopPropagation(); cancel(r.id); } }, 'キャンセル') : null);
      box.append(card);
    }
    layers.clearLayers();
    for (const r of cache) {
      if (['cancelled', 'completed'].includes(r.status)) continue;
      if (r.origin_lat != null) L.marker([r.origin_lat, r.origin_lon], { icon: pin('o', '出') }).bindTooltip(`#${r.id} 出発地`).addTo(layers);
      L.marker([r.lat, r.lon], { icon: pin('d', '着') }).bindTooltip(`#${r.id} 配送先`).addTo(layers);
      if (r.cart_lat != null) L.circleMarker([r.cart_lat, r.cart_lon], { radius: 10, color: '#c2410c', fillColor: '#f08a24', fillOpacity: 1 }).bindTooltip(`#${r.id} 台車`).addTo(layers);
    }
  }

  function focus(r) {
    const p = r.cart_lat != null ? [r.cart_lat, r.cart_lon] : [r.lat, r.lon];
    liveMap.setView(p, Math.max(liveMap.getZoom(), 16));
  }

  async function cancel(id) {
    if (!confirm('この予約をキャンセルしますか?')) return;
    try { await api(`/api/reservations/${id}/cancel`, { method: 'POST', body: {} }); refresh(); }
    catch (ex) { toast(ex.message, 'warning'); }
  }

  async function refresh() {
    try {
      const next = (await api('/api/reservations')).reservations;
      const sig = JSON.stringify(next);
      if (sig !== window.__sig) { window.__sig = sig; cache = next; render(); }  // 変化があるときだけ描画
    } catch (_) {}
  }
  refresh();
  setInterval(refresh, 3000);
})();
