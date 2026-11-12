/* ════════ Function switcher (b471) — one login, several functions ════════
   Shared by the CMMS (index.html) and FG Stock (fg.html).
   A person's functions = CMMS (unless "FG only") + each FG role they hold
   (users.fg_roles, set in CMMS › Manage › Users). Shown as a slider under the header:
     2–3 functions  -> equal segments
     4+ functions   -> 3 segments visible, swipe / scroll sideways for the rest
   Picking one never covers the screen. The title of each app stays the same.
   The first function after login = users.fg_default; after that the tab remembers. */
(function () {
  const DEF = {
    cmms: ['CMMS', 'CMMS', '#EA580C'],
    packing: ['แพคกิ้ง', 'Packing', '#0D9488'],
    packcheck: ['Checker แพคกิ้ง', 'Packing check', '#B45309'],
    receiver: ['ตรวจรับพาเลท', 'Pallet Checker', '#2563EB'],
    issuer: ['โหลดพาเลท', 'Pallet Loader', '#7C3AED'],
    checker: ['คลังสินค้า', 'Warehouse', '#0B6E99'],
  };
  const ALL = ['packing', 'packcheck', 'receiver', 'issuer', 'checker'];
  function list(me) {
    if (!me) return [];
    const fg = me.role === 'admin' || me.cmms_role === 'admin' || (me.fg_roles || []).includes('admin')
      ? ALL.slice() : (me.fg_roles || []).filter(r => ALL.includes(r));
    return (me.fg_only && fg.length ? [] : ['cmms']).concat(fg);
  }
  function current(me) {
    const fns = list(me);
    const m = location.search.match(/[?&]fn=([a-z]+)/);
    let f = m ? m[1] : '';
    if (!f) { try { f = sessionStorage.getItem('bflfp_fn') || ''; } catch (e) {} }
    if (!fns.includes(f)) f = fns.includes(me.fg_default) ? me.fg_default : fns[0];
    try { sessionStorage.setItem('bflfp_fn', f); } catch (e) {}
    return f;
  }
  function go(f, here) {
    try { sessionStorage.setItem('bflfp_fn', f); } catch (e) {}
    const onFg = /\/fg\.html$/.test(location.pathname);
    if (f === 'cmms') {
      try { sessionStorage.setItem('bflfp_app', 'cmms'); } catch (e) {}
      if (!onFg && here) return here(f);
      location.href = '/?fn=cmms';
    } else {
      if (onFg && here) return here(f);
      location.href = '/fg.html?fn=' + f;
    }
  }
  let STYLE = false;
  function css() {
    if (STYLE) return; STYLE = true;
    const s = document.createElement('style');
    s.textContent = `.fnbar{background:#0F172A;padding:6px 10px 8px;position:sticky;top:var(--fnbar-top,0);z-index:4}
.fnbar .tr{display:flex;gap:4px;background:#1E293B;border-radius:999px;padding:4px;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;max-width:720px}
.fnbar .tr::-webkit-scrollbar{display:none}
.fnbar button{flex:1 0 calc((100% - 8px)/3);min-width:0;scroll-snap-align:start;border:0;border-radius:999px;background:transparent;color:#CBD5E1;
  font:inherit;font-size:13.5px;min-height:40px;padding:4px 8px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;justify-content:center;gap:6px}
.fnbar.n2 button{flex-basis:calc((100% - 4px)/2)}
.fnbar button .d{width:8px;height:8px;border-radius:4px;flex:none}
.fnbar button.on{color:#fff;font-weight:700}
.fnbar button .b{background:#F59E0B;color:#0F172A;border-radius:99px;font-size:11px;font-weight:700;padding:0 6px}
.fnbar .more{color:#94A3B8;font-size:11px;text-align:center;margin-top:3px}
@media(min-width:821px){.fnbar .tr{display:inline-flex;flex-wrap:wrap;max-width:none;border-radius:14px}.fnbar button{flex:0 0 auto;min-width:140px;padding:4px 16px}.fnbar .more{display:none}}`;
    document.head.appendChild(s);
  }
  function render(el, me, cur, opts) {
    opts = opts || {};
    const fns = list(me);
    if (!el) return;
    if (fns.length < 2) { el.innerHTML = ''; el.className = ''; return; }
    css();
    const th = (opts.lang || 'TH') === 'TH';
    el.className = 'fnbar n' + Math.min(fns.length, 4);
    const badge = opts.badges || {};
    el.innerHTML = `<div class="tr" role="tablist" aria-label="${th ? 'เลือกงาน' : 'Switch function'}">` + fns.map(f => {
      const d = DEF[f], on = f === cur;
      const name = f === 'cmms' && me.cmms_role ? 'CMMS' : (th ? d[0] : d[1]);
      return `<button role="tab" aria-selected="${on}" class="${on ? 'on' : ''}" style="${on ? 'background:' + d[2] : ''}" data-fn="${f}">` +
        `<span class="d" style="background:${on ? '#fff' : d[2]}"></span><span>${name}</span>${badge[f] ? `<span class="b">${badge[f]}</span>` : ''}</button>`;
    }).join('') + `</div>` + (fns.length > 3 ? `<div class="more">${th ? '← เลื่อนดูงานอื่น →' : '← swipe for more →'}</div>` : '');
    el.querySelectorAll('button[data-fn]').forEach(b => b.onclick = () => { if (b.dataset.fn !== cur) go(b.dataset.fn, opts.here); });
    const a = el.querySelector('button.on');
    if (a && fns.length > 3) try { a.scrollIntoView({ inline: 'center', block: 'nearest' }); } catch (e) {}
  }
  window.FnBar = { list, current, go, render, DEF };
})();
