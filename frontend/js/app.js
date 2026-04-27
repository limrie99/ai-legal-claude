import { contract, agents } from './data.js';

// ── State ──
let activeFilter = 'all';
let autoScroll = true;
let startTime = null;
let elapsedTimer = null;
let eventCounter = 0;
let uploadedContractText = null; // Stores the actual uploaded contract text for real AI analysis
let useRealAI = false; // Whether to use real API or demo simulation
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
  $('#btn-demo').addEventListener('click', startDemo);
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
  ];
  const validExts = ['.pdf', '.txt', '.md'];
  const ext = '.' + file.name.split('.').pop().toLowerCase();

  if (!validTypes.includes(file.type) && !validExts.includes(ext)) {
    alert('Unsupported file type. Please upload a PDF, TXT, or Markdown file. For Word docs, export as PDF or paste the text.');
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
    parsePdfFile(file);
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

async function parsePdfFile(file) {
  try {
    if (!window.pdfjsLib) {
      throw new Error('PDF reader is still loading. Please wait a moment and try again.');
    }

    const arrayBuffer = await file.arrayBuffer();
    const text = await extractTextFromPDFBinary(arrayBuffer);
    const wordCount = text.split(/\s+/).filter(Boolean).length;

    if (wordCount < 25) {
      throw new Error('No readable text was found in this PDF. It may be scanned or image-only, and OCR did not recover enough text.');
    }

    uploadedContractText = text;
    useRealAI = true;
    const lineCount = text.split('\n').length;
    $('#upload-file-status').textContent = 'Parsed';
    $('#upload-file-status').style.color = 'var(--green)';
    $('#upload-file-status').style.animation = 'none';
    startAnalysis(file.name, `${lineCount} lines \u00b7 ${wordCount.toLocaleString()} words`);
  } catch (err) {
    uploadedContractText = null;
    useRealAI = false;
    $('#upload-file-status').textContent = 'Could not read PDF text';
    $('#upload-file-status').style.color = 'var(--red)';
    $('#upload-file-status').style.animation = 'none';
    alert(`${err.message}\n\nTry copying the contract text and pasting it into the text box instead.`);
    resetUploadForm();
  }
}

function resetUploadForm() {
  const zone = $('#upload-zone');
  const pasteSection = zone.nextElementSibling;
  const demoSection = pasteSection?.nextElementSibling;
  const progress = $('#upload-progress');
  zone.style.display = '';
  if (pasteSection) pasteSection.style.display = '';
  if (demoSection) demoSection.style.display = '';
  if (progress) progress.classList.add('hidden');
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

    // Try real AI API first, fall back to demo
    if (useRealAI && uploadedContractText) {
      addSystemEvent(`Contract uploaded: ${fileName} \u2014 connecting to AI...`);
      startRealAnalysis(uploadedContractText);
    } else {
      addSystemEvent('Running demo analysis (upload a .txt or .md file for real AI analysis)...');
      agents.forEach(agent => launchAgent(agent));
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
      addSystemEvent(`API Error: ${err.error || 'Unknown error'} \u2014 falling back to demo mode`);
      agents.forEach(agent => launchAgent(agent));
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
    addSystemEvent(`Connection failed: ${err.message} \u2014 falling back to demo mode`);
    agents.forEach(agent => launchAgent(agent));
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
async function extractTextFromPDFBinary(arrayBuffer) {
  if (window.pdfjsLib) {
    const pdf = await window.pdfjsLib.getDocument({ data: arrayBuffer }).promise;
    const pages = [];

    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      const lines = [];
      let lastY = null;

      for (const item of content.items) {
        const y = Math.round(item.transform?.[5] || 0);
        if (lastY !== null && Math.abs(y - lastY) > 4) lines.push('\n');
        lines.push(item.str);
        lastY = y;
      }

      pages.push(lines.join(' ').replace(/\s+\n\s+/g, '\n').replace(/[ \t]{2,}/g, ' ').trim());
    }

    const extracted = pages.join('\n\n').trim();
    if (extracted.split(/\s+/).filter(Boolean).length >= 25) return extracted;

    return await extractPdfTextWithOcr(pdf);
  }

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

async function extractPdfTextWithOcr(pdf) {
  if (!window.Tesseract) return '';

  const pageLimit = pdf.numPages;
  const ocrPages = [];
  $('#upload-file-status').textContent = 'OCR reading...';

  for (let pageNumber = 1; pageNumber <= pageLimit; pageNumber++) {
    $('#upload-file-status').textContent = `OCR page ${pageNumber}/${pageLimit}`;
    const page = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 2 });
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d', { willReadFrequently: true });
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);

    await page.render({ canvasContext: context, viewport }).promise;
    const result = await window.Tesseract.recognize(canvas, 'eng');
    const text = result?.data?.text?.trim();
    if (text) ocrPages.push(text);
    canvas.width = 0;
    canvas.height = 0;
  }
  return ocrPages.join('\n\n').trim();
}

// ── Start Demo ──
function startDemo() {
  // Clear empty state
  const empty = $('#event-empty');
  if (empty) empty.remove();

  // Set contract info
  const info = $('#contract-info');
  info.querySelector('.contract-name').textContent = contract.name;
  info.querySelector('.contract-meta').innerHTML = `
    <span>${contract.parties}</span>
    <span>${contract.type} \u00b7 ${contract.value}</span>
    <span>${contract.effectiveDate} \u00b7 ${contract.term}</span>
    <span>Governing Law: ${contract.governingLaw}</span>
  `;

  // Status
  setStatus('active', 'Analyzing...');
  startTime = performance.now();
  updateElapsed();

  // Disclaimer event first
  addDisclaimerEvent();

  // System event
  addSystemEvent('Contract loaded \u2014 launching 5 parallel agents...');

  // Launch all agents in parallel
  agents.forEach(agent => launchAgent(agent));
}

// ── Launch Agent ──
function launchAgent(agent) {
  const sidebarItem = $(`.agent-item[data-agent="${agent.id}"]`);
  const lane = $(`.timeline-lane[data-agent="${agent.id}"]`);
  const progress = lane.querySelector('.lane-progress');
  const check = lane.querySelector('.lane-complete-check');

  // Set analyzing state
  sidebarItem.dataset.status = 'analyzing';
  sidebarItem.querySelector('.agent-dot').style.background = agent.color;
  sidebarItem.querySelector('.agent-status').textContent = 'analyzing';
  sidebarItem.style.borderLeftColor = agent.color;

  // Add agent-start event
  addEvent({
    agent: agent.id,
    agentName: agent.name,
    agentColor: agent.color,
    type: 'system',
    badge: 'Agent',
    summary: `${agent.name} started (${agent.weight} weight)`,
    isStart: true,
  });

  // Animate progress bar
  let progressVal = 0;
  const progressInterval = setInterval(() => {
    progressVal += (90 - progressVal) * 0.04;
    progress.style.width = progressVal + '%';
  }, 50);

  // Schedule events
  agent.events.forEach(evt => {
    setTimeout(() => {
      // Add dot to timeline
      addTimelineDot(lane, evt, agent);
      // Add row to event stream
      addEvent({
        agent: agent.id,
        agentName: agent.name,
        agentColor: agent.color,
        type: evt.type,
        badge: evt.badge,
        summary: evt.summary,
        detail: evt.detail,
      });
    }, evt.time);
  });

  // Agent complete
  setTimeout(() => {
    clearInterval(progressInterval);
    progress.style.width = '100%';
    progress.style.opacity = '0.08';
    check.classList.add('visible');

    sidebarItem.dataset.status = 'complete';
    sidebarItem.querySelector('.agent-status').textContent = agent.summary;

    updateAgentCount();
    checkAllComplete();
  }, agent.duration);
}

// ── Timeline Dots ──
function addTimelineDot(lane, evt, agent) {
  const track = lane.querySelector('.lane-track');
  const dot = document.createElement('div');
  dot.className = `lane-event-dot ${evt.type}`;
  // Position: spread across 10%-90% of track width
  const pct = 10 + (evt.time / agent.duration) * 80;
  dot.style.left = pct + '%';
  dot.title = `${evt.badge}: ${evt.summary}`;
  track.appendChild(dot);
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

function checkAllComplete() {
  const done = $$('.agent-item[data-status="complete"]').length;
  if (done < 5) return;

  clearInterval(elapsedTimer);
  setStatus('', 'Complete');

  // Final summary
  setTimeout(() => {
    finalScoreData = { score: contract.score, grade: contract.grade, label: contract.label, summary: `Score: ${contract.score}/100 (${contract.grade})` };
    addSystemEvent(`All 5 agents complete \u2014 Contract Safety Score: ${contract.score}/100 (${contract.grade} \u2014 ${contract.label})`);
    addSystemEvent(`Estimated exposure: $420,000 - $1,235,000+ against $15,000 contract \u2014 DO NOT SIGN without major revisions`);
    addDisclaimerEvent();

    // Show score gauge, PDF button, and contract viewer button
    showScore();
    showPdfButton();
    showViewerButton();
  }, 500);
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

  // Use uploaded text or demo contract text
  const rawText = uploadedContractText || getDemoContractText();
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

function getDemoContractText() {
  return `INDEPENDENT CONTRACTOR SERVICES AGREEMENT
Nexus Digital Solutions LLC — Professional Services Contract

This Independent Contractor Services Agreement (the "Agreement") is entered into as of March 1, 2026 (the "Effective Date"), by and between:

Nexus Digital Solutions LLC, a Delaware limited liability company with its principal place of business at 1200 Innovation Drive, Suite 400, Wilmington, DE 19801 (hereinafter referred to as "Client" or "Company"), and

The undersigned independent contractor (hereinafter referred to as "Contractor").

1. SCOPE OF SERVICES

1.1. Contractor shall perform website redesign and development services for Client's corporate website (the "Project"), including but not limited to: user interface design, front-end development, back-end integration, content migration, quality assurance testing, and deployment to production environment.

1.2. The Project shall be completed in accordance with the specifications outlined in Exhibit A (attached hereto and incorporated by reference), as may be modified by Client from time to time in its sole discretion.

1.3. Contractor shall provide all necessary revisions, modifications, and adjustments to deliverables at no additional cost until Client confirms full satisfaction with the final output. There shall be no limitation on the number of revision cycles, and Contractor acknowledges that iterative refinement is an inherent component of the creative development process contemplated under this Agreement.

1.4. Contractor shall devote sufficient time and resources to ensure timely completion of the Project and shall be available for reasonable consultation during Client's standard business hours.

2. COMPENSATION AND PAYMENT

2.1. In consideration for the services rendered hereunder, Client shall pay Contractor a total project fee of Fifteen Thousand Dollars ($15,000.00), payable in milestone installments as follows:
Phase 1 — Project kickoff and wireframes: $3,000.00
Phase 2 — Design mockups and approval: $4,000.00
Phase 3 — Development and integration: $5,000.00
Phase 4 — Testing, launch, and handoff: $3,000.00

2.2. Payment for each milestone shall be due within ninety (90) days following Client's receipt of a proper invoice from Contractor. Client reserves the right to withhold payment for any deliverable that Client determines, in its reasonable judgment, to be unsatisfactory or not in conformance with the Project specifications.

2.3. Contractor shall be solely responsible for all taxes, insurance, and other obligations arising from compensation received under this Agreement.

2.4. The fees set forth in Section 2.1 constitute the entire compensation payable to Contractor for all services performed under this Agreement, including all revisions contemplated in Section 1.3.

3. INTELLECTUAL PROPERTY RIGHTS

3.1. All work product, deliverables, materials, designs, code, documentation, graphics, and any other output created by Contractor in connection with the Project (collectively, "Work Product"), shall be considered "work made for hire" as defined under the United States Copyright Act (17 U.S.C. § 101). To the extent that any Work Product does not qualify as work made for hire, Contractor hereby irrevocably assigns to Client all right, title, and interest in and to such Work Product.

3.2. The assignment in Section 3.1 includes, without limitation, any pre-existing intellectual property, tools, frameworks, libraries, code snippets, templates, or methodologies that Contractor incorporates into or utilizes in the creation of the Work Product (collectively, "Contractor Materials"). Contractor represents and warrants that Contractor has full authority to assign such Contractor Materials.

3.3. Contractor hereby irrevocably waives, to the fullest extent permitted by applicable law, all moral rights in and to the Work Product worldwide and in perpetuity.

4. CONFIDENTIALITY AND NON-DISCLOSURE

4.1. Contractor acknowledges that during the course of this engagement, Contractor may have access to confidential and proprietary information of Client (collectively, "Confidential Information").

4.2. Contractor shall not, during the term of this Agreement or at any time thereafter, disclose, publish, or otherwise disseminate any Confidential Information to any third party without the prior written consent of Client.

4.3. Contractor shall not, at any time during or after the term of this Agreement, disclose the existence of this Agreement or the business relationship between the parties to any third party. Contractor further agrees that Contractor shall not use any Work Product in Contractor's portfolio, website, social media, marketing materials, or any other public or private communication without the express prior written consent of Client, which may be withheld in Client's sole and absolute discretion.

5. NON-COMPETITION AND NON-SOLICITATION

5.1. During the term of this Agreement and for a period of twenty-four (24) months following its termination or expiration (the "Restricted Period"), Contractor shall not, directly or indirectly, engage in, own, manage, operate, control, be employed by, participate in, consult for, or be connected with any business or activity that is competitive with Client's business within the technology industry.

5.2. During the Restricted Period, Contractor shall not, directly or indirectly, solicit, contact, or attempt to solicit any client, customer, vendor, or business partner of Client for the purpose of providing services similar to those provided under this Agreement.

6. TERM, TERMINATION, AND RENEWAL

6.1. This Agreement shall commence on the Effective Date and shall continue for an initial term of twelve (12) months (the "Initial Term").

6.2. Upon expiration of the Initial Term, this Agreement shall automatically renew for successive periods of twelve (12) months each, unless either party provides written notice of non-renewal at least one hundred twenty (120) days prior to the expiration of the then-current term.

6.3. Client may terminate this Agreement at any time, with or without cause, effective immediately upon written notice to Contractor. In the event of termination by Client under this Section, Client shall have no obligation to compensate Contractor for any work in progress or incomplete deliverables.

6.4. Contractor may terminate this Agreement only upon ninety (90) days' prior written notice to Client. During such notice period, Contractor shall continue to perform all obligations under this Agreement.

6.5. In the event of cancellation or termination of the Project for any reason, Contractor acknowledges and agrees that Contractor shall not be entitled to any compensation for work performed prior to the effective date of termination, except for milestone payments that have been previously approved and invoiced in accordance with Section 2.

7. INDEMNIFICATION

7.1. Contractor shall indemnify, defend, and hold harmless Client and its officers, directors, employees, agents, successors, and assigns from and against any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising out of or relating to: (a) Contractor's performance or failure to perform under this Agreement; (b) any breach of any representation, warranty, or covenant made by Contractor; (c) any negligent or wrongful act or omission of Contractor; or (d) any claim by a third party related to the Work Product, regardless of whether such claim arises in whole or in part from the negligence or acts of Client.

7.2. Contractor's indemnification obligations under this Section shall survive the termination or expiration of this Agreement indefinitely.

8. LIMITATION OF LIABILITY

8.1. IN NO EVENT SHALL CLIENT BE LIABLE TO CONTRACTOR FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES.

8.2. CLIENT'S TOTAL AGGREGATE LIABILITY UNDER THIS AGREEMENT SHALL NOT EXCEED THE AMOUNT OF FEES ACTUALLY PAID BY CLIENT TO CONTRACTOR DURING THE THIRTY (30) DAY PERIOD IMMEDIATELY PRECEDING THE EVENT GIVING RISE TO THE CLAIM.

8.3. The limitations set forth in Sections 8.1 and 8.2 shall apply to Client only. Contractor acknowledges that Contractor's liability under this Agreement shall not be subject to any cap or limitation.

9. REPRESENTATIONS AND WARRANTIES

9.1. Contractor represents and warrants that: (a) Contractor has the right, power, and authority to enter into this Agreement; (b) the Work Product will be original and will not infringe upon any intellectual property rights of any third party; (c) Contractor will perform the services in a professional and workmanlike manner; and (d) Contractor is an independent contractor and not an employee of Client.

9.2. CLIENT MAKES NO WARRANTIES, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY IMPLIED WARRANTIES OF MERCHANTABILITY OR FITNESS FOR A PARTICULAR PURPOSE.

10. GOVERNING LAW AND DISPUTE RESOLUTION

10.1. This Agreement shall be governed by and construed in accordance with the laws of the British Virgin Islands, without regard to its conflict of laws principles.

10.2. Any dispute arising out of or relating to this Agreement shall be resolved exclusively in the courts of the British Virgin Islands.

10.3. In the event of any legal action arising under this Agreement, the prevailing party shall be entitled to recover its reasonable attorneys' fees and costs from the non-prevailing party.

11. GENERAL PROVISIONS

11.1. Entire Agreement. This Agreement constitutes the entire agreement between the parties.

11.2. Amendments. This Agreement may not be amended except by a written instrument signed by both parties; provided, however, that Client may modify the Project specifications set forth in Exhibit A at any time upon written notice to Contractor.

11.3. Severability. If any provision is held to be invalid, the remaining provisions shall continue in full force.

11.4. Waiver. Failure to enforce any provision shall not constitute a waiver.

11.5. Assignment. Contractor may not assign this Agreement without Client's consent. Client may freely assign this Agreement.

11.6. Notices. All notices shall be in writing.

11.7. Survival. Sections 3, 4, 5, 7, 8, and 10 shall survive the termination or expiration of this Agreement.`;
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
