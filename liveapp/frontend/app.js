const API = '/api';

let token = null;
let user = null;
let pollTimer = null;

const loginEl = document.getElementById('login');
const appEl = document.getElementById('app');
const loginForm = document.getElementById('loginForm');
const loginErr = document.getElementById('loginErr');
const logoutBtn = document.getElementById('logoutBtn');
const adminBtn = document.getElementById('adminBtn');
const adminDrawer = document.getElementById('adminDrawer');
const adminScrim = document.getElementById('adminScrim');
const closeDrawer = document.getElementById('closeDrawer');
const processBtn = document.getElementById('processBtn');
const refreshJobs = document.getElementById('refreshJobs');
const processLog = document.getElementById('processLog');
const jobsList = document.getElementById('jobsList');
const dashFrame = document.getElementById('dashFrame');

const statusHasDataset = document.getElementById('statusHasDataset');
const statusActiveVersion = document.getElementById('statusActiveVersion');
const statusProcessing = document.getElementById('statusProcessing');
const statusRunningJob = document.getElementById('statusRunningJob');
const statusUserCount = document.getElementById('statusUserCount');

const uploadForm = document.getElementById('uploadForm');
const filesHint = document.getElementById('filesHint');
const uploadFiles = document.getElementById('uploadFiles');
const uploadSubmit = document.getElementById('uploadSubmit');
const importDiskBtn = document.getElementById('importDiskBtn');
const uploadMsg = document.getElementById('uploadMsg');
const clearUploadMsg = document.getElementById('clearUploadMsg');

const processBtn = document.getElementById('processBtn');
const processHint = document.getElementById('processHint');

function setError(msg) {
  loginErr.textContent = msg || '';
}

function authHeaders(extra = {}) {
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: 'Bearer ' + token } : {}),
    ...extra,
  };
}

function authHeadersNoContentType(extra = {}) {
  // Used for FormData so the browser sets the multipart boundary itself.
  return {
    ...(token ? { Authorization: 'Bearer ' + token } : {}),
    ...extra,
  };
}

async function api(path, opts = {}) {
  const isFormData = opts.body instanceof FormData;
  const res = await fetch(API + path, {
    ...opts,
    headers: isFormData ? authHeadersNoContentType(opts.headers || {}) : authHeaders(opts.headers || {}),
  });
  if (res.status === 401) {
    logout();
    throw new Error('Not authenticated');
  }
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch (e) {
      detail = await res.text();
    }
    throw new Error(detail || 'Request failed');
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res.text();
}

function restoreSession() {
  const t = localStorage.getItem('dgp_token');
  const u = localStorage.getItem('dgp_user');
  if (t && u) {
    token = t;
    try { user = JSON.parse(u); } catch (e) { user = null; }
    showApp();
  } else {
    showLogin();
  }
}

function persistSession() {
  if (token) localStorage.setItem('dgp_token', token);
  else localStorage.removeItem('dgp_token');
  if (user) localStorage.setItem('dgp_user', JSON.stringify(user));
  else localStorage.removeItem('dgp_user');
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  setError('');
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  if (!username || !password) {
    setError('Enter username and password');
    return;
  }
  try {
    const data = await api('/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    token = data.token;
    user = data.user;
    persistSession();
    showApp();
  } catch (err) {
    setError(err.message || 'Login failed');
  }
});

logoutBtn.addEventListener('click', logout);
adminBtn.addEventListener('click', () => openDrawer());
closeDrawer.addEventListener('click', closeDrawerFn);
adminScrim.addEventListener('click', closeDrawerFn);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawerFn();
});

function openDrawer() {
  adminDrawer.classList.remove('hidden');
  adminScrim.classList.remove('hidden');
  refreshAdmin();
  loadActivity();
}

function closeDrawerFn() {
  adminDrawer.classList.add('hidden');
  adminScrim.classList.add('hidden');
}

function logout() {
  token = null;
  user = null;
  persistSession();
  clearInterval(pollTimer);
  pollTimer = null;
  processLog.textContent = '';
  jobsList.innerHTML = '';
  closeDrawerFn();
  showLogin();
}

processBtn.addEventListener('click', async () => {
  if (processBtn.disabled) return;
  processBtn.disabled = true;
  processBtn.textContent = 'Starting...';
  try {
    const res = await api('/admin/process', { method: 'POST' });
    appendLog('Started processing job #' + res.job_id);
    refreshAdmin();
    startPolling(res.job_id);
  } catch (err) {
    appendLog('Failed to start: ' + err.message);
  } finally {
    processBtn.disabled = false;
    processBtn.textContent = 'Run ETL scripts (optional)';
  }
});

clearUploadMsg.addEventListener('click', () => {
  uploadMsg.classList.add('hidden');
  uploadMsg.textContent = '';
  uploadFiles.value = '';
});

// Some deployments (Vercel) cannot accept a 45s bundle import inside a
// serverless request. There the data is published from the source machine with
// publish_site.py, so swap the upload form for an explanation.
function applyImportMode(enabled) {
  const form = document.getElementById('uploadForm');
  const hint = document.getElementById('importHint');
  if (!form || !hint) return;
  form.classList.toggle('hidden', !enabled);
  hint.classList.toggle('hidden', enabled);
}

// Same idea for the ETL button: hide it where the server cannot spawn the
// scripts and explain publish_site.py instead.
function applyProcessingMode(enabled) {
  if (!processBtn || !processHint) return;
  processBtn.classList.toggle('hidden', !enabled);
  processHint.classList.toggle('hidden', enabled);
}

// Import a zip of output/site.
uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!uploadFiles.files || uploadFiles.files.length === 0) {
    showUploadMsg('Select the zipped output/site folder first.');
    return;
  }
  const fd = new FormData();
  fd.append('file', uploadFiles.files[0]);
  await runImport(fd, 'Uploading zip...');
});

// Import the bundle already sitting in output/site on the server.
importDiskBtn.addEventListener('click', async () => {
  const fd = new FormData();
  fd.append('from_disk', 'true');
  await runImport(fd, 'Reading output/site from the server...');
});

async function runImport(fd, progressText) {
  uploadSubmit.disabled = true;
  importDiskBtn.disabled = true;
  showUploadMsg(progressText);
  try {
    const res = await api('/admin/import-site', { method: 'POST', body: fd });
    showUploadMsg(
      'Imported successfully.\n' +
      '  dataset version : v#' + res.version_id + '\n' +
      '  DSEs            : ' + res.n_dse + '\n' +
      '  customers       : ' + res.n_customers + '\n' +
      '  plan dates      : ' + (res.plan_dates || []).join(', ')
    );
    appendLog('Imported site bundle as version #' + res.version_id +
              ' (' + res.n_customers + ' customers)');
    uploadFiles.value = '';
    refreshAdmin();
    loadDashboardFrame();
  } catch (err) {
    showUploadMsg('Import failed: ' + (err.message || err));
  } finally {
    uploadSubmit.disabled = false;
    importDiskBtn.disabled = false;
  }
}

function showUploadMsg(text) {
  uploadMsg.classList.remove('hidden');
  uploadMsg.textContent = text;
}

refreshJobs.addEventListener('click', refreshJobsList);

// --- Viewer activity monitor ------------------------------------------------

const actFrom = document.getElementById('actFrom');
const actTo = document.getElementById('actTo');
const actSummary = document.getElementById('actSummary');
const actList = document.getElementById('actList');
const actRefresh = document.getElementById('actRefresh');
const actCsv = document.getElementById('actCsv');
const actXlsx = document.getElementById('actXlsx');

function todayISO() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function initActivityDates() {
  const t = todayISO();
  if (!actFrom.value) actFrom.value = t;
  if (!actTo.value) actTo.value = t;
}

actRefresh.addEventListener('click', loadActivity);

async function loadActivity() {
  initActivityDates();
  const qs = `from=${actFrom.value}&to=${actTo.value}`;
  actSummary.textContent = 'Loading...';
  try {
    const data = await api('/admin/activity?' + qs);
    renderActivity(data);
  } catch (err) {
    actSummary.textContent = 'Error: ' + err.message;
    actList.innerHTML = '';
  }
}

function renderActivity(data) {
  const s = data.summary || {};
  actSummary.textContent =
    `${s.days} days · ${s.users} users · ${s.opens} opens`;
  actList.innerHTML = '';

  const rows = data.rows || [];
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'row';
    empty.innerHTML = '<span class="muted">No activity in this range</span><span></span>';
    actList.appendChild(empty);
    return;
  }

  // header
  const head = document.createElement('div');
  head.className = 'row';
  head.style.cssText = 'font-weight:600;font-size:12px;color:var(--muted)';
  head.innerHTML =
    '<span>Date / Name</span><span>Role · Opens · Last open</span>';
  actList.appendChild(head);

  for (const r of rows) {
    const row = document.createElement('div');
    row.className = 'row';
    const right =
      `${(r.role || '').toUpperCase()} · ${r.opens} · ${r.last_open || ''}`;
    row.innerHTML =
      `<span><strong>${r.date}</strong> &nbsp; ${escapeHtml(r.name)} ` +
      `<span class="muted">(${escapeHtml(r.code)})</span></span>` +
      `<span class="muted">${escapeHtml(right)}</span>`;
    row.title = `First open: ${r.first_open || '-'}`;
    actList.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function downloadActivity(fmt) {
  initActivityDates();
  const qs = `from=${actFrom.value}&to=${actTo.value}&format=${fmt}`;
  // fetch with the auth header, then save via a blob URL
  fetch(API + '/admin/activity/export?' + qs, {
    headers: { Authorization: 'Bearer ' + token },
  })
    .then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.blob();
    })
    .then((blob) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `viewer_activity_${actFrom.value}_to_${actTo.value}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    })
    .catch((err) => {
      actSummary.textContent = 'Export failed: ' + err.message;
    });
}

actCsv.addEventListener('click', () => downloadActivity('csv'));
actXlsx.addEventListener('click', () => downloadActivity('xlsx'));

function showLogin() {
  loginEl.classList.remove('hidden');
  appEl.classList.add('hidden');
  document.getElementById('password').value = '';
  setError('');
}

function showApp() {
  loginEl.classList.add('hidden');
  appEl.classList.remove('hidden');
  adminBtn.classList.toggle('hidden', user.role !== 'admin');
  closeDrawerFn();
  loadDashboardFrame();
  if (user.role === 'admin') refreshAdmin();
}

function frameError(message) {
  dashFrame.srcdoc =
    '<html><body style="font-family:Inter;color:#fff;background:#0b1020;padding:20px">' +
    message +
    '</body></html>';
}

async function loadDashboardFrame() {
  try {
    await api('/core'); // fail fast with the real API error if not ready
    // The template lives in the project root; fetch it and swap its encrypted
    // bundle loader for API calls. All rendering code stays untouched.
    const tpl = await fetch('/_dashboard_template.html').catch(() => null);
    if (tpl && tpl.ok) {
      const patched = injectLiveLoader(await tpl.text());
      if (typeof patched !== 'string' || patched.startsWith('<html><body style="font-family:Inter;padding:20px;color:#ef4444')) {
        frameError(patched.replace(/<[^>]+>/g, ' ').trim());
        return;
      }
      dashFrame.srcdoc = patched;
      recordOpen();
      return;
    }
    dashFrame.srcdoc = baseFallback();
  } catch (err) {
    frameError('Error: ' + err.message);
  }
}

// Log this dashboard open for the admin Viewer Activity monitor. Fire and
// forget: a tracking failure must never block the dashboard.
function recordOpen() {
  fetch(API + '/activity/open', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token },
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Template -> live dashboard
//
// The original _dashboard_template.html reads an AES-GCM encrypted bundle from
// dse-data/. For the live app we keep every rendering function untouched and
// only swap the four data-loading functions for API calls.
//
// These MUST be replaced by brace matching, never by regex: the original
// bodies contain nested blocks, so a lazy regex ([\s\S]*?}\n) stops at the
// first inner brace and truncates the function mid-body, producing a
// SyntaxError that kills the entire <script>.
// ---------------------------------------------------------------------------

// Replace `function NAME(...) { ... }` (or `async function NAME`) using real
// brace counting. Returns null when the function is not found.
function replaceFunction(html, name, replacement) {
  const re = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`);
  const m = html.match(re);
  if (!m) return null;

  const start = m.index;
  const braceStart = html.indexOf('{', start);
  if (braceStart < 0) return null;

  let depth = 0;
  let i = braceStart;
  for (; i < html.length; i++) {
    const ch = html[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) { i++; break; }
    } else if (ch === '"' || ch === "'" || ch === '`') {
      // skip string/template literals so braces inside them don't count
      const quote = ch;
      i++;
      while (i < html.length && html[i] !== quote) {
        if (html[i] === '\\') i++;
        i++;
      }
    }
  }
  if (depth !== 0) return null; // unbalanced -> refuse to patch
  return html.slice(0, start) + replacement + html.slice(i);
}

// Runs inside the iframe: replaces decryptFile/loadDseChunk so the template
// pulls customers from the API. loadDseChunkLive MUST live here because the
// template's script block is a separate frame from app.js.
//
// It also kills the legacy password lock screen before it can paint. The
// template ships .lock as position:fixed; z-index:3000, so without this the
// old password page flashes on screen for as long as /api/core takes. The
// styles below are injected in <head> so they apply before any script runs.
function liveDataLoader() {
  return `<style id="dgpBootStyle">
  .lock, #lockScreen { display: none !important; }
  #dgpBoot {
    position: fixed; inset: 0; z-index: 99999;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 18px; padding: 24px;
    background: radial-gradient(1200px 800px at 50% 0, #172041, #0b1020);
    color: #e6e6e6; text-align: center;
    font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial;
  }
  #dgpBoot.gone { display: none; }
  #dgpBoot .dgp-title { font-size: 16px; font-weight: 700; letter-spacing: .01em; }
  #dgpBoot .dgp-sub { font-size: 13px; color: #9aa0b4; margin-top: -8px; }
  #dgpBoot .dgp-track {
    width: min(440px, 78vw); height: 6px; border-radius: 999px;
    background: #1f2a4a; overflow: hidden;
  }
  #dgpBoot .dgp-fill {
    height: 100%; width: 38%; border-radius: 999px;
    background: linear-gradient(90deg, #4f9bff, #2563eb);
    animation: dgpSlide 1.05s ease-in-out infinite;
  }
  @keyframes dgpSlide {
    0%   { transform: translateX(-105%); }
    100% { transform: translateX(290%); }
  }
  #dgpBoot .dgp-msg {
    font-size: 12.5px; color: #9aa0b4; min-height: 18px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  #dgpBoot .dgp-msg.err { color: #ef4444; font-weight: 600; max-width: 460px; }
  #dgpBoot .dgp-retry {
    margin-top: 4px; padding: 8px 16px; border-radius: 999px; cursor: pointer;
    border: 1px solid #1f2a4a; background: #131b33; color: #e6e6e6;
    font-size: 13px; font-weight: 600;
  }
  #dgpBoot .dgp-retry:hover { border-color: #3b82f6; }
</style>
<div id="dgpBoot">
  <div class="dgp-title">Easy SBF Deep-Dive Dashboard</div>
  <div class="dgp-sub">Loading your data</div>
  <div class="dgp-track"><div class="dgp-fill"></div></div>
  <div class="dgp-msg" id="dgpBootMsg">Connecting&hellip;</div>
</div>
<script>
(function(){
  var TOKEN = localStorage.getItem('dgp_token') || '';
  function api(path){
    return fetch('/api'+path, {
      headers: { 'Authorization': 'Bearer '+TOKEN },
      cache: 'no-store'
    }).then(function(r){
      if(!r.ok) throw new Error(path+' HTTP '+r.status);
      return r.json();
    });
  }
  window.__liveApi = api;
})();
</script>`;
}

function injectLiveLoader(html) {
  const patched = [];

  // 0. Hide the legacy password screen before it renders, and show the
  //    progress bar in its place.
  const boot = liveDataLoader();
  html = html.includes('</head>')
    ? html.replace('</head>', boot + '\n</head>')
    : boot + html;
  // 1. Core payload + per-DSE customer chunks come from the API.
  const p1 = replaceFunction(html, 'decryptFile',
    'async function decryptFile(rel){ throw new Error("encrypted bundle is not used in live mode"); }\n');
  if (p1 === null) return fail('could not patch decryptFile');
  html = p1; patched.push('decryptFile');

  const p2 = replaceFunction(html, 'loadDseChunk', [
    'function loadDseChunk(dse){',
    '  if (!custChunks[dse]){',
    '    custChunks[dse] = window.__liveApi("/dse/"+encodeURIComponent(dse)+"/customers")',
    '      .then(function(recs){',
    '        if(!DATA.customers) DATA.customers = {};',
    '        Object.assign(DATA.customers, recs);',
    '      })',
    '      .catch(function(err){ delete custChunks[dse]; throw err; });',
    '  }',
    '  return custChunks[dse];',
    '}'
  ].join('\n'));
  if (p2 === null) return fail('could not patch loadDseChunk');
  html = p2; patched.push('loadDseChunk');

  // 2. Drop the now-unused crypto helpers (safe to leave, but they reference
  //    a null MANIFEST, so remove them to avoid confusion).
  for (const fn of ['fetchManifest', 'deriveAesKey']) {
    const r = replaceFunction(html, fn,
      `async function ${fn}(){ throw new Error("not used in live mode"); }\n`);
    if (r !== null) { html = r; patched.push(fn); }
  }

  // 3. Neutralise the encrypted-bundle declarations. No rendering code reads
  //    these any more, so leaving them only invites confusion later.
  html = html.replace(
    /^\s*const DATA_BASE\s*=.*$/m,
    'const DATA_BASE = null; // live mode: data comes from /api, not dse-data/'
  );
  html = html.replace(/^\s*let MANIFEST\s*=.*$/m, 'let MANIFEST = null;');
  html = html.replace(/^\s*let AES_KEY\s*=.*$/m, 'let AES_KEY = null;');

  // 4. Password screen -> authenticated load.
  const unlockStart = html.search(/async function unlock\s*\(/);
  if (unlockStart >= 0) {
    const bindStart = html.indexOf('function bindLockScreen', unlockStart);
    const callSite = html.indexOf('bindLockScreen();', bindStart);
    if (bindStart >= 0 && callSite >= 0) {
      html = html.slice(0, unlockStart) + liveUnlockCode() +
             html.slice(callSite + 'bindLockScreen();'.length);
      patched.push('unlock');
    }
  }

  return html;
}

// Replaces unlock() + bindLockScreen() with an automatic, password-free load
// that uses the viewer's existing JWT and drives the injected progress bar.
function liveUnlockCode() {
  return `function dgpBootMsg(text, isError){
  var el = document.getElementById("dgpBootMsg");
  if (!el) return;
  el.textContent = text;
  el.className = isError ? "dgp-msg err" : "dgp-msg";
}
function dgpBootDone(){
  var b = document.getElementById("dgpBoot");
  if (b) b.classList.add("gone");
}
function dgpBootRetry(){
  var b = document.getElementById("dgpBoot");
  if (b) b.classList.remove("gone");
  unlock();
}
async function unlock(){
  dgpBootMsg("Loading dashboard data\\u2026");
  try{
    var core = await window.__liveApi("/core");
    dgpBootMsg("Building report\\u2026");
    DATA = core;
    DATA.customers = DATA.customers || {};
    var lock = ev("lockScreen");
    if(lock) lock.classList.add("off");
    var layout = ev("layout");
    if(layout) layout.classList.remove("locked");
    var input = ev("pwd");
    if(input) input.value = "";
    init();
    dgpBootDone();
  }catch(e){
    dgpBootMsg("Could not load the dashboard: " + e.message, true);
    var b = document.getElementById("dgpBoot");
    if (b && !b.querySelector(".dgp-retry")){
      var btn = document.createElement("button");
      btn.className = "dgp-retry";
      btn.textContent = "Retry";
      btn.addEventListener("click", dgpBootRetry);
      b.appendChild(btn);
    }
  }
}
function bindLockScreen(){
  unlock();
}
bindLockScreen();\n`;
}

function baseFallback() {
  return '<html><body style="font-family:Inter;padding:20px;color:#fff;background:#0b1020">Dashboard template not found in project root.</body></html>';
}

function fail(reason) {
  return '<html><body style="font-family:Inter;padding:20px;color:#ef4444;background:#0b1020">Dashboard live loader failed: '
    + reason + '</body></html>';
}

async function refreshAdmin() {
  if (user.role !== 'admin') return;
  try {
    const status = await api('/admin/status');
    statusHasDataset.textContent = status.has_dataset ? 'Yes' : 'No';
    if (status.active_version) {
      statusActiveVersion.textContent = 'v#' + status.active_version.id + ' @ ' + (status.active_version.created_at||'').replace('T',' ').slice(0,16);
    } else {
      statusActiveVersion.textContent = 'Not published';
    }
    statusProcessing.textContent = status.processing ? 'Running' : 'Idle';
    statusRunningJob.textContent = status.processing ? 'Job #' + status.running_job_id : '-';
    statusUserCount.textContent = status.user_count;
    applyImportMode(status.import_enabled !== false);
    applyProcessingMode(status.local_processing_enabled !== false);
    refreshJobsList();
    if (status.processing && !pollTimer) {
      startPolling(status.running_job_id);
    }
    if (!status.processing && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  } catch (err) {
    appendLog('Admin status error: ' + err.message);
  }
}

async function refreshJobsList() {
  if (user.role !== 'admin') return;
  try {
    const jobs = await api('/admin/jobs?limit=10');
    jobsList.innerHTML = '';
    if (!jobs.length) {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<span class="muted">No jobs yet</span><span></span>';
      jobsList.appendChild(row);
      return;
    }
    for (const j of jobs) {
      const row = document.createElement('div');
      row.className = 'row';
      let pill = '<span class="pill">' + j.status + '</span>';
      if (j.status === 'success') pill = '<span class="pill ok">SUCCESS</span>';
      if (j.status === 'failed') pill = '<span class="pill fail">FAILED</span>';
      if (j.status === 'running') pill = '<span class="pill run">RUNNING</span>';
      row.innerHTML = '<span>Job #' + j.id + ' — ' + (j.started_at||'').replace('T',' ').slice(0,16) + ' by ' + (j.triggered_by||'') + '</span><span style="display:flex;gap:8px;align-items:center">' + pill + (j.version_id? '<span class="muted">v#'+j.version_id+'</span>':'') + '</span>';
      const det = document.createElement('div');
      det.className = 'log hidden';
      det.style.margin='8px 12px 12px';
      det.textContent = j.log || j.error || '(no log)';
      row.addEventListener('click', () => det.classList.toggle('hidden'));
      jobsList.appendChild(row);
      jobsList.appendChild(det);
    }
  } catch (err) {
    appendLog('Jobs error: ' + err.message);
  }
}

function appendLog(line) {
  const ts = new Date().toISOString().replace('T',' ').slice(0,19);
  processLog.textContent = '[' + ts + '] ' + line + '\n' + processLog.textContent;
}

function startPolling(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await api('/admin/process/' + jobId);
      appendLog('Job #' + jobId + ' status: ' + job.status);
      if (job.status !== 'running') {
        clearInterval(pollTimer);
        pollTimer = null;
        refreshAdmin();
        loadDashboardFrame();
      }
    } catch (err) {
      appendLog('Poll error: ' + err.message);
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 4000);
}

restoreSession();
