import io, sys
P = sys.argv[1]
s = io.open(P, encoding="utf-8").read()
def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, ("not found / not unique", old[:90], s.count(old))
    s = s.replace(old, new)

# --- PDF helper next to printA4
rep('''function printA4(title,sub,head,rows,foot,right){''',
r'''/* b506 — same table as Print A4, downloaded as a real PDF file (built on the server, Thai font) */
async function pdfA4(title,sub,head,rows,foot,right,file){
  try{
    const r=await fetch('/api/fg/pdf',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title,sub:`FG ${ME.factory_code||''} · ${sub||''}`,head,rows:rows.map(x=>x.map(v=>v==null?'':String(v))),foot,right:right||[],file:file||'fg-report'})});
    if(!r.ok){let m='HTTP '+r.status;try{const d=await r.json();m=(d.detail&&d.detail.msg)||d.detail||m;}catch(e){}throw new Error(m);}
    const b=await r.blob(),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=(file||'fg-report')+'.pdf';document.body.appendChild(a);a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},2000);
  }catch(e){alert(L('สร้าง PDF ไม่ได้: ','Could not make the PDF: ')+(e.message||e));}
}
function printA4(title,sub,head,rows,foot,right){''')

# --- dashboard: 1 day
rep('''${[[7,L('7 วัน','7 days')],[30,L('30 วัน','30 days')]''', '''${[[1,L('1 วัน','1 day')],[7,L('7 วัน','7 days')],[30,L('30 วัน','30 days')]''')

# --- history: time range, product + lot search, PDF, Location + Remark
rep('''let HF={d1:daysAgo(6),d2:today(),q:'',status:''},HROWS=[];''',
    '''let HF={d1:daysAgo(6),d2:today(),q:'',status:'',t1:'',t2:'',p:'',l:''},HROWS=[];''')
rep('''    <label class="f">${L('ค้นหา','Search')}<input class="in" id="hq" value="${esc(HF.q)}" placeholder="${L('ล็อต / สินค้า','lot / product')}" onkeydown="if(event.key==='Enter')hLoad('${kind}')"></label>
    <button class="btn pri" onclick="hLoad('${kind}')">${L('แสดง','Show')}</button><button class="btn" onclick="hCsv('${kind}')">Excel (CSV)</button><button class="btn" onclick="hA4('${kind}')">${L('พิมพ์ A4','Print A4')}</button></div><div id="hBody"></div>`;
  ta($('#hq'),taSrv('all'),()=>hLoad(kind));''',
'''    <label class="f">${L('เวลา จาก','Time from')}<input class="in" type="time" id="ht1" value="${HF.t1}" style="width:120px"></label><label class="f">${L('ถึง','to')}<input class="in" type="time" id="ht2" value="${HF.t2}" style="width:120px"></label>
    <label class="f">${L('สินค้า','Product')}<input class="in" id="hp" value="${esc(HF.p)}" placeholder="${L('เบอร์อาหาร / ชื่อ','code / name')}" onkeydown="if(event.key==='Enter')hLoad('${kind}')"></label>
    <label class="f">${L('ล็อต','Lot')}<input class="in" id="hl" value="${esc(HF.l)}" placeholder="Lot No." onkeydown="if(event.key==='Enter')hLoad('${kind}')" style="width:150px"></label>
    <button class="btn pri" onclick="hLoad('${kind}')">${L('แสดง','Show')}</button><button class="btn" onclick="hCsv('${kind}')">Excel (CSV)</button><button class="btn" onclick="hA4('${kind}')">${L('พิมพ์ A4','Print A4')}</button><button class="btn" onclick="hPdf('${kind}')">PDF</button></div><div id="hBody"></div>`;
  ta($('#hp'),taSrv('product'),()=>hLoad(kind));ta($('#hl'),taSrv('lot',()=>$('#hp').value.trim()),()=>hLoad(kind),0);''')
rep('''  HF={d1:$('#h1').value,d2:$('#h2').value,q:$('#hq').value.trim(),status:$('#hs').value};
  const B=$('#hBody'),u=`/api/fg/history?kind=${kind}&d1=${HF.d1}&d2=${HF.d2}&status=${HF.status}&q=${encodeURIComponent(HF.q)}&limit=3000`;loading(B,u);''',
'''  HF={d1:$('#h1').value,d2:$('#h2').value,q:'',status:$('#hs').value,t1:$('#ht1').value,t2:$('#ht2').value,p:$('#hp').value.trim(),l:$('#hl').value.trim()};
  const B=$('#hBody'),u=`/api/fg/history?kind=${kind}&d1=${HF.d1}&d2=${HF.d2}&status=${HF.status}&t1=${HF.t1}&t2=${HF.t2}&product=${encodeURIComponent(HF.p)}&lot=${encodeURIComponent(HF.l)}&limit=3000`;loading(B,u);''')
rep('''   R?XC('get_type',L('ประเภท','Get type')):XC('customer_id',L('ลูกค้า','Customer'))]).concat(R?[]:[XC('release_type',L('ประเภท','Type')),XC('plate',L('ทะเบียน','Plate'))])''',
'''   R?XC('get_type',L('ประเภท','Get type')):XC('customer_id',L('ลูกค้า','Customer'))]).concat(R?[XC('location',L('ที่จัดเก็บ','Location'))]:[XC('release_type',L('ประเภท','Type')),XC('plate',L('ทะเบียน','Plate'))])
   .concat([XC('_rm',L('หมายเหตุ','Remark'),{val:hRemark,fmt:r=>`<span class="muted">${esc(hRemark(r))}</span>`})])''')
rep('''function hA4(kind){const R=kind==='recv',HV=xView('h'+kind,HROWS);
  const tq=HV.reduce((a,r)=>a+(r.status==='confirmed'?r.qty:0),0),tk=HV.reduce((a,r)=>a+(r.status==='confirmed'?r.total_kg||0:0),0);
  printA4(R?'รายงานรับเข้า FG / FG receive report':'รายงานจ่ายออก FG / FG release report',`${fmtD(HF.d1)} – ${fmtD(HF.d2)}${HF.q?' · '+HF.q:''}`,
   ['Date','Time','Product','Name','Lot',R?'Pallet':'Customer','Qty','kg',R?'Receive man':'Release man',R?'Get type':'Type / mode','Status','Checker'],
   HV.map(r=>[fmtD(R?r.receive_date:r.release_date),R?r.receive_time:r.release_time,r.product_id,r.product_name,r.lot_no,R?r.pallet_no||'':r.customer_id,n0(r.qty),n0(r.total_kg),R?r.receive_man:r.release_man,R?r.get_type:r.release_type+(r.issue_mode?' · '+r.issue_mode:''),L(...(STL[r.status]||[r.status,r.status])),r.checker]),
   `${L('รวมที่ยืนยันแล้ว','Confirmed total')}: ${n0(tq)} ${L('หน่วย','units')} · ${n0(tk)} kg · ${HV.length} ${L('รายการ','rows')}`,[6,7]);}''',
'''const hRemark=r=>[r.memo,r.note].filter((x,i,a)=>x&&a.indexOf(x)===i).join(' · ');
function hRep(kind){const R=kind==='recv',HV=xView('h'+kind,HROWS);
  const tq=HV.reduce((a,r)=>a+(r.status==='confirmed'?r.qty:0),0),tk=HV.reduce((a,r)=>a+(r.status==='confirmed'?r.total_kg||0:0),0);
  const flt=[HF.t1||HF.t2?`${L('เวลา','time')} ${HF.t1||'00:00'}–${HF.t2||'23:59'}`:'',HF.p?L('สินค้า ','product ')+HF.p:'',HF.l?'lot '+HF.l:''].filter(Boolean).join(' · ');
  return [R?'รายงานรับเข้า FG / FG receive report':'รายงานจ่ายออก FG / FG release report',`${fmtD(HF.d1)} – ${fmtD(HF.d2)}${flt?' · '+flt:''}`,
   ['Date','Time','Product','Name','Lot',R?'Pallet':'Customer','Qty','kg',R?'Receive man':'Release man',R?'Get type':'Type / mode',R?'Location':'Plate','Status','Checker','Remark'],
   HV.map(r=>[fmtD(R?r.receive_date:r.release_date),R?r.receive_time:r.release_time,r.product_id,r.product_name,r.lot_no,R?r.pallet_no||'':r.customer_id,n0(r.qty),n0(r.total_kg),R?r.receive_man:r.release_man,R?r.get_type:r.release_type+(r.issue_mode?' · '+r.issue_mode:''),R?r.location||'':r.plate||'',L(...(STL[r.status]||[r.status,r.status])),r.checker,hRemark(r)]),
   `${L('รวมที่ยืนยันแล้ว','Confirmed total')}: ${n0(tq)} ${L('หน่วย','units')} · ${n0(tk)} kg · ${HV.length} ${L('รายการ','rows')}`,[6,7]];}
function hA4(kind){printA4(...hRep(kind));}
function hPdf(kind){const a=hRep(kind);pdfA4(...a,`fg-${kind==='recv'?'receive':'release'}_${HF.d1}_${HF.d2}`);}''')
rep('''R?'GetType':'Customer',R?'Location':'ReleaseType','Plate','Status','Checker','Checked at','Source'],''',
    '''R?'GetType':'Customer',R?'Location':'ReleaseType','Plate','Status','Checker','Checked at','Source','Remark'],''')
rep('''r.plate||'',r.status,r.checker,r.checked_at,r.src]));}''', '''r.plate||'',r.status,r.checker,r.checked_at,r.src,hRemark(r)]));}''')

# --- checker queue: date + time range, PDF
rep('''     <button class="btn" onclick="qPrint()">${L('พิมพ์แบบฟอร์มวันนี้','Print today’s form')}</button>
     <button class="btn" onclick="qCsv()">Excel (CSV)</button></div></div>''',
'''     <label class="f">${L('วันที่','Date')}<input class="in" type="date" id="qD" value="${QF.d}" onchange="qF()"></label>
     <label class="f">${L('เวลา จาก','Time from')}<input class="in" type="time" id="qT1" value="${QF.t1}" onchange="qF()" style="width:115px"></label>
     <label class="f">${L('ถึง','to')}<input class="in" type="time" id="qT2" value="${QF.t2}" onchange="qF()" style="width:115px"></label>
     <button class="btn" onclick="qPrint()">${L('พิมพ์แบบฟอร์ม','Print form')}</button>
     <button class="btn" onclick="qPdf()">PDF</button>
     <button class="btn" onclick="qCsv()">Excel (CSV)</button></div></div>''')
rep('''async function qLoad(){
  QSEL={};
  try{QROWS=(await api('/api/fg/queue?kind='+QK)).rows;}catch(e){$('#qBody').innerHTML=err(e);return;}''',
'''/* b506: date + time range — filters the waiting list (by phone-check time) and is the period of the printed form / PDF */
let QF={d:'',t1:'',t2:''},QALL=[];   // empty date = every waiting row (nothing hidden); form / PDF use today when empty
const qAt=r=>(QK==='recv'?r.recv_at:r.issue_at)||'';
function qIn(dt){const d=(dt||'').slice(0,10),t=(dt||'').slice(11,16);if(QF.d&&d!==QF.d)return false;
  if(QF.t1&&QF.t2&&QF.t1>QF.t2)return t>=QF.t1||t<=QF.t2;return(!QF.t1||t>=QF.t1)&&(!QF.t2||t<=QF.t2);}
function qF(){QF={d:$('#qD').value,t1:$('#qT1').value,t2:$('#qT2').value};QSEL={};QROWS=QALL.filter(r=>qIn(qAt(r)));qDraw();}
async function qLoad(){
  QSEL={};
  try{QALL=(await api('/api/fg/queue?kind='+QK)).rows;QROWS=QALL.filter(r=>qIn(qAt(r)));}catch(e){$('#qBody').innerHTML=err(e);return;}''')
rep('''async function qPrint(){
  let rows=[];
  try{rows=(await api(`/api/fg/history?kind=${QK}&d1=${today()}&d2=${today()}&limit=5000`)).rows.filter(r=>r.src==='app'&&(r.status==='waiting'||r.status==='confirmed'));}catch(e){alert(e.message);return;}
  rows.reverse();''',
'''async function qFormRows(){
  const d=QF.d||today();
  const rows=(await api(`/api/fg/history?kind=${QK}&d1=${d}&d2=${d}&t1=${QF.t1}&t2=${QF.t2}&limit=5000`)).rows.filter(r=>r.src==='app'&&(r.status==='waiting'||r.status==='confirmed'));
  return rows.reverse();
}
async function qPdf(){
  let rows=[];try{rows=await qFormRows();}catch(e){alert(e.message);return;}
  const R=QK==='recv',d=QF.d||today(),tot=rows.reduce((a,r)=>a+(r.total_kg||0),0);
  pdfA4(`F-WI04-WHD01-04 · บันทึกการ${R?'รับเข้า':'จ่ายออก'} FG / FG ${R?'receive':'issue'} record`,
    `${fmtD(d)}${QF.t1||QF.t2?` · ${L('เวลา','time')} ${QF.t1||'00:00'}–${QF.t2||'23:59'}`:''}`,
    ['No.','Pallet','Product','Name','Lot','Packaging','Qty','kg/unit','Total kg',R?'Packed by':'Customer',R?'Location':'Plate','Phone check','Status','Checker','Remark'],
    rows.map((r,i)=>[i+1,r.pallet_no||'',r.product_id,r.product_name,r.lot_no,r.packaging||'',n0(r.qty),R?n0(r.weight_unit):(r.qty?n0((r.total_kg||0)/r.qty):''),n0(r.total_kg),R?r.receive_man:r.customer_id,R?r.location||'':r.plate||'',
      `${R?r.recv_by:r.issue_by} ${(R?r.recv_at:r.issue_at||'').slice(11,16)}`,L(...(STL[r.status]||[r.status,r.status])),r.checker||'',r.note||r.memo||'']),
    `${L('ยอดรวม','Total')}: ${n0(tot)} kg · ${rows.length} ${L('รายการ','rows')}`,[0,6,7,8],`fg-form-${QK}_${d}`);
}
async function qPrint(){
  let rows=[];
  try{rows=await qFormRows();}catch(e){alert(e.message);return;}''')
rep('''     <div style="display:flex;justify-content:space-between;margin:3px 0">วันที่ ${fmtD(today())}<span>เวลา ${now}</span></div>''',
    '''     <div style="display:flex;justify-content:space-between;margin:3px 0">วันที่ ${fmtD(list?(QF.d||today()):today())}<span>เวลา ${list&&(QF.t1||QF.t2)?(QF.t1||'00:00')+' – '+(QF.t2||'23:59'):now}</span></div>''')

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok")
