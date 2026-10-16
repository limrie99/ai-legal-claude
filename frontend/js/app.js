import { contract, agents } from './data.js';

// ── State ──
let activeFilter = 'all';
let autoScroll = true;
let startTime = null;
let elapsedTimer = null;
let eventCounter = 0;
let uploadedContractText = null; // Stores the actual uploaded contract text for real AI analysis
let useRealAI = false; // True once an uploaded or pasted document has text to send
let collectedFindings = []; // All findings collected during analysis for PDF generation
let finalScoreData = null; // Score data for PDF

// ── DOM refs ──
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  buildAgentList();
  buildTimelineLanes();
  wireFilters();
  wireButtons();
  wireUpload();
  wireDisclaimer();
  const demoSection = document.querySelector('.demo-section');
  if (demoSection) demoSection.style.display = 'none';
});

// ── Build sidebar agent list ──
function buildAgentList() {
  const list = $('#agent-list');
  list.innerHTML = agents.map(a => `
    <div class="agent-item" data-agent="${a.id}" data-status="waiting">
      <span class="agent-dot" style="background:${a.color}"></span>
      <span class="agent-name">${a.name}</span>
      <span class="agent-status">idle</span>
    </div>
  `).join('');
}

// ── Build timeline lanes ──
function buildTimelineLanes() {
  const lanes = $('#timeline-lanes');
  lanes.innerHTML = agents.map(a => `
    <div class="timeline-lane" data-agent="${a.id}">
      <div class="lane-label">
        <span class="lane-color-dot" style="background:${a.color}"></span>
        <span class="lane-name">${a.name}</span>
      </div>
      <div class="lane-track">
        <div class="lane-progress" style="background:${a.color}"></div>
        <span class="lane-complete-check">\u2713</span>
      </div>
    </div>
  `).join('');
}

// ── Filters ──
function wireFilters() {
  $$('.filter-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      $$('.filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      activeFilter = pill.dataset.filter;
      applyFilter();
    });
  });
}

function applyFilter() {
  $$('.event-row, .event-detail').forEach(el => {
    if (activeFilter === 'all') {
      el.style.display = '';
    } else {
      const agent = el.dataset.agent || el.previousElementSibling?.dataset?.agent;
      el.style.display = (agent === activeFilter) ? '' : 'none';
    }
  });
}

// ── Buttons ──
function wireButtons() {
  $('#btn-auto-scroll').addEventListener('click', (e) => {
    autoScroll = !autoScroll;
    e.currentTarget.dataset.active = autoScroll;
  });

  $('#btn-expand-all').addEventListener('click', () => {
    const rows = $$('.event-row:not(.system-row)');
    const anyExpanded = [...rows].some(r => r.classList.contains('expanded'));
    rows.forEach(r => r.classList.toggle('expanded', !anyExpanded));
  });
}

// ── Disclaimer ──
function wireDisclaimer() {
  const closeBtn = $('#disclaimer-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      $('#disclaimer-banner').classList.add('hidden');
    });
  }
}

// ── Upload Handling ──
function wireUpload() {
  const zone = $('#upload-zone');
  const fileInput = $('#file-input');
  const browseBtn = $('#btn-browse');
  const pasteArea = $('#paste-area');
  const pasteBtn = $('#btn-paste-analyze');

  // Click zone to open file picker
  zone.addEventListener('click', (e) => {
    if (e.target === browseBtn || e.target.closest('.upload-browse-btn')) return;
    fileInput.click();
  });

  // Browse button
  browseBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    fileInput.click();
  });

  // File selected via picker
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) handleFile(fileInput.files[0]);
  });

  // Drag and drop
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });

  zone.addEventListener('dragleave', () => {
    zone.classList.remove('dragover');
  });

  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
  });

  // Also allow drop on the entire event stream area
  const stream = $('#event-stream');
  stream.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });

  stream.addEventListener('dragleave', (e) => {
    if (!stream.contains(e.relatedTarget)) zone.classList.remove('dragover');
  });

  stream.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
  });

  // Paste area
  pasteArea.addEventListener('input', () => {
    pasteBtn.disabled = pasteArea.value.trim().length === 0;
  });

  pasteBtn.addEventListener('click', () => {
    const text = pasteArea.value.trim();
    if (text.length > 0) handlePastedText(text);
  });

  // Allow Ctrl/Cmd+Enter in paste area
  pasteArea.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      const text = pasteArea.value.trim();
      if (text.length > 0) handlePastedText(text);
    }
  });
}

function handleFile(file) {
  const validTypes = [
    'application/pdf',
    'text/plain',
    'text/markdown',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  ];
  const validExts = ['.pdf', '.txt', '.md', '.doc', '.docx'];
  const ext = '.' + file.name.split('.').pop().toLowerCase();

  if (!validTypes.includes(file.type) && !validExts.includes(ext)) {
    alert('Unsupported file type. Please upload a .pdf, .txt, .md, .doc, or .docx file.');
    return;
  }

  // Show upload progress
  const progress = $('#upload-progress');
  progress.classList.remove('hidden');
  $('#upload-file-name').textContent = file.name;
  $('#upload-file-size').textContent = formatFileSize(file.size);
  $('#upload-file-status').textContent = 'Reading...';

  // Hide the upload zone and paste area
  const zone = $('#upload-zone');
  const pasteSection = zone.nextElementSibling;
  const demoSection = pasteSection?.nextElementSibling;
  zone.style.display = 'none';
  if (pasteSection) pasteSection.style.display = 'none';
  if (demoSection) demoSection.style.display = 'none';

  // Read the file
  if (ext === '.pdf') {
    // For PDFs, we read as text (basic extraction). Real PDF parsing would need a library.
    const reader = new FileReader();
    reader.onload = () => {
      // Try to extract text from PDF (basic approach)
      const text = extractTextFromPDFBinary(reader.result);
      uploadedContractText = text || null;
      useRealAI = !!uploadedContractText;
      $('#upload-file-status').textContent = uploadedContractText ? 'Parsed' : 'Parsed (demo)';
      $('#upload-file-status').style.color = 'var(--green)';
      $('#upload-file-status').style.animation = 'none';
      const meta = uploadedContractText
        ? `${uploadedContractText.split('\\n').length} lines \u00b7 ${uploadedContractText.split(/\\s+/).filter(Boolean).length.toLocaleString()} words`
        : `PDF document \u00b7 ${formatFileSize(file.size)}`;
      startAnalysis(file.name, meta);
    };
    reader.readAsArrayBuffer(file);
  } else {
    // Read text-based files
    const reader = new FileReader();
    reader.onload = () => {
      const text = reader.result;
      uploadedContractText = text;
      useRealAI = true;
      const lineCount = text.split('\n').length;
      const wordCount = text.split(/\s+/).filter(Boolean).length;
      $('#upload-file-status').textContent = 'Parsed';
      $('#upload-file-status').style.color = 'var(--green)';
      $('#upload-file-status').style.animation = 'none';
      startAnalysis(file.name, `${lineCount} lines \u00b7 ${wordCount.toLocaleString()} words`);
    };
    reader.readAsText(file);
  }
}

function handlePastedText(text) {
  uploadedContractText = text;
  useRealAI = true;
  const lineCount = text.split('\n').length;
  const wordCount = text.split(/\s+/).filter(Boolean).length;

  // Show upload progress with paste info
  const progress = $('#upload-progress');
  progress.classList.remove('hidden');
  $('#upload-file-name').textContent = 'Pasted Contract';
  $('#upload-file-size').textContent = `${lineCount} lines \u00b7 ${wordCount.toLocaleString()} words`;
  $('#upload-file-status').textContent = 'Ready';
  $('#upload-file-status').style.color = 'var(--green)';
  $('#upload-file-status').style.animation = 'none';

  // Hide upload zone and paste area
  const zone = $('#upload-zone');
  const pasteSection = zone.nextElementSibling;
  const demoSection = pasteSection?.nextElementSibling;
  zone.style.display = 'none';
  if (pasteSection) pasteSection.style.display = 'none';
  if (demoSection) demoSection.style.display = 'none';

  startAnalysis('Pasted Contract', `${lineCount} lines \u00b7 ${wordCount.toLocaleString()} words`);
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function startAnalysis(fileName, fileMeta) {
  // Brief delay then start
  setTimeout(() => {
    // Remove empty state entirely
    const empty = $('#event-empty');
    if (empty) empty.remove();

    // Set contract info
    const info = $('#contract-info');
    info.querySelector('.contract-name').textContent = fileName;
    info.querySelector('.contract-meta').innerHTML = `
      <span>${fileMeta}</span>
      <span style="margin-top:4px;color:var(--accent);">Uploaded document</span>
    `;

    setStatus('active', 'Analyzing...');
    startTime = performance.now();
    updateElapsed();

    addDisclaimerEvent();

    if (useRealAI && uploadedContractText) {
      addSystemEvent(`Contract uploaded: ${fileName} \u2014 connecting to AI...`);
      startRealAnalysis(uploadedContractText);
    } else {
      addSystemEvent('No contract text to analyze.');
      clearInterval(elapsedTimer);
      setStatus('', 'Stopped');
    }
  }, 600);
}

// ── Real AI Analysis via SSE ──
async function startRealAnalysis(contractText) {
  const agentEventCounts = {};

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: contractText }),
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({ error: 'Server error' }));
      addSystemEvent(`API Error: ${err.error || 'Unknown error'}`);
      clearInterval(elapsedTimer);
      setStatus('', 'Stopped');
      return;
    }

    addSystemEvent('Connected to AI \u2014 5 agents analyzing in parallel...');

    // Read SSE stream
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      let currentEventType = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEventType = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const dataStr = line.slice(6).trim();
          if (!dataStr || dataStr === '{}') {
            if (currentEventType === 'done') {
              onAllAgentsCompleteLive();
            }
            continue;
          }
          try {
            const data = JSON.parse(dataStr);
            handleSSEEvent(currentEventType, data, agentEventCounts);
          } catch (e) { /* skip unparseable */ }
        }
      }
    }
  } catch (err) {
    addSystemEvent(`Connection failed: ${err.message}`);
    clearInterval(elapsedTimer);
    setStatus('', 'Stopped');
  }
}

function handleSSEEvent(eventType, data, agentEventCounts) {
  if (eventType === 'system') {
    addSystemEvent(data.message);
    return;
  }

  if (eventType === 'agent_start') {
    agentEventCounts[data.agent] = 0;

    // Update sidebar
    const sidebarItem = $(`.agent-item[data-agent="${data.agent}"]`);
    if (sidebarItem) {
      sidebarItem.dataset.status = 'analyzing';
      sidebarItem.querySelector('.agent-dot').style.background = data.color;
      sidebarItem.querySelector('.agent-status').textContent = 'analyzing';
      sidebarItem.style.borderLeftColor = data.color;
    }

    // Animate timeline progress bar
    const lane = $(`.timeline-lane[data-agent="${data.agent}"]`);
    if (lane) {
      const progress = lane.querySelector('.lane-progress');
      progress.style.background = data.color;
      let val = 0;
      const interval = setInterval(() => { val += (85 - val) * 0.02; progress.style.width = val + '%'; }, 100);
      lane._progressInterval = interval;
    }

    addEvent({
      agent: data.agent, agentName: data.name, agentColor: data.color,
      type: 'system', badge: 'Agent',
      summary: `${data.name} started (${data.weight} weight)`,
      isStart: true,
    });
    return;
  }

  if (eventType === 'finding') {
    agentEventCounts[data.agent] = (agentEventCounts[data.agent] || 0) + 1;

    // Add timeline dot
    const lane = $(`.timeline-lane[data-agent="${data.agent}"]`);
    if (lane) {
      const track = lane.querySelector('.lane-track');
      const dot = document.createElement('div');
      dot.className = `lane-event-dot ${data.type}`;
      dot.style.left = (10 + Math.min(agentEventCounts[data.agent] * 8, 80)) + '%';
      dot.title = `${data.badge}: ${data.summary}`;
      track.appendChild(dot);
    }

    addEvent({
      agent: data.agent, agentName: data.agentName, agentColor: data.agentColor,
      type: data.type, badge: data.badge, summary: data.summary,
      detail: data.detail || null,
    });
    return;
  }

  if (eventType === 'agent_complete') {
    const sidebarItem = $(`.agent-item[data-agent="${data.agent}"]`);
    if (sidebarItem) {
      sidebarItem.dataset.status = 'complete';
      sidebarItem.querySelector('.agent-status').textContent = `${agentEventCounts[data.agent] || 0} findings`;
    }

    const lane = $(`.timeline-lane[data-agent="${data.agent}"]`);
    if (lane) {
      if (lane._progressInterval) clearInterval(lane._progressInterval);
      lane.querySelector('.lane-progress').style.width = '100%';
      lane.querySelector('.lane-progress').style.opacity = '0.08';
      lane.querySelector('.lane-complete-check').classList.add('visible');
    }

    updateAgentCount();
    return;
  }

  if (eventType === 'score') {
    contract.score = data.score;
    contract.grade = data.grade;
    contract.label = data.label;
    finalScoreData = data;
    addSystemEvent(`Contract Safety Score: ${data.score}/100 (${data.grade} \u2014 ${data.label})`);
    if (data.summary) addSystemEvent(data.summary);
    addDisclaimerEvent();
    showScore();
    showPdfButton();
    showViewerButton();
    clearInterval(elapsedTimer);
    setStatus('', 'Complete');
    return;
  }
}

function onAllAgentsCompleteLive() {
  setTimeout(() => {
    if ($('#score-display').classList.contains('hidden')) {
      clearInterval(elapsedTimer);
      setStatus('', 'Complete');
    }
  }, 3000);
}

// ── Basic PDF text extraction (best-effort) ──
function extractTextFromPDFBinary(arrayBuffer) {
  try {
    const bytes = new Uint8Array(arrayBuffer);
    const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    const parts = [];
    const streamRegex = /stream\s*\n([\s\S]*?)endstream/g;
    let match;
    while ((match = streamRegex.exec(text)) !== null) {
      const chunk = match[1].replace(/[^\x20-\x7E\n\r\t]/g, ' ').replace(/\s{3,}/g, ' ').trim();
      if (chunk.length > 20) parts.push(chunk);
    }
    if (parts.length > 0) return parts.join('\n\n');
    const readable = text.replace(/[^\x20-\x7E\n\r\t]/g, ' ').replace(/\s{3,}/g, ' ').trim();
    return readable.length > 100 ? readable : null;
  } catch { return null; }
}

// ── Start Demo ──
function startDemo() {
  // Clear empty state
  return;
}

// ── Event Stream ──
function addEvent({ agent, agentName, agentColor, type, badge, summary, detail, isStart }) {
  // Collect for PDF
  if (!isStart && type !== 'system') {
    collectedFindings.push({ agent, agentName, agentColor, type, badge, summary, detail: detail || undefined });
  }

  const stream = $('#event-stream');
  eventCounter++;
  $('#event-count').textContent = eventCounter;

  const elapsed = startTime ? ((performance.now() - startTime) / 1000).toFixed(1) + 's' : '';

  // Event row
  const row = document.createElement('div');
  row.className = 'event-row' + (isStart ? ' agent-start' : '');
  row.dataset.agent = agent;
  row.style.borderLeftColor = agentColor;

  const badgeClass = getBadgeClass(type);

  row.innerHTML = `
    <span class="row-agent" style="color:${agentColor}">${agentName}</span>
    <span class="row-icon">${getIcon(type)}</span>
    <span class="row-badge ${badgeClass}">${badge}</span>
    <span class="row-summary">${escapeHtml(summary)}</span>
    <span class="row-time">${elapsed}</span>
  `;

  // Click to expand
  if (detail) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => row.classList.toggle('expanded'));

    const detailEl = document.createElement('div');
    detailEl.className = 'event-detail';
    detailEl.dataset.agent = agent;
    detailEl.innerHTML = buildDetail(detail);

    stream.appendChild(row);
    stream.appendChild(detailEl);
  } else {
    stream.appendChild(row);
  }

  if (autoScroll) {
    stream.scrollTop = stream.scrollHeight;
  }
}

function addSystemEvent(text) {
  const stream = $('#event-stream');
  eventCounter++;
  $('#event-count').textContent = eventCounter;

  const row = document.createElement('div');
  row.className = 'event-row system-row';
  row.innerHTML = `
    <span class="row-icon">\u26A1</span>
    <span class="row-summary">${escapeHtml(text)}</span>
    <span class="row-time">${startTime ? ((performance.now() - startTime) / 1000).toFixed(1) + 's' : '0.0s'}</span>
  `;
  stream.appendChild(row);
}

function addDisclaimerEvent() {
  const stream = $('#event-stream');
  const row = document.createElement('div');
  row.className = 'event-row disclaimer-row';
  row.innerHTML = `
    <span class="row-icon">\u26A0</span>
    <span class="row-summary disclaimer-summary">
      <strong>Legal Disclaimer:</strong> This analysis is AI-generated and does not constitute legal advice.
      This tool is not a lawyer, law firm, or substitute for professional legal counsel.
      No attorney-client relationship is created by use of this tool.
      Always consult a licensed attorney before signing any contract or taking legal action.
      Results may be inaccurate, incomplete, or not applicable to your jurisdiction.
    </span>
  `;
  stream.appendChild(row);
  if (autoScroll) stream.scrollTop = stream.scrollHeight;
}

function buildDetail(d) {
  let html = `<h4>${escapeHtml(d.title)}</h4>`;
  html += `<span class="detail-tag ${d.risk.includes('CRITICAL') ? 'badge-critical' : 'badge-high'}">${d.risk}</span>`;
  html += `<p>${escapeHtml(d.text)}</p>`;
  if (d.fix) {
    html += `<h4>Recommended Fix</h4>`;
    html += `<div class="recommended-text">${escapeHtml(d.fix)}</div>`;
  }
  return html;
}

// ── Helpers ──
function getBadgeClass(type) {
  const map = { high: 'badge-high', medium: 'badge-medium', low: 'badge-low', info: 'badge-info', system: 'badge-system', critical: 'badge-critical' };
  return map[type] || 'badge-info';
}

function getIcon(type) {
  const map = { high: '\uD83D\uDD34', medium: '\uD83D\uDFE1', low: '\uD83D\uDFE2', info: '\uD83D\uDD35', system: '\uD83D\uDFE3', critical: '\uD83D\uDD3A' };
  return map[type] || '\u2022';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function setStatus(state, text) {
  const dot = $('#status-dot');
  const label = $('#status-text');
  dot.className = 'status-dot' + (state === 'active' ? ' active' : '');
  label.textContent = text;
}

function updateElapsed() {
  elapsedTimer = setInterval(() => {
    if (startTime) {
      const s = ((performance.now() - startTime) / 1000).toFixed(1);
      $('#timeline-elapsed').textContent = s + 's';
    }
  }, 100);
}

function updateAgentCount() {
  const done = $$('.agent-item[data-status="complete"]').length;
  $('#agent-count-badge').textContent = `${done} / 5`;
}

// ── PDF Download ──
let pdfWired = false;

function showPdfButton() {
  const btn = $('#btn-download-pdf');
  btn.classList.remove('hidden');
  if (!pdfWired) {
    pdfWired = true;
    btn.addEventListener('click', downloadPdf);
  }
}

async function downloadPdf() {
  const btn = $('#btn-download-pdf');
  if (btn.dataset.loading === 'true') return;

  const origHTML = btn.innerHTML;
  btn.dataset.loading = 'true';
  btn.classList.add('loading');
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Generating...';

  try {
    const payload = {
      findings: collectedFindings,
      score: finalScoreData || { score: contract.score, grade: contract.grade, label: contract.label },
      contract: {
        type: contract.type || 'Contract',
        parties: contract.parties || '',
        effectiveDate: contract.effectiveDate || '',
        term: contract.term || '',
        value: contract.value || '',
        governingLaw: contract.governingLaw || '',
      },
    };

    console.log('PDF payload:', JSON.stringify(payload).length, 'bytes,', collectedFindings.length, 'findings');

    const response = await fetch('/api/generate-pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      let errMsg = 'Unknown error';
      try { const e = await response.json(); errMsg = e.error || errMsg; } catch {}
      addSystemEvent(`PDF error: ${errMsg}`);
      return;
    }

    const blob = await response.blob();
    if (blob.size < 100) {
      addSystemEvent('PDF error: Generated file too small');
      return;
    }

    // Trigger download
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'Contract-Review-Report.pdf';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();

    // Cleanup after a delay
    setTimeout(() => {
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    }, 1000);

    addSystemEvent('PDF report downloaded successfully');
  } catch (err) {
    console.error('PDF download error:', err);
    addSystemEvent(`PDF download failed: ${err.message}`);
  } finally {
    btn.dataset.loading = 'false';
    btn.classList.remove('loading');
    btn.innerHTML = origHTML;
  }
}

// ── Contract Viewer with Highlights ──
let viewerWired = false;

function showViewerButton() {
  const btn = $('#btn-view-contract');
  btn.classList.remove('hidden');
  if (!viewerWired) {
    viewerWired = true;
    btn.addEventListener('click', toggleContractViewer);
    $('#btn-close-viewer').addEventListener('click', toggleContractViewer);
  }
}

function toggleContractViewer() {
  const viewer = $('#contract-viewer');
  const btn = $('#btn-view-contract');
  const isHidden = viewer.classList.contains('hidden');

  if (isHidden) {
    viewer.classList.remove('hidden');
    btn.classList.add('active');
    renderHighlightedContract();
  } else {
    viewer.classList.add('hidden');
    btn.classList.remove('active');
  }
}

function renderHighlightedContract() {
  const body = $('#viewer-body');

  const rawText = uploadedContractText;
  if (!rawText) {
    body.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:40px;">No contract text available to display.</div>';
    return;
  }

  // Build highlight map from findings
  const highlights = [];
  for (const f of collectedFindings) {
    if (!f.detail) continue;
    const title = f.detail.title || f.summary || '';
    const risk = f.type;
    // Extract section numbers like "1.3", "3.2", "5.1"
    const sectionMatch = title.match(/Section\s+(\d+\.?\d*)/i) || f.summary.match(/Section\s+(\d+\.?\d*)/i) || f.badge.match(/^(\d+\.?\d*)/);
    if (sectionMatch) {
      highlights.push({
        section: sectionMatch[1],
        risk: (risk === 'critical' || risk === 'high') ? 'high' : risk === 'medium' ? 'medium' : 'low',
        label: f.badge,
        tip: f.summary,
      });
    }
  }

  // Escape and highlight the contract text
  let html = escapeHtml(rawText);

  // Highlight section headers and their content
  for (const h of highlights) {
    const sectionNum = h.section.replace('.', '\\.');
    // Match section references like "1.3." or "1.3 " at start of line
    const regex = new RegExp(`(^|\\n)(${sectionNum}[\\.\\s][^\\n]*(?:\\n(?!\\d+\\.).*)*)`,'gm');
    html = html.replace(regex, (match, prefix, content) => {
      const tagClass = `hl-tag-${h.risk}`;
      const hlClass = `hl-${h.risk}`;
      return `${prefix}<span class="${hlClass}" title="${escapeHtml(h.tip)}"><span class="hl-tag ${tagClass}">${h.risk.toUpperCase()}</span>${content}</span>`;
    });
  }

  body.innerHTML = html;
}

// ── Score Gauge ──
function showScore() {
  const display = $('#score-display');
  display.classList.remove('hidden');

  const circumference = 2 * Math.PI * 52; // ~326.73
  const fill = $('#gauge-fill');
  const pct = contract.score / 100;

  // Color based on score
  let color = '--red';
  if (contract.score >= 70) color = '--green';
  else if (contract.score >= 40) color = '--yellow';
  fill.style.stroke = `var(${color})`;

  // Animate
  setTimeout(() => {
    fill.style.strokeDashoffset = circumference * (1 - pct);
  }, 100);

  // Animate number
  let current = 0;
  const target = contract.score;
  const step = () => {
    current += Math.max(1, Math.floor((target - current) * 0.15));
    if (current >= target) current = target;
    $('#gauge-value').textContent = current;
    if (current < target) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);

  $('#score-grade').textContent = contract.grade;
  $('#score-grade').style.color = `var(${color})`;
  $('#score-label').textContent = contract.label;
  $('#score-label').style.color = `var(${color})`;
}
