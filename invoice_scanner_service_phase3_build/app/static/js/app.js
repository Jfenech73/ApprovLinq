const state = { batches: [], selectedBatchId: null, progressTimer: null, companies: [] };
function $(id) { return document.getElementById(id); }
function truncate(v, n=80){ const s=String(v??""); return s.length>n?s.slice(0,n-1)+"…":s; }
function escapeHtml(value){ return String(value ?? "").replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m])); }
function formatDate(v){ return v ? new Date(v).toLocaleString() : "-"; }
function setMessage(el, text, kind=""){ el.textContent=text||""; el.className=`message ${kind}`.trim(); }
function confidenceDisplay(v){ return v==null?"-":`${Number(v).toFixed(0)}%`; }
function reviewBadge(row){ return row.review_required?"Review":"OK"; }
function hideProgress(){}
function stopProgressPolling(){ if(state.progressTimer){ clearInterval(state.progressTimer); state.progressTimer=null; } }
async function api(path, options={}){
  const response = await fetch(path, { ...options, headers: authHeaders(options.headers || {}) });
  if(!response.ok){ let message=`${response.status} ${response.statusText}`; try{ const d=await response.json(); if(d?.detail) message=typeof d.detail==='string'?d.detail:JSON.stringify(d.detail);}catch(_){} throw new Error(message); }
  const ct=response.headers.get('content-type')||'';
  if(ct.includes('application/json')) return response.json();
  return response;
}
async function loadCompanies(){
  state.companies = await api('/tenant/companies');
  const select=$('companySelector');
  select.innerHTML = state.companies.map(c=>`<option value="${c.id}">${escapeHtml(c.company_name)} (${escapeHtml(c.company_code)})</option>`).join('');
}
async function loadBatches(){
  const companyId = $('companySelector')?.value;
  const path = companyId ? `/batches?company_id=${encodeURIComponent(companyId)}` : '/batches';
  state.batches = await api(path);
  const tbody = $('batchesTableBody'); tbody.innerHTML='';
  if(!state.batches.length){ tbody.innerHTML = '<tr><td colspan="5" class="muted">No batches found.</td></tr>'; return; }
  for(const batch of state.batches){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td><strong>${escapeHtml(batch.batch_name)}</strong><br /><span class="muted">${escapeHtml(batch.id)}</span></td><td><span class="pill">${escapeHtml(batch.status||'-')}</span></td><td>${batch.page_count??'-'}</td><td>${formatDate(batch.created_at)}</td><td>${formatDate(batch.processed_at)}</td>`;
    tr.addEventListener('click', ()=>selectBatch(batch.id)); tbody.appendChild(tr);
  }
}
async function selectBatch(batchId, options={}){
  state.selectedBatchId=batchId; const batch = await api(`/batches/${batchId}`);
  $('selectedBatchEmpty').classList.add('hidden'); $('selectedBatchPanel').classList.remove('hidden');
  $('selectedBatchId').textContent=batch.id; $('selectedBatchName').textContent=batch.batch_name; $('selectedBatchStatus').textContent=batch.status; $('selectedBatchNotes').textContent=batch.notes||'-';
  renderFiles(batch.files||[]); await loadRows(); if(batch.status==='processing') startProgressPolling(); else if(!options.preservePolling){ stopProgressPolling(); hideProgress(); }
}
function renderFiles(files){ const tbody=$('filesTableBody'); tbody.innerHTML=''; if(!files.length){ tbody.innerHTML='<tr><td colspan="5" class="muted">No files uploaded yet.</td></tr>'; return; }
  for(const file of files){ const tr=document.createElement('tr'); const errorText=file.error_message?truncate(file.error_message,160):'-'; tr.innerHTML=`<td>${escapeHtml(file.original_filename)}</td><td><span class="pill">${escapeHtml(file.status)}</span></td><td>${file.page_count??'-'}</td><td title="${escapeHtml(file.error_message||'')}">${escapeHtml(errorText)}</td><td>${formatDate(file.uploaded_at)}</td>`; tbody.appendChild(tr); }
}
async function loadRows(){ const tbody=$('rowsTableBody'); tbody.innerHTML=''; if(!state.selectedBatchId){ tbody.innerHTML='<tr><td colspan="9" class="muted">Select a batch first.</td></tr>'; return; } const rows=await api(`/batches/${state.selectedBatchId}/rows`); if(!rows.length){ tbody.innerHTML='<tr><td colspan="9" class="muted">No extracted rows yet.</td></tr>'; return; }
  for(const row of rows){ const description=truncate(row.description||'-',80), supplier=truncate(row.supplier_name||'-',60), invoiceNo=truncate(row.invoice_number||'-',40); const tr=document.createElement('tr'); tr.innerHTML=`<td>${escapeHtml(row.source_filename||'-')}</td><td>${row.page_no??'-'}</td><td title="${escapeHtml(row.supplier_name||'')}">${escapeHtml(supplier)}</td><td title="${escapeHtml(row.invoice_number||'')}">${escapeHtml(invoiceNo)}</td><td>${escapeHtml(row.invoice_date||'-')}</td><td title="${escapeHtml(row.description||'')}">${escapeHtml(description)}</td><td>${row.total_amount??'-'}</td><td>${confidenceDisplay(row.confidence_score)}</td><td title="Posting: ${escapeHtml(row.supplier_posting_account||'-')} | Nominal: ${escapeHtml(row.nominal_account_code||'-')}">${reviewBadge(row)}</td>`; tbody.appendChild(tr); }
}
function startProgressPolling(){ stopProgressPolling(); state.progressTimer=setInterval(async()=>{ if(!state.selectedBatchId) return; const progress=await api(`/batches/${state.selectedBatchId}/progress`); $('selectedBatchStatus').textContent=progress.status; $('selectedBatchNotes').textContent=`${progress.notes||''} (${progress.percent}%)`; if(progress.status!=='processing'){ stopProgressPolling(); await selectBatch(state.selectedBatchId,{preservePolling:true}); await loadBatches(); } }, 3000); }
$('createBatchForm').addEventListener('submit', async (event)=>{ event.preventDefault(); const input=$('batchName'); const message=$('createBatchMessage'); setMessage(message,'Creating batch...'); try{ const company_id=$('companySelector').value; const batch=await api('/batches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({batch_name:input.value.trim(), company_id})}); input.value=''; setMessage(message,`Batch created: ${batch.id}`,'success'); await loadBatches(); await selectBatch(batch.id);}catch(error){setMessage(message,error.message);} });
$('uploadBtn').addEventListener('click', async ()=>{ const input=$('pdfFiles'); const message=$('actionMessage'); if(!state.selectedBatchId){ setMessage(message,'Select a batch first.'); return; } if(!input.files.length){ setMessage(message,'Choose at least one PDF file.'); return; } const form=new FormData(); for(const file of input.files) form.append('files', file); setMessage(message,'Uploading files...'); try{ await api(`/batches/${state.selectedBatchId}/files`, { method:'POST', body:form }); input.value=''; setMessage(message,'Files uploaded.','success'); await selectBatch(state.selectedBatchId); await loadBatches(); }catch(error){ setMessage(message,error.message);} });
$('processBtn').addEventListener('click', async ()=>{ const message=$('actionMessage'); if(!state.selectedBatchId){ setMessage(message,'Select a batch first.'); return; } setMessage(message,'Starting processing...'); try{ await api(`/batches/${state.selectedBatchId}/process`, { method:'POST' }); setMessage(message,'Batch processing started.','success'); await selectBatch(state.selectedBatchId); await loadBatches(); startProgressPolling(); }catch(error){ setMessage(message,error.message);} });
$('exportBtn').addEventListener('click', async ()=>{ const message=$('actionMessage'); if(!state.selectedBatchId){ setMessage(message,'Select a batch first.'); return; } setMessage(message,'Preparing export...'); try{ const response = await api(`/batches/${state.selectedBatchId}/export`); const blob = await response.blob(); const url = URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=`batch_${state.selectedBatchId}.xlsx`; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); setMessage(message,'Export downloaded.','success'); }catch(error){ setMessage(message,error.message);} });
$('refreshRowsBtn').addEventListener('click', loadRows); $('refreshBatchesBtn').addEventListener('click', loadBatches); $('companySelector').addEventListener('change', async ()=>{ state.selectedBatchId=null; $('selectedBatchPanel').classList.add('hidden'); $('selectedBatchEmpty').classList.remove('hidden'); await loadBatches(); });
ensureAuth(); loadCompanies().then(loadBatches).catch(err=>setMessage($('createBatchMessage'), err.message));
