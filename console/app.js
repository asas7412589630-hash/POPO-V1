/* POPO-V2 Console — simplified single-page setup. */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const LS = 'popo_v2_console';

// Language control. Kept here rather than in i18n.js because it is a DOM
// concern: i18n.js must stay runnable outside a browser for the dictionary test.
document.addEventListener('DOMContentLoaded', function () {
  const picker = $('#langSelect');
  if (!picker) return;
  picker.value = currentLang;
  picker.onchange = () => setLang(picker.value);
});

let state = {
  base: '',
  upstreamHost: '',
  upstreamKey: '',
  upstreamPrefix: '',
  key: '',
  published: [],
  available: [],
  sourceModels: [],
};
let sourceRowsCache = [];

function loadState() {
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(LS) || '{}')); } catch {}
  // A stored selection is only meaningful against the catalogue it came from.
  // Clearing it here keeps the counter honest before any request is made.
  const known = new Set(state.available || []);
  state.published = (state.published || []).filter(m => known.has(m));
  for (const m of Object.keys(state.armor || {})) {
    if (!known.has(m)) delete state.armor[m];
  }
}
function saveState() { localStorage.setItem(LS, JSON.stringify(state)); }

function readBody(r) {
  const ct = r.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) return r.json().catch(() => ({}));
  return r.text().then(t => ({ raw: t }));
}

async function api(method, path, body) {
  const url = state.base + path;
  const headers = { 'Accept': 'application/json' };
  const adminKey = ($('#adminKey')?.value || '').trim();
  if (adminKey) headers['Authorization'] = 'Bearer ' + adminKey;
  else if (state.key) headers['Authorization'] = 'Bearer ' + state.key;
  if (body) headers['Content-Type'] = 'application/json';
  try {
    const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
    const data = await readBody(res);
    return { ok: res.ok, status: res.status, data };
  } catch (error) {
    return { ok: false, status: 0, data: { error: { message: error?.message || 'Network request failed' } } };
  }
}

function normalizeBase(v) {
  let raw = String(v || '').trim().replace(/\/+$/, '');
  if (!raw) return '';
  if (!raw.startsWith('http')) raw = 'http://' + raw;
  try {
    const u = new URL(raw);
    return u.protocol + '//' + u.host + u.pathname.replace(/\/+$/, '');
  } catch { return raw.replace(/\/+$/, ''); }
}

function showMsg(text, ok) {
  const el = $('#msg');
  el.textContent = text || '';
  el.style.display = 'block';
  el.className = ok ? 'ok' : 'err';
}
function clearMsg() {
  const el = $('#msg');
  el.style.display = 'none';
  el.textContent = '';
  el.className = '';
}

function showTab(name) {
  // Result and armor were separate V1-style tabs. Keep their deep links and
  // existing call sites working while presenting six concise V1-like sections.
  if (name === 'result' || name === 'armor') name = name === 'result' ? 'setup' : 'tuning';
  $$('.step').forEach(s => s.classList.toggle('on', s.id === 'tab-' + name));
  $$('.tabs button').forEach(b => {
    const active = b.dataset.tab === name;
    b.classList.toggle('on', active);
    b.setAttribute('aria-selected', String(active));
  });
  if (['sources', 'keys', 'records', 'tuning'].includes(name)) $('#operatorAccess')?.setAttribute('open', '');
  if (name === 'sources') loadSources();
  if (name === 'keys') { loadKeys(); loadAccounts(); }
  if (name === 'records') loadConversations();
}

function renderModels() {
  const list = $('#modelList');
  if (!state.available.length) {
    $('#modelGroup').style.display = 'none';
    // The counter would otherwise keep a stale number from a previous session.
    if (state.published.length) state.published = [];
    updateSelectedCount();
    return;
  }
  $('#modelGroup').style.display = 'block';
  list.replaceChildren();

  // Armour choices come from the server, not from a list written here: the registry
  // is the source of truth and an operator may edit it without touching this file.
  const armorNames = (state.armorFamilies && state.armorFamilies.length)
    ? state.armorFamilies
    : ['default'];
  const armorLabel = (name) => {
    const d = (state.armorDetails || {})[name];
    return (d && d.label) || name;
  };

  state.available.forEach((m, i) => {
    const row = document.createElement('div');
    row.className = 'model-item';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'm-' + i;
    cb.value = m;
    cb.checked = state.published.includes(m);

    const label = document.createElement('label');
    label.htmlFor = cb.id;
    label.textContent = m;
    label.style.flex = '1';
    label.style.minWidth = '0';
    label.style.overflow = 'hidden';
    label.style.textOverflow = 'ellipsis';

    // Per-model armour. "default" means "let the model decide from its family",
    // which is what almost every model should stay on.
    const select = document.createElement('select');
    select.className = 'armor-select';
    select.dataset.model = m;
    ['default'].concat(armorNames).forEach(name => {
      if (name === 'default' && armorNames.indexOf('default') !== -1) return;
      const opt = document.createElement('option');
      opt.value = name === 'default' ? '' : name;
      opt.textContent = name === 'default' ? '自動（依模型）' : armorLabel(name);
      select.appendChild(opt);
    });
    const current = (state.armor && state.armor[m]) || '';
    select.value = current;
    select.onchange = () => {
      state.armor = state.armor || {};
      if (select.value) state.armor[m] = select.value;
      else delete state.armor[m];
      saveState();
    };
    // Deliberately NOT disabled when unchecked: choosing armour and choosing to
    // publish are separate decisions, and locking the dropdown until the checkbox
    // is ticked makes the natural order ("pick the armour I want") impossible.
    // Dimmed instead, so it still reads as inactive-but-usable.
    select.style.opacity = cb.checked ? '1' : '.72';

    cb.onchange = () => {
      select.style.opacity = cb.checked ? '1' : '.72';
      state.published = Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(i => i.value);
      saveState();
      updateSelectedCount();
    };

    row.append(cb, label, select);
    list.appendChild(row);
  });
  updateSelectedCount();
}

function updateSelectedCount() {
  const el = $('#modelCount');
  if (!el) return;
  // Count what is actually ticked, not what the state object remembers. The state
  // can hold a selection from a previous catalogue, and a counter that disagrees
  // with the visible checkboxes is worse than no counter.
  const list = $('#modelList');
  const ticked = list ? list.querySelectorAll('input[type=checkbox]:checked').length : 0;
  el.textContent = ticked ? `${ticked} 個已選` : '';
  // Keep state in step with the DOM so a save writes what the operator sees.
  state.published = list
    ? Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(i => i.value)
    : [];
}

async function loadArmorFamilies() {
  const { ok, data } = await api('GET', '/armor/registry');
  if (!ok || !data) return;
  // `families` is the 2.5.0 field; fall back to `profiles` for an older server.
  state.armorFamilies = data.families || data.profiles || [];
  state.armorDetails = data.details || {};
  renderModels();
}

function fillForm() {
  $('#upstreamHost').value = state.upstreamHost || '';
  $('#upstreamKey').value = state.upstreamKey || '';
  $('#upstreamPrefix').value = state.upstreamPrefix || '';
  if (state.key) {
    $('#outUrl').textContent = state.base + '/v1';
    $('#outKey').textContent = state.key;
    $('#outModels').textContent = state.published.join(', ') || '-';
    $('#tab-result').classList.add('open');
  } else {
    $('#tab-result').classList.remove('open');
  }
  renderModels();
  const sel = $('#armorModel');
  sel.replaceChildren();
  (state.available.length ? state.available : ['glm-5.3']).forEach(m => {
    const option = document.createElement('option'); option.value = m; option.textContent = m; sel.appendChild(option);
  });
}

async function refreshStatus() {
  const result = await api('GET', '/setup/status');
  const { ok, data } = result;
  const pill = $('#connectionStatus');
  pill.classList.toggle('offline', !ok && result.status === 0);
  pill.querySelector('.status-text').textContent = result.status === 0 ? '無法連線' : (ok ? '已連線' : '服務可達');
  $('#aboutStatus').textContent = ok
    ? (data.configured ? '服務在線 · 已完成初始設定' : '服務在線 · 尚未完成初始設定')
    : (result.status === 0 ? '無法連線到本機 POPO 服務。' : `服務在線，狀態檢查回應 HTTP ${result.status}。`);
  if (ok && data.configured) {
    state.base = normalizeBase(location.origin);
    state.available = data.models || [];
    state.published = data.published || [];
    saveState();
    renderModels();
  }
}

async function loadModelList() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  if (!host) return showMsg('請先填寫 Base URL', false);
  const key = $('#upstreamKey').value.trim();
  $('#btnTest').disabled = true;
  showMsg(key ? '讀取模型清單（含金鑰驗證）...' : '讀取模型清單（不需金鑰）...', true);
  const { ok, data } = await api('POST', '/setup/list-models', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    setup_token: $('#setupToken').value.trim(),
  });
  $('#btnTest').disabled = false;
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('讀取失敗：' + detail, false);
  }
  state.upstreamHost = host;
  state.upstreamKey = key;
  state.upstreamPrefix = $('#upstreamPrefix').value.trim();
  state.available = data.models || [];
  // Keep a previous selection only if those models still exist in this catalogue.
  // Otherwise the counter says "2 selected" while no checkbox is ticked — the
  // selection lives in localStorage and outlives the list it referred to.
  const availableSet = new Set(state.available);
  state.published = (state.published || []).filter(m => availableSet.has(m));
  state.armor = state.armor || {};
  // Armour choices for models that are gone are dropped for the same reason.
  for (const m of Object.keys(state.armor)) {
    if (!availableSet.has(m)) delete state.armor[m];
  }
  saveState();
  renderModels();
  const auth = data.authenticated ? '（已帶金鑰）' : '（未帶金鑰）';
  showMsg(`讀到 ${state.available.length} 個模型${auth}。勾選要發布的，並選擇每個模型的破甲。`, true);
}

async function verifyKey() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  const key = $('#upstreamKey').value.trim();
  if (!host || !key) return showMsg('請填寫 Base URL 與 API Key', false);
  $('#btnVerify').disabled = true;
  showMsg('驗證金鑰（不消耗 token）...', true);
  const { ok, data } = await api('POST', '/setup/verify-key', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    setup_token: $('#setupToken').value.trim(),
  });
  $('#btnVerify').disabled = false;
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('驗證失敗：' + detail, false);
  }
  const verdict = data.verdict || 'unknown';
  const bad = verdict === 'rejected' || verdict === 'unreachable';
  showMsg(`金鑰：${data.message || verdict}`, !bad);
}

async function testConnection() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  const key = $('#upstreamKey').value.trim();
  if (!host || !key) return showMsg('請填寫 Base URL 與 API Key', false);
  const published = Array.from($$('#modelList input:checked')).map(i => i.value);
  if (!published.length) return showMsg('請先勾選至少一個模型', false);
  $('#btnTest').disabled = true;
  showMsg('測試對話（會消耗少量 token）...', true);
  const { ok, data } = await api('POST', '/setup/test', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    model: published[0],
    setup_token: $('#setupToken').value.trim(),
  });
  $('#btnTest').disabled = false;
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('對話測試失敗：' + detail, false);
  }
  showMsg(`對話可用，實際使用模型：${data.model}`, true);
}

async function doSetup() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  const key = $('#upstreamKey').value.trim();
  if (!host || !key) return showMsg('請填寫 Base URL 與 API Key', false);
  const published = Array.from($$('#modelList input:checked')).map(i => i.value);
  $('#btnSetup').disabled = true;
  showMsg('驗證並建立金鑰...', true);
  // Per-model armour, filtered to the models actually being published: sending
  // assignments for models that were not selected would be rejected as unknown.
  const armor = {};
  for (const m of published) {
    const chosen = (state.armor || {})[m];
    if (chosen) armor[m] = chosen;
  }
  const { ok, data } = await api('POST', '/setup/quick', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    models: published,
    profiles: armor,
    setup_token: $('#setupToken').value.trim(),
  });
  $('#btnSetup').disabled = false;
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('失敗：' + detail, false);
  }
  state.base = normalizeBase(location.origin);
  state.key = data.key;
  state.published = data.published || [];
  state.available = data.models || [];
  state.upstreamHost = host;
  state.upstreamKey = key;
  saveState();
  fillForm();
  $('#tab-result').classList.add('open');
  showTab('setup');
  $('#tab-result').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  showMsg('破甲 API 已建立', true);
}

async function publishModels() {
  const published = Array.from($$('#modelList input:checked')).map(i => i.value);
  state.published = published;
  saveState();
  const { ok, data } = await api('POST', '/setup/models', { models: published });
  if (!ok) return showMsg('發布模型失敗：' + (data?.error?.detail || ''), false);
  $('#outModels').textContent = state.published.join(', ') || '-';
}

async function previewArmor() {
  const model = $('#armorModel').value;
  const level = $('#armorLevel').value;
  // If no triad key yet, /armor is public (no auth required).
  const headers = state.key ? { 'Authorization': 'Bearer ' + state.key } : {};
  const res = await fetch(state.base + `/armor?model=${encodeURIComponent(model)}&level=${encodeURIComponent(level)}`, { headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return showMsg('預覽失敗：' + (data?.error?.detail || ''), false);
  $('#armorPreview').textContent = `profile: ${data.profile}\nblocks: ${data.blocks?.join(', ') || '-'}\npolicy_len: ${data.policy_len}\n\n${data.content}`;
}

function copyTo(id) {
  const el = $('#' + id);
  navigator.clipboard.writeText(el.textContent).then(() => {
    showMsg('已複製', true);
  }).catch(() => showMsg('複製失敗', false));
}

function bind() {
  loadState();
  state.base = normalizeBase(location.origin);
  fillForm();
  refreshStatus();

  $('#tabs').onclick = e => {
    const button = e.target.closest('button[data-tab]');
    if (button) showTab(button.dataset.tab);
  };
  // Three separate actions on purpose: listing the catalogue and judging the key
  // are cheap and independent, and only the chat probe spends tokens.
  const listBtn = $('#btnList');
  if (listBtn) listBtn.onclick = loadModelList;
  const verifyBtn = $('#btnVerify');
  if (verifyBtn) verifyBtn.onclick = verifyKey;
  if ($('#btnTest')) $('#btnTest').onclick = testConnection;
  $('#btnSetup').onclick = doSetup;
  $('#btnPreview').onclick = previewArmor;
  $('#btnAdminProfile').onclick = async () => {
    const model=$('#armorModel').value, level=$('#armorLevel').value;
    const r=await api('GET',`/admin/profile?model=${encodeURIComponent(model)}&level=${encodeURIComponent(level)}`);
    if(adminReady(r))pretty('#adminProfileData',r.data);
  };
  $$('.copy').forEach(b => b.onclick = () => copyTo(b.dataset.target));
  $('#btnRegen').onclick = doSetup;
}

bind();

function pretty(target, value) { $(target).textContent = JSON.stringify(value, null, 2); }
function adminReady(result) {
  if (!result.ok) {
    const message = result.data?.error?.detail || result.data?.error?.message || `HTTP ${result.status}`;
    showMsg(message, false); return false;
  }
  return true;
}
function selectedSource() { return $('#sourceSelect').value || $('#sourceId').value.trim(); }

async function loadSources() {
  const result = await api('GET', '/admin/sources');
  if (!adminReady(result)) return;
  sourceRowsCache = result.data.sources || [];
  pretty('#sourceList', sourceRowsCache.map(({id,name,base_url,configured,published_models}) => ({id,name,base_url,configured,published_models})));
  const select = $('#sourceSelect'); select.replaceChildren();
  sourceRowsCache.forEach(row => { const opt=document.createElement('option'); opt.value=row.id; opt.textContent=`${row.name} (${row.id})`; select.appendChild(opt); });
  if (sourceRowsCache.length && !$('#sourceId').value) fillSource(sourceRowsCache[0].id);
  await loadRoutes();
}
function fillSource(id) {
  const row = sourceRowsCache.find(r => r.id === id); if (!row) return;
  $('#sourceSelect').value = row.id; $('#sourceId').value = row.id;
  $('#sourceName').value = row.name; $('#sourceUrl').value = row.base_url;
  $('#sourceSecret').value = '';
}
async function sourceAction(action, extra={}) {
  const result = await api('POST', '/admin/sources', {action, source_id:selectedSource(), ...extra});
  if (!adminReady(result)) return null;
  showMsg(`${action} 完成`, true); return result.data;
}
function renderSourceModels(models) {
  state.sourceModels = models || [];
  const host = $('#sourceModelList'); host.replaceChildren();
  state.sourceModels.forEach((model,i) => {
    const row=document.createElement('div'); row.className='model-item';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.value=model; cb.id='sm-'+i;
    const label=document.createElement('label'); label.htmlFor=cb.id; label.textContent=model;
    row.append(cb,label); host.appendChild(row);
  });
}
async function loadRoutes() {
  const result = await api('GET', '/admin/model-routes');
  if (adminReady(result)) pretty('#routeList', result.data.routes || []);
}
function renderManagementRows(target, rows, kind) {
  const host=$(target); host.replaceChildren();
  if (!rows.length) { host.textContent='沒有資料。'; return; }
  rows.forEach(rec => {
    const row=document.createElement('div'); row.className='model-item';
    const label=document.createElement('span'); label.style.flex='1';
    label.textContent = kind==='key'
      ? `${rec.label || '未命名'} · ${rec.key_id} · ${rec.enabled?'啟用':'停用'}`
      : `${rec.username} · ${rec.role} · ${rec.disabled?'停用':'啟用'} · ${rec.uid}`;
    row.appendChild(label);
    if (kind==='key' && rec.enabled && rec.key_id!=='admin') {
      const disable=document.createElement('button'); disable.textContent='停用';
      disable.onclick=async()=>{const r=await api('POST','/admin/keys',{action:'disable',key_id:rec.key_id}); if(adminReady(r))loadKeys();}; row.appendChild(disable);
      const del=document.createElement('button'); del.textContent='刪除';
      del.onclick=async()=>{if(!confirm(`刪除金鑰 ${rec.key_id}？`))return; const r=await api('POST','/admin/keys',{action:'delete',key_id:rec.key_id}); if(adminReady(r))loadKeys();}; row.appendChild(del);
    }
    if (kind==='account' && !rec.disabled) {
      const reset=document.createElement('button'); reset.textContent='重設密碼';
      reset.onclick=async()=>{const password=prompt(`輸入 ${rec.username} 的新密碼（至少 12 字元）`);if(password===null)return;const r=await api('POST','/admin/accounts',{action:'password',uid:rec.uid,password});if(adminReady(r))showMsg('密碼已更新',true);}; row.appendChild(reset);
      const disable=document.createElement('button'); disable.textContent='停用';
      disable.onclick=async()=>{const r=await api('POST','/admin/accounts',{action:'disable',uid:rec.uid}); if(adminReady(r))loadAccounts();}; row.appendChild(disable);
    }
    host.appendChild(row);
  });
}
async function loadKeys() { const r=await api('GET','/admin/keys'); if(adminReady(r))renderManagementRows('#keyList',r.data.keys||[],'key'); }
async function loadAccounts() { const r=await api('GET','/admin/accounts'); if(adminReady(r))renderManagementRows('#accountList',r.data.accounts||[],'account'); }
async function loadConversations() {
  const r=await api('GET','/admin/conversations'); if(!adminReady(r))return;
  const rows=r.data.conversations||[], select=$('#conversationSelect'); select.replaceChildren();
  rows.forEach(item=>{const id=typeof item==='string'?item:(item.conversation_id||item.id); if(!id)return;const opt=document.createElement('option');opt.value=id;opt.textContent=id;select.appendChild(opt);});
  pretty('#conversationData',rows);
}
async function loadUsage() { const r=await api('GET',`/admin/usage?days=${encodeURIComponent($('#usageDays').value||7)}`); if(adminReady(r))pretty('#usageData',r.data); }
async function loadAudit() { const r=await api('GET',`/admin/audit?days=${encodeURIComponent($('#usageDays').value||7)}`); if(adminReady(r))pretty('#auditData',r.data); }

document.addEventListener('DOMContentLoaded', function () {
  $('#sourceSelect').onchange=()=>fillSource($('#sourceSelect').value);
  $('#btnSourceSave').onclick=async()=>{const r=await sourceAction('save',{name:$('#sourceName').value,base_url:$('#sourceUrl').value,api_key:$('#sourceSecret').value}); if(r){$('#sourceSecret').value='';await loadSources();fillSource(r.id);}};
  $('#btnSourceRefresh').onclick=loadSources;
  $('#btnSourceDiscover').onclick=async()=>{const r=await sourceAction('discover');if(r)renderSourceModels(r.models);};
  $('#btnSourceTest').onclick=async()=>{const model=$('#sourceModelList input:checked')?.value||'';const r=await sourceAction('test',{model});if(r)pretty('#routeList',r);};
  $('#btnSourcePublish').onclick=async()=>{const models=$$('#sourceModelList input:checked').map(cb=>cb.value);const profile=$('#sourceProfile').value;const profiles={};models.forEach(m=>{if(profile)profiles[m]=profile;});const r=await sourceAction('publish',{models,profiles});if(r){pretty('#routeList',r);await loadSources();}};
  $('#btnSourceDelete').onclick=async()=>{const id=selectedSource();if(!id||!confirm(`刪除來源 ${id} 及其發布路由？`))return;const r=await sourceAction('delete');if(r)loadSources();};
  $('#btnKeyCreate').onclick=async()=>{const r=await api('POST','/admin/keys',{action:'create',label:$('#newKeyLabel').value});if(adminReady(r)){pretty('#newKeyResult',r.data.key);$('#newKeyResult').style.display='block';loadKeys();}};
  $('#btnKeysRefresh').onclick=loadKeys;
  $('#btnAccountCreate').onclick=async()=>{const r=await api('POST','/admin/accounts',{action:'create',username:$('#newAccountName').value,password:$('#newAccountPassword').value,role:'owner'});if(adminReady(r)){showMsg('帳號已建立',true);$('#newAccountPassword').value='';loadAccounts();}};
  $('#btnAccountsRefresh').onclick=loadAccounts;
  $('#btnConversations').onclick=loadConversations;
  $('#btnConversationDetail').onclick=async()=>{const id=$('#conversationSelect').value;if(!id)return;const r=await api('GET','/admin/conversations/'+encodeURIComponent(id));if(adminReady(r))pretty('#conversationData',r.data);};
  $('#btnUsage').onclick=loadUsage; $('#btnAudit').onclick=loadAudit;
  $('#btnReplay').onclick=async()=>{const r=await api('POST','/admin/replay',{conversation_id:$('#replayConversation').value,prompt:$('#replayPrompt').value||undefined});if(adminReady(r))pretty('#replayData',r.data);};
  $('#btnCompare').onclick=async()=>{const models=$('#compareModels').value.split(',').map(s=>s.trim()).filter(Boolean);const r=await api('POST','/admin/compare',{models,prompt:$('#comparePrompt').value});if(adminReady(r))pretty('#replayData',r.data);};
  $('#btnRetention').onclick=async()=>{const days=Number($('#retentionDays').value);if(!Number.isInteger(days)||days<1)return showMsg('請輸入大於 0 的天數',false);if(!confirm(`確定刪除 ${days} 天以前的記錄？此操作無法復原。`))return;const r=await api('POST','/admin/retention',{days,confirm:true});if(adminReady(r))showMsg('清理完成：'+JSON.stringify(r.data),true);};
  api('GET','/armor/registry').then(r=>{if(!r.ok)return;const select=$('#sourceProfile');select.replaceChildren();const fallback=document.createElement('option');fallback.value='';fallback.textContent='依模型預設';select.appendChild(fallback);(r.data.profiles||[]).forEach(profile=>{const o=document.createElement('option');o.value=profile;o.textContent=profile;select.appendChild(o);});});
});


/* ---- tuning -------------------------------------------------------------
 * Values come from the server, including their bounds. The page never invents a
 * range: if the server adds a field it appears here, and if it removes one the
 * input disappears with it.
 */
async function loadTuning() {
  const { ok, data } = await api('GET', '/admin/tuning');
  if (!ok) { showMsg(t('unauthorized') + ': ' + (data.error && data.error.message || ''), false); return; }
  const host = $('#tuningFields');
  host.innerHTML = '';
  (data.fields || []).forEach(field => {
    const wrap = document.createElement('div');
    wrap.className = 'row';
    const label = document.createElement('label');
    label.style.flex = '2';
    label.textContent = field.name + (field.note ? ' — ' + field.note : '');
    const input = document.createElement('input');
    input.dataset.field = field.name;
    input.dataset.kind = field.type;
    input.value = data.current[field.name];
    wrap.appendChild(label);
    wrap.appendChild(input);
    host.appendChild(wrap);
  });
  showMsg(t('ready'), true);
}

async function saveTuning() {
  const payload = {};
  $$('#tuningFields input').forEach(input => {
    payload[input.dataset.field] = input.dataset.kind === 'bool'
      ? (input.value === 'true' || input.value === '1')
      : input.value;
  });
  const { ok, data } = await api('POST', '/admin/tuning', payload);
  showMsg(ok ? t('copied') : (data.error && data.error.message || t('error')), ok);
}

document.addEventListener('DOMContentLoaded', function () {
  const load = $('#btnTuningLoad');
  const save = $('#btnTuningSave');
  if (load) load.onclick = loadTuning;
  if (save) save.onclick = saveTuning;
  // The armour picker is built from the server's registry, so the console never
  // hard-codes a list that drifts from blocks/registry.json.
  if (typeof loadArmorFamilies === 'function') loadArmorFamilies();
});
