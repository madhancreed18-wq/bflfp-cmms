import io, sys
P = sys.argv[1]
s = io.open(P, encoding="utf-8").read()
def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, ("not found / not unique", old[:90], s.count(old))
    s = s.replace(old, new)

rep("""'people','users','accimp']};""", """'people','users','accimp','accarch']};""")
rep("""  ['accimp',IC.master,L('นำเข้า Access','Import Access'),'admin','M']]""",
    """  ['accimp',IC.master,L('นำเข้า Access','Import Access'),'admin','M'],
  ['accarch',IC.hist,L('ข้อมูลเก่า Access (ทุกตาราง)','Old Access data (all tables)'),'admin','M']]""")
rep("""accimp:pgAccImp}[p])();""", """accimp:pgAccImp,accarch:pgAccArch}[p])();""")
rep("""async function pgUsers(){""",
r"""/* ════════ b501: the whole old Access database — every table, every column, newest first ════════ */
let ARC={tbl:'',q:'',page:1},ARCD=null;
async function pgAccArch(){
  let d;try{d=await api('/api/fg/access-archive');}catch(e){$('#app').innerHTML=err(e);return;}
  if(!ARC.tbl||!d.tables.some(t=>t.tbl===ARC.tbl))ARC.tbl=(d.tables.find(t=>t.tbl==='tbl_Receive_Product')||d.tables[0]||{}).tbl||'';
  $('#app').innerHTML=`<h1>${L('ข้อมูลเก่าจาก Access','Old Access data')}</h1>
   <p class="sub">${L('ทุกตารางและทุกคอลัมน์จากไฟล์ Access ที่นำเข้า — เก็บไว้ครบ ไม่มีอะไรหาย (ใหม่สุดอยู่บน)','every table and every column from the imported Access file — kept in full, nothing lost (newest on top)')}</p>
   ${d.tables.length?`<div class="card" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end">
    <label class="f">${L('ตาราง','Table')}<select class="in" id="arT" onchange="ARC.tbl=this.value;ARC.page=1;arLoad()">${d.tables.map(t=>`<option value="${esc(t.tbl)}" ${t.tbl===ARC.tbl?'selected':''}>${esc(t.tbl)} · ${n0(t.n)}</option>`).join('')}</select></label>
    <label class="f" style="flex:1;min-width:180px">${L('ค้นหา (ทุกคอลัมน์)','Search (any column)')}<input class="in" id="arQ" value="${esc(ARC.q)}" onkeydown="if(event.key==='Enter'){ARC.q=this.value.trim();ARC.page=1;arLoad()}"></label>
    <button class="btn pri" onclick="ARC.q=$('#arQ').value.trim();ARC.page=1;arLoad()">${L('ค้นหา','Search')}</button>
    <button class="btn" onclick="arCsv()">CSV ${L('ทั้งตาราง','whole table')}</button></div><div id="arB"></div>`
   :`<div class="msg m-warn">${L('ยังไม่มีข้อมูล — นำเข้าไฟล์ Access ก่อน (นำเข้า Access)','Nothing yet — import the Access file first (Import Access)')}</div>`}`;
  if(d.tables.length)arLoad();
}
async function arLoad(){
  const B=$('#arB');B.innerHTML=LOADH;
  try{ARCD=await api(`/api/fg/access-archive/rows?tbl=${encodeURIComponent(ARC.tbl)}&q=${encodeURIComponent(ARC.q)}&page=${ARC.page}&size=200`);}catch(e){B.innerHTML=err(e);return;}
  const d=ARCD,pages=Math.max(1,Math.ceil(d.total/d.size));
  B.innerHTML=`<div class="muted" style="margin:8px 2px">${n0(d.total)} ${L('แถว','rows')} · ${L('หน้า','page')} ${d.page}/${pages}</div>
   <div class="tw" style="max-height:calc(100vh - 330px);overflow:auto"><table><thead><tr>${d.cols.map(c=>`<th class="nw">${esc(c)}</th>`).join('')}</tr></thead><tbody>
   ${d.rows.map(r=>`<tr>${d.cols.map(c=>`<td class="nw">${esc(r[c]==null?'':r[c])}</td>`).join('')}</tr>`).join('')||`<tr><td class="muted" style="padding:20px">${L('ไม่มีรายการ','Nothing here')}</td></tr>`}</tbody></table></div>
   <div class="btns" style="margin-top:8px"><button class="btn sm" ${d.page<=1?'disabled':''} onclick="ARC.page--;arLoad()">‹ ${L('ใหม่กว่า','Newer')}</button>
    <button class="btn sm" ${d.page>=pages?'disabled':''} onclick="ARC.page++;arLoad()">${L('เก่ากว่า','Older')} ›</button></div>`;
}
async function arCsv(){
  let d;try{d=await api(`/api/fg/access-archive/rows?tbl=${encodeURIComponent(ARC.tbl)}&q=${encodeURIComponent(ARC.q)}&all=1`);}catch(e){alert(e.message);return;}
  csv(`${ARC.tbl}.csv`,d.cols,d.rows.map(r=>d.cols.map(c=>r[c])));
}
async function pgUsers(){""")

io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok")
