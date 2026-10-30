import io, sys
P = sys.argv[1]
s = io.open(P, encoding="utf-8").read()
def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, ("not found / not unique", old[:90], s.count(old))
    s = s.replace(old, new)

# --- CSS
rep('''.scanbig svg{width:42px;height:42px}''',
'''.scanbig svg{width:42px;height:42px}
/* b491 packing checker: USB scanner box + verify card */
.usbw{border:2px dashed #CBD5E1;border-radius:16px;padding:18px;background:#fff;margin:10px 0 14px;text-align:center}
.usbw.on{border-color:#16A34A;background:#F0FDF4}
.usbw .st{font-weight:700;font-size:15px;margin-bottom:10px;color:#64748B;display:flex;align-items:center;justify-content:center;gap:8px}
.usbw.on .st{color:#15803D}.usbw .st i{width:12px;height:12px;border-radius:6px;background:#94A3B8;display:inline-block}
.usbw.on .st i{background:#22C55E;box-shadow:0 0 0 4px #BBF7D0;animation:usbp 1.4s infinite}
@keyframes usbp{50%{box-shadow:0 0 0 7px #DCFCE7}}
.usbw input{width:100%;height:52px;font-size:17px;text-align:center;border:1px solid #CBD5E1;border-radius:10px;font-family:inherit}
.vgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin:10px 0}
.vgrid div{background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px}.vgrid small{display:block;color:var(--ink2);font-size:12px}
.vgrid b{font-size:16px;font-weight:600;word-break:break-word}.vgrid .big b{font-size:20px}''')

# --- pages per function
rep('''const FN_PAGES={packing:['slip'],packcheck:['slipchk','slip'],''',
    '''const FN_PAGES={packing:['slip'],packcheck:['pscan','papproved'],''')
rep('''  admin:['packing','lists','prod','cust','people','users']};''',
    '''  admin:['paudit','packing','lists','prod','cust','people','users']};''')
rep('''  ['slipchk',IC.check,L('Checker แพคกิ้ง','Packing check'),'packcheck',''],''',
'''  ['slipchk',IC.check,L('Checker แพคกิ้ง','Packing check'),'packcheck',''],
  ['pscan',IC.scan,L('สแกนตรวจใบพาเลท','Scan & verify slip'),'packcheck',''],
  ['papproved',IC.check,L('รายการที่ตรวจแล้ว','Checked list'),'packcheck',''],''')
rep('''  ['packing',IC.pack,L('ข้อมูลแพคกิ้ง','Packing records'),'admin','M'],''',
'''  ['paudit',IC.hist,L('ประวัติพิมพ์ / ตรวจใบพาเลท','Slip print & check audit'),'admin','M'],
  ['packing',IC.pack,L('ข้อมูลแพคกิ้ง','Packing records'),'admin','M'],''')
rep('''['home','slip','slips','slipchk','recv',''', '''['home','slip','slips','slipchk','pscan','papproved','recv',''')
rep('''slipchk:pgSlipChk,''', '''slipchk:pgSlipChk,pscan:pgPScan,papproved:pgPApproved,paudit:pgPAudit,''')
rep('''packcheck:L('ตรวจใบพาเลทก่อนส่ง FG','approve slips before FG')''',
    '''packcheck:L('PC / มือถือ: สแกน QR ใบพาเลท → ตรวจ → อนุมัติ','PC (USB scanner) or phone: scan slip QR → verify → approve')''')

# --- A6 reprint mark
rep('''async function slipHtml(s){''', '''async function slipHtml(s,copy){''')
rep('''<span>FG ${esc(ME.factory_code)} · ${esc(s.pack_type||'')}''',
    '''<span>${copy>1?`<b style="border:1.5px solid #000;padding:0 4px;margin-right:4px">REPRINT · COPY ${copy}</b>`:''}FG ${esc(ME.factory_code)} · ${esc(s.pack_type||'')}''')

# --- print: verify first, warn on reprint
rep('''async function printSlips(list){
  if(!list||!list.length)return;
  let pages='';for(const s of list)pages+=await slipHtml(s);
  const w=window.open('','_blank');
  if(!w){alert(L('เบราว์เซอร์บล็อกหน้าต่างพิมพ์ — อนุญาต pop-up','The browser blocked the print window — allow pop-ups'));return;}
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Pallet slip</title><style>${A6CSS}</style></head><body>${pages}<script>window.onload=function(){setTimeout(function(){window.print()},200)}<\\/script></body></html>`);
  w.document.close();
  if(can('pack'))post('/api/fg/slips-printed',{ids:list.map(s=>s.id)}).catch(()=>{});
}''',
r'''/* b491 — every print goes through a confirm step. A slip that was printed before is a REPRINT:
   red warning (printed N× · last by · when), a reason is required, and the copy is marked on the slip. */
let PRQ=null;
const PRWHY=()=>[['jam',L('กระดาษติด / พิมพ์ไม่ออก','Printer jam / bad print')],['damaged',L('ใบพาเลทชำรุด / เปื้อน','Slip damaged / dirty')],
  ['lost',L('ใบพาเลทหาย','Slip lost')],['fixed',L('แก้ไขข้อมูลแล้ว','Data was corrected')],['other',L('อื่นๆ','Other')]];
async function printSlips(list){
  list=(list||[]).filter(Boolean);if(!list.length)return;
  let pr=[];
  if(can('pack')){try{pr=(await post('/api/fg/slips-print-check',{ids:list.map(s=>s.id)})).printed||[];}catch(e){}}
  const PM={};pr.forEach(p=>PM[p.id]=p);PRQ={list,PM};
  const re=pr.length;
  dlg(`<h2>${re?'⚠️ '+L('ใบพาเลทนี้เคยพิมพ์แล้ว','Already printed — this is a REPRINT'):L('ยืนยันการพิมพ์ใบพาเลท','Confirm pallet slip print')}</h2>
   ${re?`<div class="msg m-bad">${L('ใบต่อไปนี้พิมพ์ไปแล้ว — การพิมพ์ซ้ำจะถูกบันทึกในชื่อของคุณ','These slips were printed before — the reprint is recorded under your name')}</div>`:''}
   <div class="tw" style="max-height:40vh;overflow:auto"><table><thead><tr><th>${L('พาเลท','Pallet')}</th><th>Lot No.</th><th>${L('เบอร์อาหาร','Product')}</th><th class="r">${L('ถุง','Bags')}</th><th>${L('พิมพ์แล้ว','Printed')}</th></tr></thead><tbody>
    ${list.map(s=>{const p=PM[s.id];return `<tr style="${p?'background:#FEF2F2':''}"><td><b>#${esc(s.pallet_no)}</b></td><td class="mono">${esc(s.lot_no)}</td><td>${esc(s.product_code)}</td><td class="r">${n0(s.qty)}</td>
      <td>${p?`<b style="color:#B91C1C">${p.print_count}× </b><span class="muted">${esc(p.last_by||'-')} · ${fmtT(p.last_at)}</span>`:`<span class="muted">${L('ยังไม่เคยพิมพ์','first print')}</span>`}</td></tr>`;}).join('')}</tbody></table></div>
   ${re?`<label class="f" style="margin-top:12px">${L('เหตุผลที่พิมพ์ซ้ำ','Reason for reprint')} *<select class="in" id="prWhy" onchange="prChk()"><option value="">— ${L('เลือก','choose')} —</option>${PRWHY().map(w=>`<option value="${w[0]}">${esc(w[1])}</option>`).join('')}</select></label>
    <label class="f" style="margin-top:8px">${L('รายละเอียด','Details')}<input class="in" id="prTx" oninput="prChk()" placeholder="${L('จำเป็นถ้าเลือก อื่นๆ','required for Other')}"></label>
    <label style="display:flex;gap:8px;align-items:flex-start;margin-top:10px;font-size:13.5px"><input type="checkbox" id="prAck" onchange="prChk()" style="margin-top:3px">
     <span>${L('ฉันจะเก็บ / ทำลายใบเก่า ให้มีใบพาเลทเพียงใบเดียวบนพาเลท','I will remove / destroy the old slip so only ONE slip is on the pallet')}</span></label>`:''}
   <div class="btns" style="justify-content:flex-end;margin-top:14px"><button class="btn" onclick="closeDlg()">${L('ยกเลิก','Cancel')}</button>
    <button class="btn ${re?'bad':'pri'}" id="prGo" ${re?'disabled':''} onclick="prGo()">${re?L('พิมพ์ซ้ำ','Reprint'):L('พิมพ์','Print')} ${list.length} ${L('ใบ','slip(s)')}</button></div>`);
}
function prReason(){const w=$('#prWhy');if(!w)return '';const t=($('#prTx').value||'').trim(),o=PRWHY().find(x=>x[0]===w.value);return o?(o[1]+(t?' — '+t:'')):'';}
function prChk(){const b=$('#prGo');if(!b)return;const w=$('#prWhy').value,t=($('#prTx').value||'').trim();
  b.disabled=!(w&&(w!=='other'||t)&&$('#prAck').checked);}
async function prGo(){
  if(!PRQ)return;const {list,PM}=PRQ;const reason=prReason();
  if(Object.keys(PM).length&&!reason)return;
  const w=window.open('','_blank');   // opened inside the click so the browser does not block it
  if(!w){alert(L('เบราว์เซอร์บล็อกหน้าต่างพิมพ์ — อนุญาต pop-up','The browser blocked the print window — allow pop-ups'));return;}
  closeDlg();
  w.document.write('<p style="font-family:sans-serif">Preparing…</p>');
  let pages='';for(const s of list)pages+=await slipHtml(s,PM[s.id]?PM[s.id].print_count+1:1);
  w.document.open();
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Pallet slip</title><style>${A6CSS}</style></head><body>${pages}<script>window.onload=function(){setTimeout(function(){window.print()},200)}<\/script></body></html>`);
  w.document.close();
  if(can('pack'))post('/api/fg/slips-printed',{ids:list.map(s=>s.id),reason}).catch(()=>{});
  PRQ=null;
}''')

# --- new pages (before the lists section)
rep('''/* ---------- lists (machines, lines) ---------- */''',
r'''/* ════════ b491 Packing checker: scan the printed slip → verify → approve. PC = USB scanner, phone = camera ════════ */
let PSMODE=(()=>{try{const v=localStorage.getItem('fg_scan_mode');if(v)return v;}catch(e){}return matchMedia('(pointer:fine)').matches?'usb':'camera';})();
let PS=null,PSLAST='';
function pgPScan(){
  PS=null;
  $('#app').innerHTML=`<div class="ph" style="max-width:820px"><h1>${L('สแกนตรวจใบพาเลท','Scan & verify pallet slip')}</h1>
   <p class="sub">${L('สแกน QR บนใบพาเลทที่พิมพ์แล้ว ตรวจกับพาเลทจริง แล้วอนุมัติ — FG รับได้เฉพาะใบที่อนุมัติ','Scan the QR on the printed slip, check it against the real pallet, then approve — FG can only receive approved slips')}</p>
   <div class="tabs" style="margin-bottom:6px"><button class="tab ${PSMODE==='usb'?'on':''}" onclick="psMode('usb')">⌨ ${L('เครื่องสแกน USB (PC)','USB scanner (PC)')}</button>
    <button class="tab ${PSMODE==='camera'?'on':''}" onclick="psMode('camera')">📷 ${L('กล้อง (มือถือ)','Camera (phone)')}</button></div>
   <div id="psIn"></div><div id="psLast">${PSLAST}</div><div id="psBody"></div></div>`;
  psIn();
}
function psMode(m){PSMODE=m;try{localStorage.setItem('fg_scan_mode',m)}catch(e){}pgPScan();}
function psIn(){
  const B=$('#psIn');if(!B)return;
  if(PSMODE==='camera'){B.innerHTML=scanBox(L('สแกน QR ใบพาเลท','Scan pallet slip QR'),'psCam','#B45309');return;}
  B.innerHTML=`<div class="usbw" id="usbw" onclick="psFocus()"><div class="st"><i></i><span id="usbst"></span></div>
    <input id="psUsb" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${L('ยิง QR ด้วยเครื่องสแกน USB ที่นี่','Scan the QR with the USB scanner here')}"
     onfocus="psSt()" onblur="psSt();setTimeout(psFocus,400)" onkeydown="if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();const v=this.value;this.value='';if(v.trim())psGot(v,'usb')}"></div>`;
  psFocus();psSt();
}
function psCam(v){psGot(v,'camera');}
function psSt(){const w=$('#usbw'),s=$('#usbst');if(!w||!s)return;const on=document.activeElement===$('#psUsb');w.classList.toggle('on',on);
  s.textContent=on?L('พร้อม — รอสแกน','Ready — waiting for scan'):L('คลิกที่นี่ก่อนสแกน','Click here before scanning');}
function psFocus(){if(PAGE!=='pscan'||PSMODE!=='usb'||$('#dlg'))return;const a=document.activeElement;
  if(a&&a!==document.body&&a.id!=='psUsb'&&/INPUT|TEXTAREA|SELECT/.test(a.tagName))return;const i=$('#psUsb');if(i)i.focus();}
let PSVIA='usb';
async function psGot(text,via){
  PSVIA=via||PSMODE;
  const B=$('#psBody');B.innerHTML=`<div class="card">${L('กำลังค้นหา…','Looking up…')}</div>`;
  try{PS=await post('/api/fg/slips-scan',{text,via:PSVIA});}
  catch(e){B.innerHTML=err(e);if(navigator.vibrate)try{navigator.vibrate([60,60,60])}catch(x){};psFocus();return;}
  psDraw();psFocus();
}
function psDraw(){
  const s=PS.slip,pr=PS.prints||[],n=s.print_count||0,B=$('#psBody');
  const can_act=s.status==='packed'&&(s.pc_status==='pending'||s.pc_status==='rejected');
  let banner='';
  if(s.status==='cancelled')banner=`<div class="msg m-bad">${L('ใบนี้ถูกยกเลิกแล้ว — ห้ามใช้','This slip is CANCELLED — do not use it')}</div>`;
  else if(s.status!=='packed')banner=`<div class="msg m-info">${esc(L(...(STL[s.status]||[s.status,s.status])))} — ${L('ไม่ต้องตรวจ','nothing to check')}</div>`;
  else if(s.pc_status==='approved')banner=`<div class="msg m-ok">✓ ${L('อนุมัติแล้วโดย','Already approved by')} ${esc(s.pc_by)} · ${fmtT(s.pc_at)}</div>`;
  else if(s.pc_status==='rejected')banner=`<div class="msg m-warn">${L('เคยไม่อนุมัติ:','Rejected before:')} ${esc(s.pc_note||'')} (${esc(s.pc_by)})</div>`;
  const v=(l,x,c)=>`<div class="${c||''}"><small>${l}</small><b>${x}</b></div>`;
  const pcc={pending:'background:#FEF3C7;color:#92400E',approved:'background:#DCFCE7;color:#166534',rejected:'background:#FEE2E2;color:#991B1B'}[s.pc_status]||'background:#EEF2F6';
  B.innerHTML=`<div class="card" style="border:2px solid ${can_act?'#F59E0B':'var(--line)'}">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap"><span class="mono" style="font-size:22px;font-weight:700">${esc(s.lot_no)} · #${esc(s.pallet_no)}</span>
     <span class="pill" style="${pcc}">${esc(L(...(PCL[s.pc_status]||[s.pc_status,s.pc_status])))}</span></div>
    ${banner}
    ${n>1?`<div class="msg m-bad">⚠️ ${L('ใบนี้พิมพ์แล้ว','This slip was printed')} ${n} ${L('ครั้ง — ตรวจว่ามีใบพาเลทใบเดียวบนพาเลท','times — make sure only ONE slip is on the pallet')}
      <div style="font-weight:400;font-size:13px;margin-top:4px">${pr.filter(p=>p.reprint).map(p=>`${L('ฉบับ','Copy')} ${p.copy_no}: ${esc(p.user_name)} · ${fmtT(p.printed_at)} · ${esc(p.reason||'')}`).join('<br>')}</div></div>`:''}
    ${n===0&&s.status==='packed'?`<div class="msg m-warn">${L('ระบบไม่มีบันทึกการพิมพ์ใบนี้','No print recorded for this slip')}</div>`:''}
    <div class="vgrid">
     ${v(L('เบอร์อาหาร','Formula code'),esc(s.product_code),'big')}${v(L('ชื่อสินค้า','Formula name'),esc(s.product_name))}
     ${v(L('จำนวนถุง','Bags'),n0(s.qty)+' '+esc(s.unit||''),'big')}${v(L('กก./ถุง · น้ำหนักรวม','kg/bag · total'),esc(s.bag_weight||n0(s.weight_unit))+' · '+n0(s.total_weight)+' kg')}
     ${v('MFG → EXP',fmtD(s.mfg)+' → '+fmtD(s.exp))}${v(L('เครื่องแพค / ไลน์','Machine / line'),esc(s.pack_machine||'-')+' · '+esc(s.pd_line||'-'))}
     ${v(L('ผู้แพค','Staff'),esc(s.staff_name||'-'))}${v(L('เวลาแพค / พิมพ์','Packed / printed'),fmtT(s.packed_at)+' · '+n+'×')}</div>
    ${can_act?`<div style="display:flex;gap:10px;margin-top:6px"><button class="btn bad big" style="flex:1" onclick="psAct('reject')">${L('ไม่อนุมัติ','Reject')}</button>
      <button class="btn ok big" style="flex:2" onclick="psAct('approve')">✓ ${L('ถูกต้อง — อนุมัติ → ส่ง FG','Correct — approve → to FG')}</button></div>`:''}
    <details style="margin-top:10px"><summary class="muted" style="cursor:pointer">${L('ประวัติขั้นตอน','Audit trail')}</summary>${logList(PS.log)}</details></div>`;
}
async function psAct(action){
  const s=PS.slip;let note='';
  if(action==='reject'){note=prompt(L('เหตุผลที่ไม่อนุมัติ','Reason for rejecting'))||'';if(!note.trim()){psFocus();return;}}
  try{const d=await post('/api/fg/slips-check',{ids:[s.id],action,note,via:PSVIA});
    if(!d.done)throw new Error(L('ใบนี้ถูกตรวจไปแล้ว','This slip was already checked'));}
  catch(e){$('#psBody').insertAdjacentHTML('afterbegin',err(e));psFocus();return;}
  PSLAST=`<div class="msg ${action==='approve'?'m-ok':'m-bad'}">${action==='approve'?'✓ '+L('อนุมัติแล้ว','Approved'):'✗ '+L('ไม่อนุมัติ','Rejected')}: ${esc(s.lot_no)} · #${esc(s.pallet_no)} · ${esc(s.product_code)} <span class="muted">${new Date().toTimeString().slice(0,5)}</span></div>`;
  PS=null;$('#psBody').innerHTML='';$('#psLast').innerHTML=PSLAST;psFocus();
}

/* checked list — what the packing checker approved / rejected, by check date */
let PAF={d1:today(),d2:today(),pc:'approved',me:0,q:''};
async function pgPApproved(){
  $('#app').innerHTML=`<h1>${L('รายการที่ตรวจแล้ว','Checked slips')}</h1><p class="sub">${L('ใบพาเลทที่ Checker แพคกิ้งอนุมัติ / ไม่อนุมัติ ตามวันที่ตรวจ','pallet slips approved / rejected by the packing checker, by check date')}</p>
   <div class="card" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end">
    <label class="f">${L('จาก','From')}<input class="in" type="date" id="paD1" value="${PAF.d1}"></label><label class="f">${L('ถึง','To')}<input class="in" type="date" id="paD2" value="${PAF.d2}"></label>
    <label class="f" style="flex:1;min-width:160px">${L('ค้นหา','Search')}<input class="in" id="paQ" value="${esc(PAF.q)}" placeholder="Lot / ${L('เบอร์อาหาร','product')}"></label>
    <label style="display:flex;gap:6px;align-items:center;min-height:40px"><input type="checkbox" id="paMe" ${PAF.me?'checked':''}> ${L('เฉพาะของฉัน','Only mine')}</label>
    <button class="btn pri" onclick="paLoad()">${L('แสดง','Show')}</button></div>
   <div class="tabs" id="paT"></div><div id="paBody"></div>`;
  paLoad();
}
async function paLoad(){
  PAF.d1=$('#paD1').value||today();PAF.d2=$('#paD2').value||PAF.d1;PAF.q=$('#paQ').value.trim();PAF.me=$('#paMe').checked?1:0;
  const B=$('#paBody');B.innerHTML=LOADH;
  let d;try{d=await api(`/api/fg/slips?pc=${PAF.pc}&datef=pc&d1=${PAF.d1}&d2=${PAF.d2}&q=${encodeURIComponent(PAF.q)}${PAF.me?'&by=me':''}&limit=2000`);}catch(e){B.innerHTML=err(e);return;}
  $('#paT').innerHTML=['approved','rejected'].map(k=>`<button class="tab ${PAF.pc===k?'on':''}" onclick="PAF.pc='${k}';paLoad()">${esc(L(...PCL[k]))}${PAF.pc===k?' · '+d.rows.length:''}</button>`).join('')
    +`<span class="muted" style="margin-left:auto;align-self:center">${L('รอสแกนตรวจ','Waiting to be scanned')}: <b>${d.counts.pending||0}</b></span>`;
  SLIPS=d.rows;
  const tb=d.rows.reduce((a,s)=>a+(+s.qty||0),0),tk=d.rows.reduce((a,s)=>a+(+s.total_weight||0),0);
  B.innerHTML=`<div class="tw"><table><thead><tr><th>${L('เวลาตรวจ','Checked at')}</th><th>${L('ผู้ตรวจ','Checked by')}</th><th>${L('พาเลท','Pallet')}</th><th>${L('เบอร์อาหาร','Product')}</th><th>Lot No.</th><th>MFG / EXP</th><th class="r">${L('ถุง','Bags')}</th><th class="r">kg</th><th>${L('พิมพ์','Printed')}</th><th>${L('สถานะ FG','FG status')}</th><th></th></tr></thead><tbody>
   ${d.rows.map(s=>`<tr><td class="nw">${fmtT(s.pc_at)}</td><td>${esc(s.pc_by)}${s.pc_note?`<div class="muted">${esc(s.pc_note)}</div>`:''}</td><td><b>#${esc(s.pallet_no)}</b></td><td>${esc(s.product_code)}<div class="muted">${esc(s.product_name)}</div></td>
    <td class="mono nw"><b>${esc(s.lot_no)}</b></td><td class="nw">${fmtD(s.mfg)}<div class="muted">${fmtD(s.exp)}</div></td><td class="r">${n0(s.qty)}</td><td class="r">${n0(s.total_weight)}</td>
    <td>${(s.print_count||0)>1?`<b style="color:#B91C1C">${s.print_count}×</b>`:(s.print_count||0)+'×'}</td><td>${stPill(s.status)}</td>
    <td class="nw"><button class="btn sm" onclick="slipPreview(${s.id})">A6</button> <button class="btn sm" onclick="logDlg('packing',${s.id},'#${esc(s.pallet_no)}')">${L('ประวัติ','Log')}</button></td></tr>`).join('')
    ||`<tr><td colspan="11" class="muted" style="padding:20px;text-align:center">${L('ไม่มีรายการ','Nothing here')}</td></tr>`}</tbody>
   ${d.rows.length?`<tfoot><tr><td colspan="6">${d.rows.length} ${L('พาเลท','pallets')}</td><td class="r">${n0(tb)}</td><td class="r">${n0(tk)}</td><td colspan="3"></td></tr></tfoot>`:''}</table></div>`;
}

/* audit — every print / reprint / scan / approve / reject of a pallet slip, per user */
const PAL={slip_created:['สร้างใบพาเลท','Slip created'],slip_edited:['แก้ไขใบพาเลท','Slip edited'],slip_fixed_after_fg_reject:['แก้ไขหลัง FG ไม่รับ','Fixed after FG reject'],
  printed:['พิมพ์ (ฉบับแรก)','Printed (1st copy)'],reprinted:['พิมพ์ซ้ำ','REPRINTED'],checker_scanned:['Checker สแกน','Checker scanned'],
  pack_approve:['Checker อนุมัติ','Checker approved'],pack_reject:['Checker ไม่อนุมัติ','Checker rejected'],cancelled:['ยกเลิก','Cancelled'],
  moved_to_fg:['FG รับเข้า','Received by FG'],rejected_by_fg:['FG ไม่รับ','Rejected by FG'],packed:['กลับเป็นแพคแล้ว','Back to packed'],slip_from_access:['สร้างจาก Access','Made from Access']};
Object.assign(LOGL,PAL);
let PDF={d1:daysAgo(6),d2:today(),user:'',action:'',q:''},PDD=null;
async function pgPAudit(){
  $('#app').innerHTML=`<h1>${L('ประวัติพิมพ์ / ตรวจใบพาเลท','Pallet slip audit trail')}</h1><p class="sub">${L('ใครพิมพ์ พิมพ์ซ้ำ สแกน อนุมัติ ใบไหน เมื่อไร — แยกตามผู้ใช้','who printed, reprinted, scanned and approved which slip, and when — per user')}</p>
   <div class="card" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end">
    <label class="f">${L('จาก','From')}<input class="in" type="date" id="pdD1" value="${PDF.d1}"></label><label class="f">${L('ถึง','To')}<input class="in" type="date" id="pdD2" value="${PDF.d2}"></label>
    <label class="f">${L('ผู้ใช้','User')}<select class="in" id="pdU" onchange="PDF.user=this.value"><option value="">${L('ทุกคน','Everyone')}</option></select></label>
    <label class="f">${L('การกระทำ','Action')}<select class="in" id="pdA"><option value="">${L('ทั้งหมด','All')}</option>${Object.keys(PAL).map(k=>`<option value="${k}" ${PDF.action===k?'selected':''}>${esc(L(...PAL[k]))}</option>`).join('')}</select></label>
    <label class="f" style="flex:1;min-width:140px">${L('ค้นหา','Search')}<input class="in" id="pdQ" value="${esc(PDF.q)}" placeholder="Lot / ${L('เบอร์อาหาร','product')}"></label>
    <button class="btn pri" onclick="pdLoad()">${L('แสดง','Show')}</button><button class="btn" onclick="pdCsv()">CSV</button></div>
   <div id="pdSum"></div><div id="pdBody"></div>`;
  pdLoad();
}
function pdUser(u){PDF.user=u;pdLoad();}
async function pdLoad(){
  PDF.d1=$('#pdD1').value;PDF.d2=$('#pdD2').value;PDF.action=$('#pdA').value;PDF.q=$('#pdQ').value.trim();
  $('#pdBody').innerHTML=LOADH;
  try{PDD=await api(`/api/fg/packing-audit?d1=${PDF.d1}&d2=${PDF.d2}&user=${encodeURIComponent(PDF.user)}&action=${PDF.action}&q=${encodeURIComponent(PDF.q)}`);}catch(e){$('#pdBody').innerHTML=err(e);return;}
  $('#pdU').innerHTML=`<option value="">${L('ทุกคน','Everyone')}</option>`+PDD.users.map(u=>`<option ${u===PDF.user?'selected':''}>${esc(u)}</option>`).join('');
  const by={};PDD.summary.forEach(r=>{(by[r.user_name]=by[r.user_name]||{})[r.action]=r.n;});
  const cols=['printed','reprinted','checker_scanned','pack_approve','pack_reject','slip_created','cancelled'];
  PDD.ul=Object.keys(by).sort();
  $('#pdSum').innerHTML=`<div class="tw" style="margin-bottom:12px"><table><thead><tr><th>${L('ผู้ใช้ (คลิกเพื่อกรอง)','User (click to filter)')}</th>${cols.map(c=>`<th class="r">${esc(L(...PAL[c]))}</th>`).join('')}</tr></thead><tbody>
   ${PDD.ul.map((u,i)=>`<tr style="cursor:pointer" onclick="pdUser(PDD.ul[${i}])"><td><b>${esc(u||'-')}</b></td>${cols.map(c=>`<td class="r" style="${c==='reprinted'&&by[u][c]?'color:#B91C1C;font-weight:700':''}">${by[u][c]||''}</td>`).join('')}</tr>`).join('')
   ||`<tr><td colspan="${cols.length+1}" class="muted" style="padding:14px;text-align:center">${L('ไม่มีรายการ','Nothing in this period')}</td></tr>`}</tbody></table></div>`;
  $('#pdBody').innerHTML=`<div class="tw"><table><thead><tr><th>${L('เวลา','Time')}</th><th>${L('ผู้ใช้','User')}</th><th>${L('การกระทำ','Action')}</th><th>${L('พาเลท','Pallet')}</th><th>Lot No.</th><th>${L('เบอร์อาหาร','Product')}</th><th>${L('รายละเอียด','Details')}</th></tr></thead><tbody>
   ${PDD.rows.map(r=>`<tr style="${r.action==='reprinted'?'background:#FEF2F2':''}"><td class="nw">${fmtT(r.created_at)}</td><td>${esc(r.user_name)}</td><td><b>${esc(L(...(PAL[r.action]||[r.action,r.action])))}</b></td>
    <td>#${esc(r.pallet_no||'')}</td><td class="mono nw">${esc(r.lot_no||'')}</td><td>${esc(r.product_code||'')}</td><td>${esc(r.note||'')}</td></tr>`).join('')
   ||`<tr><td colspan="7" class="muted" style="padding:20px;text-align:center">${L('ไม่มีรายการ','Nothing here')}</td></tr>`}</tbody></table></div>
   ${PDD.rows.length>=3000?`<div class="muted">${L('แสดง 3000 รายการล่าสุด — ย่อช่วงวันที่','showing the latest 3000 — narrow the dates')}</div>`:''}`;
}
function pdCsv(){if(!PDD)return;csv(`slip-audit_${PDF.d1}_${PDF.d2}.csv`,['Time','User','Action','Pallet','Lot','Product','Details'],
  PDD.rows.map(r=>[r.created_at,r.user_name,(PAL[r.action]||[r.action,r.action])[1],r.pallet_no,r.lot_no,r.product_code,r.note]));}

/* ---------- lists (machines, lines) ---------- */''')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok")
