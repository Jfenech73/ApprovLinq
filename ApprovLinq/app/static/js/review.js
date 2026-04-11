// Review workspace JS — talks to /review/* endpoints
const FIELDS = ["supplier_name","supplier_posting_account","nominal_account_code",
  "invoice_number","invoice_date","description","net_amount","vat_amount","total_amount","currency","tax_code"];
const params = new URLSearchParams(location.search);
const batchId = params.get("batch_id");
let state = { batch:null, rows:[], filter:"all", selected:null, page:1, fileId:null };
const tok = () => localStorage.getItem("token") || "";
const hdrs = () => ({"Content-Type":"application/json","Authorization":"Bearer "+tok()});

async function load(){
  const r = await fetch(`/review/batches/${batchId}`,{headers:hdrs()});
  if(!r.ok){alert("Load failed");return;}
  const d = await r.json(); state.batch=d.batch; state.rows=d.rows;
  if(state.rows.length){state.selected=state.rows[0].id; state.fileId=state.rows[0].source_file_id;}
  render();
}

function rowMatches(r){
  if(state.filter==="needs_review") return r.review_required;
  if(state.filter==="corrected")    return r.is_corrected;
  if(state.filter==="low_conf")     return r.confidence_score!=null && r.confidence_score<0.7;
  return true;
}

function render(){
  const b = state.batch;
  document.getElementById("batch-header").innerHTML =
    `<h2>${b.name}</h2><span class="status-pill ${b.status}">${b.status}</span>
     <p>${b.row_count} rows · ${b.corrected_count} corrected · ${b.flagged_count} flagged · v${b.current_export_version}</p>`;
  const list = document.getElementById("row-list"); list.innerHTML="";
  state.rows.filter(rowMatches).forEach(r=>{
    const d=document.createElement("div");
    d.className="row-card"+(r.review_required?" flagged":"")+(r.is_corrected?" corrected":"")+(r.id===state.selected?" selected":"");
    d.innerHTML=`<b>${r.source_filename||"file"}</b> · p${r.page_no} · ${r.current.supplier_name||"<no supplier>"} · ${r.current.total_amount||""}`;
    d.onclick=()=>{state.selected=r.id; state.fileId=r.source_file_id; state.page=r.page_no; render(); loadAudit(r.id); refreshPreview();};
    list.appendChild(d);
  });
  renderEditor();
}

function renderEditor(){
  const r = state.rows.find(x=>x.id===state.selected);
  const ed = document.getElementById("row-editor");
  if(!r){ed.innerHTML="";return;}
  let html = `<h3>Edit row ${r.id}</h3><div id="fields">`;
  FIELDS.forEach(f=>{
    const cur = r.current[f]??""; const orig = r.original[f]??"";
    const flagged = (r.review_fields||[]).includes(f);
    html += `<div class="field-row">
      <label>${f}${flagged?" ⚠":""}</label>
      <input data-field="${f}" value="${cur===null?"":String(cur).replace(/"/g,'&quot;')}">
      <label class="rule-cb"><input type="checkbox" data-rule="${f}"> rule</label>
      <button data-revert="${f}">↶</button></div>
      <div style="font-size:11px;color:#666;margin-left:140px">orig: ${orig===null?"":orig}</div>`;
  });
  html += `</div>
    <label><input type="checkbox" id="force-add"> Force add new supplier/nominal (requires note)</label>
    <textarea class="note-area" id="note" placeholder="Reason / note (required for force-add)"></textarea>
    <button id="btn-save">Save corrections</button>`;
  ed.innerHTML = html;
  ed.querySelector("#btn-save").onclick = saveRow;
  ed.querySelectorAll("[data-revert]").forEach(b=>b.onclick=()=>revertField(b.dataset.revert));
}

async function saveRow(){
  const r = state.rows.find(x=>x.id===state.selected);
  const changes = {}; const ruleFields = [];
  document.querySelectorAll("[data-field]").forEach(i=>{
    const f=i.dataset.field; const v=i.value===""?null:i.value;
    if(String(v??"")!==String(r.current[f]??"")) changes[f]=v;
  });
  document.querySelectorAll("[data-rule]:checked").forEach(c=>ruleFields.push(c.dataset.rule));
  const body={changes,note:document.getElementById("note").value||null,
    force_add:document.getElementById("force-add").checked,save_as_rule_fields:ruleFields};
  const res = await fetch(`/review/batches/${batchId}/rows/${r.id}`,{method:"PATCH",headers:hdrs(),body:JSON.stringify(body)});
  if(!res.ok){alert("Save failed: "+(await res.text()));return;}
  await load();
}

async function revertField(f){
  const r = state.rows.find(x=>x.id===state.selected);
  await fetch(`/review/batches/${batchId}/rows/${r.id}/revert/${f}`,{method:"POST",headers:hdrs()});
  await load();
}

async function loadAudit(rowId){
  const r = await fetch(`/review/batches/${batchId}/rows/${rowId}/audit`,{headers:hdrs()});
  const list = await r.json();
  document.getElementById("audit-list").innerHTML = list.map(a=>
    `<div style="border-bottom:1px solid #eee;padding:4px;font-size:12px">
      <b>${a.field}</b> ${a.action}: ${a.old||"∅"} → ${a.new||"∅"} <i>(${a.username||"?"})</i>
      ${a.rule_created?'<span style="color:#070">+rule</span>':''}${a.force_added?'<span style="color:#c00">+force</span>':''}
    </div>`).join("");
}

function refreshPreview(){
  if(!state.fileId) return;
  document.getElementById("preview-img").src = `/review/files/${state.fileId}/preview?page=${state.page}&t=${Date.now()}`;
  document.getElementById("page-label").textContent = "page "+state.page;
}
document.getElementById("prev-page").onclick=()=>{if(state.page>1){state.page--;refreshPreview();}};
document.getElementById("next-page").onclick=()=>{state.page++;refreshPreview();};

document.querySelectorAll("[data-filter]").forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;render();});

document.getElementById("btn-approve").onclick=async()=>{
  const r = await fetch(`/review/batches/${batchId}/transition`,{method:"POST",headers:hdrs(),body:JSON.stringify({target:"approved"})});
  if(!r.ok)alert(await r.text()); else load();
};
document.getElementById("btn-export").onclick=async()=>{
  // Existing export endpoint in batches.py should be wired to call corrected_exporter — see rollout note.
  window.location.href=`/batches/${batchId}/export?corrected=1`;
};
document.getElementById("btn-reopen").onclick=async()=>{
  const r = await fetch(`/review/batches/${batchId}/reopen`,{method:"POST",headers:hdrs()});
  if(!r.ok)alert(await r.text()); else load();
};

// Remap mode: drag to select region on preview
let dragStart=null;
document.getElementById("preview-img").addEventListener("mousedown",e=>{
  if(!document.getElementById("remap-mode").checked) return;
  const r=e.target.getBoundingClientRect(); dragStart={x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height,w:r.width,h:r.height};
});
document.getElementById("preview-img").addEventListener("mouseup",async e=>{
  if(!dragStart) return;
  const r=e.target.getBoundingClientRect();
  const x2=(e.clientX-r.left)/r.width, y2=(e.clientY-r.top)/r.height;
  const field=prompt("Which field is this region for?"); if(!field){dragStart=null;return;}
  const row=state.rows.find(x=>x.id===state.selected);
  await fetch(`/review/batches/${batchId}/rows/${row.id}/remap`,{method:"POST",headers:hdrs(),
    body:JSON.stringify({field_name:field,page_no:state.page,
      x:Math.min(dragStart.x,x2),y:Math.min(dragStart.y,y2),
      w:Math.abs(x2-dragStart.x),h:Math.abs(y2-dragStart.y),file_id:state.fileId})});
  dragStart=null; alert("Remap saved");
});

load();
