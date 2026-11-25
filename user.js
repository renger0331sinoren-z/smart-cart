(function () {
  const pickMap = makeMap('pick-map');
  const liveMap = makeMap('live-map');
  let pickMarker = null, picked = null, selected = null;
  const layers = L.layerGroup().addTo(liveMap);
  let cache = [];

  // 日時の初期値(現在+1時間)と最小値
  const f = document.getElementById('res-form');
  const pad = (n) => String(n).padStart(2, '0');
  const toLocal = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const soon = new Date(Date.now() + 3600e3);
  f.scheduled_at.value = toLocal(soon);
  f.scheduled_at.min = toLocal(new Date());

  pickMap.on('click', (e) => {
    picked = { lat: e.latlng.lat, lon: e.latlng.lng };
    if (pickMarker) pickMarker.setLatLng(e.latlng);
    else pickMarker = L.circleMarker(e.latlng, { radius: 9, color: '#2563eb', fillColor: '#3b82f6', fillOpacity: 0.9 }).addTo(pickMap);
    document.getElementById('pick-info').textContent = `位置: 選択済み(${picked.lat.toFixed(5)}, ${picked.lon.toFixed(5)})`;
  });

  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('res-error');
    errEl.textContent = '';
    if (!picked) { errEl.textContent = '地図をタップして配送先を選んでください'; return; }
    try {
      await api('/api/reservations', {
        method: 'POST',
        body: {
          scheduled_at: f.scheduled_at.value, address: f.address.value, lat: picked.lat, lon: picked.lon,
          item_desc: f.item_desc.value, forgotten_items: f.forgotten_items.value, notes: f.notes.value,
        },
      });
      toast('予約を受け付けました', 'notice');
      f.address.value = f.item_desc.value = f.forgotten_items.value = f.notes.value = '';
      refresh();
    } catch (ex) { errEl.textContent = ex.message; }
  });

  function banner(r) {
    if (r.status === 'cancelled' || r.status === 'completed') return null;
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
        h('div', { class: 'meta' }, `場所: ${r.address}`),
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
      L.circleMarker([r.lat, r.lon], { radius: 8, color: '#2563eb', fillColor: '#60a5fa', fillOpacity: 0.9 }).bindTooltip(`#${r.id} 配送先`).addTo(layers);
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
