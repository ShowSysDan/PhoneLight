/* PhoneLight admin: the live feed (/ws/admin) and small helpers shared by every page.
   One socket per page; pages register with PL.feed(views, fn). DOM writes only on change. */
'use strict';
window.PL = (function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(+v) ? '–' : (+v).toFixed(d));

  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
  function setHTML(el, html) { if (el && el._html !== html) { el.innerHTML = html; el._html = html; } }
  function pill(id, cls, text) {
    const el = $(id);
    if (!el) return;
    const want = 'pill ' + cls;
    if (el.className !== want) el.className = want;
    setText(el.querySelector('span'), text);
  }

  function duration(s) {
    s = Math.max(0, Math.round(+s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`;
  }
  function when(iso) {
    if (!iso) return '–';
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString();
  }

  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  const SIGNAL = { live: ['ok', 'sACN live'], hold: ['warn', 'sACN lost: holding'], fade: ['warn', 'sACN lost: fading'],
                   lost: ['bad', 'sACN lost'], none: ['bad', 'no sACN yet'] };
  const MODE = { color: 'TEST: manual colour', pattern: 'TEST: pattern', identify: 'OVERRIDE: identify', blackout: 'OVERRIDE: blackout' };

  let views = [], handlers = [], ws = null, tries = 0;
  function header(d) {
    const sig = SIGNAL[d.engine.signal] || ['', d.engine.signal];
    const srcs = d.sacn.sources.length;
    pill('pill-sacn', d.sacn.error ? 'bad' : sig[0],
      d.sacn.error ? 'sACN port error' : `${sig[1]} · U${d.settings.universe}` + (srcs ? ` · ${srcs} src` : ''));
    pill('pill-guests', 'info', `${d.connected} phone${d.connected === 1 ? '' : 's'}`);
    const mode = $('pill-mode');
    mode.hidden = d.engine.mode === 'sacn';
    if (!mode.hidden) pill('pill-mode', 'warn', MODE[d.engine.mode]);
  }
  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/admin');
    ws.onopen = () => { tries = 0; pill('pill-feed', 'ok', 'live'); ws.send(JSON.stringify({ views })); };
    ws.onmessage = (e) => {
      const d = JSON.parse(e.data);
      header(d);
      for (const fn of handlers) fn(d);
    };
    ws.onclose = () => {
      pill('pill-feed', 'bad', 'disconnected');
      setTimeout(connect, Math.min(5000, 500 * ++tries));
    };
  }
  function feed(want, fn) {
    views = Array.from(new Set(views.concat(want || [])));
    if (fn) handlers.push(fn);
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ views }));
  }
  document.addEventListener('DOMContentLoaded', connect);

  return { $, esc, num, setText, setHTML, duration, when, api, feed };
})();
