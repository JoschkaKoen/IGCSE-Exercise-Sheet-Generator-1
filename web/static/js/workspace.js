/**
 * workspace.js — entry point; orchestrates form submission, job polling,
 * preview mode transitions, overview panel, and keyboard/resize handlers.
 * Imports: state.js, pdf-render.js, pdf-tabs.js, downloads.js.
 *
 * Dependency graph:
 *   state.js  ──► pdf-render.js ──► pdf-tabs.js ──► workspace.js
 *             ──►                   downloads.js ──► workspace.js
 */

import {
  state, TABS, TAB_ORDER, PDF_OUTPUT_PAGE_H_PT,
  tabEnabled, activePdfTabId, firstEnabledTab, resolveTabId, tabIdToSideLayout,
  getPdfState, scrollEl, pagesStackEl, sleep,
} from './state.js';

import {
  renderPdfContinuous, rerenderPdfZoomBuffered, rerenderActivePdfTab,
  loadPdf, destroyPdfTab,
  showSpinner, hideSpinner, showEmpty, hideEmpty,
  zoomActivePdf, resetActivePdfZoom,
  updateHeaderGlassFromPdfScroll,
  ensurePdfScrollGlassListeners,
  hideAllPdfSpinners,
} from './pdf-render.js';

import { selectTab, syncSidePill, syncPdfTabChrome } from './pdf-tabs.js';
import { applyDoneData, triggerDownloadAllPdfs, applyRankingUrl, updateRankingLog, setRankingIdle } from './downloads.js';

// ─── Module-level constants ───────────────────────────────────────────────────

const CONFIG = {
  RERENDER_MAX_ATTEMPTS: 20,   // max retries waiting for the scroll element to have height
  RERENDER_RETRY_MS:     50,   // ms between rerenderWhenReady retries
  POLL_INTERVAL_MS:      200,  // job status polling interval
  RESIZE_DEBOUNCE_MS:    100,  // ResizeObserver debounce delay
  ZOOM_SETTLE_MS:        50,   // delay before re-render after pinch-zoom settles
};

// ─── DOM refs ─────────────────────────────────────────────────────────────────

const form         = document.getElementById('gen-form');
const workspace    = document.getElementById('generate-workspace');
const pdfPane      = document.getElementById('pdf-preview-pane');
const promptEl     = document.getElementById('prompt');
const submitBtn    = document.getElementById('submit-btn');
const runIndicator = document.getElementById('run-indicator');
const resultPanel  = document.getElementById('result-panel');
const overviewPanel = document.getElementById('overview-panel');
const overviewBody = document.getElementById('overview-body');
const errorPanel   = document.getElementById('error-panel');
const jobLogLine   = document.getElementById('job-log-line');
const pdfTabBarWrap = document.getElementById('pdf-tab-bar-wrap');
const submitLabel  = document.getElementById('submit-label');
const submitIconGen = document.getElementById('submit-icon-generate');
const submitIconUpd = document.getElementById('submit-icon-update');
const pdfTabBackBtn = document.getElementById('pdf-tab-back-btn');

// ─── Resize / visibility state ────────────────────────────────────────────────

let pdfResizeObserver = null;
let pdfResizeDebounce = null;
let pdfLayoutStaleWhileHidden = false;

// ─── Overview panel ───────────────────────────────────────────────────────────

function buildOverviewPanel(overview) {
  if (!overviewPanel || !overviewBody) return;
  overviewBody.innerHTML = '';
  if (!overview || !overview.papers || !overview.papers.length) {
    overviewPanel.classList.add('hidden');
    return;
  }
  let hasAny = false;
  overview.papers.forEach(function (paper) {
    if (!paper.exercises || !paper.exercises.length) return;
    hasAny = true;
    const block = document.createElement('div');
    block.className = 'overview-paper-block';
    const plabel = (paper.label && String(paper.label).trim()) ? paper.label : window.i18n['workspace.paper.default_label'];
    const paperBtn = document.createElement('button');
    paperBtn.type = 'button';
    paperBtn.className = 'overview-paper-btn';
    paperBtn.textContent = plabel;
    const firstEx = paper.exercises[0];
    paperBtn.addEventListener('click', function () { scrollPreviewToExercise(firstEx); });
    const row = document.createElement('div');
    row.className = 'overview-q-row';
    paper.exercises.forEach(function (ex) {
      const qb = document.createElement('button');
      qb.type = 'button';
      qb.className = 'overview-q-btn' + (ex.mcq ? ' overview-q-btn-mcq' : '');
      qb.textContent = String(ex.q);
      qb.setAttribute('aria-label', window.tfmt('workspace.aria.go_to_exercise', { q: ex.q }));
      qb.addEventListener('click', function () { scrollPreviewToExercise(ex); });
      row.appendChild(qb);
    });
    block.appendChild(paperBtn);
    block.appendChild(row);
    overviewBody.appendChild(block);
  });
  if (!hasAny) {
    overviewPanel.classList.add('hidden');
    return;
  }
  overviewPanel.classList.remove('hidden');
}

// ─── Exercise navigation ──────────────────────────────────────────────────────

function resolveNavPageYForTab(ex, tabId) {
  const sl = tabIdToSideLayout(tabId);
  if (sl.side === 'answers') {
    if (ex.answers_page != null && ex.answers_y_pt != null) {
      return { page: ex.answers_page, yPt: ex.answers_y_view_pt != null ? ex.answers_y_view_pt : ex.answers_y_pt };
    }
    return { page: 0, yPt: 0 };
  }
  return { page: ex.page, yPt: ex.y_view_pt != null ? ex.y_view_pt : ex.y_pt };
}

function mapSrcPageYToOffsetInCanvas(tabId, srcPage, srcYPt, canvases) {
  const n = canvases.length;
  if (!n) return null;
  const SRC_H = PDF_OUTPUT_PAGE_H_PT;
  const ySrc = Number(srcYPt);
  const layout = tabIdToSideLayout(tabId).layout;
  if (layout === 'exercise') {
    const pg = Math.max(0, Math.min(parseInt(srcPage, 10) || 0, n - 1));
    const c = canvases[pg];
    const ch = c.offsetHeight;
    if (ch <= 0) return null;
    return { pageIdx: pg, yPx: (ySrc / SRC_H) * ch };
  }
  if (layout === 'four-up') {
    const p = parseInt(srcPage, 10) || 0;
    const per = 4;
    const outIdx = Math.max(0, Math.min(Math.floor(p / per), n - 1));
    const slot = ((p % per) + per) % per;
    const row = Math.floor(slot / 2);
    const c4 = canvases[outIdx];
    const H = c4.offsetHeight;
    if (H <= 0) return null;
    const cellH = H / 2;
    return { pageIdx: outIdx, yPx: row * cellH + (ySrc / SRC_H) * cellH };
  }
  if (layout === 'two-up') {
    const p2 = parseInt(srcPage, 10) || 0;
    const per2 = 2;
    const outIdx2 = Math.max(0, Math.min(Math.floor(p2 / per2), n - 1));
    const c2 = canvases[outIdx2];
    const H2 = c2.offsetHeight;
    if (H2 <= 0) return null;
    return { pageIdx: outIdx2, yPx: (ySrc / SRC_H) * H2 };
  }
  return null;
}

function stickyHeaderHeight() {
  const hdr = document.querySelector('.site-header');
  return hdr ? hdr.getBoundingClientRect().height : 0;
}

function scrollPreviewToExercise(ex) {
  if (!workspace.classList.contains('preview-mode')) return;
  let tabId = activePdfTabId();
  if (!tabEnabled(tabId)) tabId = firstEnabledTab();
  void selectTab(tabId).then(function () {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        const sc = scrollEl(tabId);
        const stack = pagesStackEl(tabId);
        if (!sc || !stack || sc.classList.contains('hidden')) return;
        const canvases = stack.querySelectorAll('canvas.pdf-canvas-page');
        const nav = resolveNavPageYForTab(ex, tabId);
        const mapped = mapSrcPageYToOffsetInCanvas(tabId, nav.page, nav.yPt, canvases);
        if (!mapped) return;
        const c = canvases[mapped.pageIdx];
        const scRect = sc.getBoundingClientRect();
        const cRect = c.getBoundingClientRect();
        const topInScrollContent = cRect.top - scRect.top + sc.scrollTop;
        const targetY = topInScrollContent + mapped.yPx;
        const headerH = stickyHeaderHeight();
        sc.scrollTop = Math.max(0, targetY - headerH - 8);
        updateHeaderGlassFromPdfScroll();
        try { sc.focus({ preventScroll: true }); } catch (e2) { sc.focus(); }
      });
    });
  });
}

// ─── Submit button mode ───────────────────────────────────────────────────────

function setSubmitPreviewMode(on) {
  if (!submitLabel) return;
  if (submitIconGen) submitIconGen.classList.toggle('hidden', on);
  if (submitIconUpd) submitIconUpd.classList.toggle('hidden', !on);
  submitLabel.textContent = on ? window.i18n['workspace.submit.update'] : window.i18n['workspace.submit.generate_short'];
}

// ─── Preview mode transitions ─────────────────────────────────────────────────

function exitPreviewMode() {
  state.rankingActive = false;
  sessionStorage.removeItem('previewState');
  workspace.classList.remove('preview-mode', 'preview-mode--settled');
  document.body.classList.remove('preview-mode-active');
  if (pdfTabBarWrap) pdfTabBarWrap.setAttribute('hidden', '');
  pdfPane.setAttribute('aria-hidden', 'true');
  setSubmitPreviewMode(false);
  document.documentElement.style.removeProperty('--header-glass-fill');
  document.body.style.removeProperty('--header-glass-fill');
  workspace.style.removeProperty('opacity');
  workspace.style.removeProperty('transition');
}

function setTabAvailable(tabId, available) {
  const btn = document.getElementById('tab-btn-' + tabId);
  if (btn) {
    btn.disabled = !available;
    btn.setAttribute('aria-disabled', available ? 'false' : 'true');
    btn.title = available ? '' : window.i18n['workspace.tab.not_generated'];
  }
  if (available) { hideSpinner(tabId); hideEmpty(tabId); }
  else           { hideSpinner(tabId); showEmpty(tabId); }
}

async function enterPreviewMode(doneData, instant) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fadeDuration = (instant || reducedMotion) ? 0 : 20;

  workspace.style.transition = 'opacity ' + fadeDuration + 'ms ease';
  workspace.style.opacity = '0';
  await new Promise(function (r) { setTimeout(r, fadeDuration); });

  workspace.classList.add('preview-mode');
  document.body.classList.add('preview-mode-active');
  if (pdfTabBarWrap) pdfTabBarWrap.removeAttribute('hidden');
  pdfPane.removeAttribute('aria-hidden');

  state.enabledTabs.clear();
  const loadPromises = [];
  TABS.forEach(function (tab) {
    const url = doneData[tab.urlKey];
    if (url) {
      state.enabledTabs.add(tab.id);
      setTabAvailable(tab.id, true);
      loadPromises.push(
        loadPdf(tab.id, url).catch(function (err) {
          console.error(err);
          showEmpty(tab.id);
          state.enabledTabs.delete(tab.id);
        })
      );
    } else {
      setTabAvailable(tab.id, false);
    }
  });
  await Promise.all(loadPromises);
  await selectTab(firstEnabledTab());

  setSubmitPreviewMode(true);
  buildOverviewPanel(doneData.overview || { papers: [] });

  await new Promise(function (resolve) {
    requestAnimationFrame(function () { requestAnimationFrame(resolve); });
  });

  workspace.classList.add('preview-mode--settled');
  workspace.style.opacity = '1';

  ensurePdfScrollGlassListeners();

  function rerenderWhenReady(attempts) {
    if (attempts > CONFIG.RERENDER_MAX_ATTEMPTS) return;
    const id = activePdfTabId();
    const sc = scrollEl(id);
    if (sc && sc.clientHeight > 100) {
      void rerenderActivePdfTab().then(function () {
        updateHeaderGlassFromPdfScroll();
      });
    } else {
      setTimeout(function () { rerenderWhenReady(attempts + 1); }, CONFIG.RERENDER_RETRY_MS);
    }
  }
  setTimeout(function () { rerenderWhenReady(0); }, fadeDuration + CONFIG.RERENDER_RETRY_MS);
}

async function refreshPreviewMode() {
  state.rankingActive = false;
  if (overviewPanel) {
    overviewPanel.classList.add('hidden');
    if (overviewBody) overviewBody.innerHTML = '';
  }
  state.enabledTabs.clear();
  await Promise.all(TABS.map(function (tab) { return destroyPdfTab(tab.id); }));
  TABS.forEach(function (tab) {
    const btn = document.getElementById('tab-btn-' + tab.id);
    const sc = scrollEl(tab.id);
    if (sc) sc.classList.remove('hidden');
    const emp = document.getElementById('pdf-empty-' + tab.id);
    if (emp) emp.classList.add('hidden');
    showSpinner(tab.id);
    if (btn) {
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
    }
  });
}

// ─── Job polling ──────────────────────────────────────────────────────────────

function applyLogLine(data) {
  if (!jobLogLine) return;
  let line = (data.log_line != null && data.log_line !== '') ? String(data.log_line) : '';
  if (!line) {
    if      (data.status === 'pending') line = window.i18n['workspace.status.pending'];
    else if (data.status === 'running') line = window.i18n['workspace.status.starting'];
    else                                line = window.i18n['workspace.status.running'];
  }
  jobLogLine.textContent = line;
  jobLogLine.title = line;
}

async function fetchJobStatus(id) {
  const res = await fetch('/api/jobs/' + encodeURIComponent(id), {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) throw new Error(window.i18n['workspace.err.poll']);
  return res.json();
}

function showResultPanel() {
  resultPanel.classList.remove('hidden');
  resultPanel.getBoundingClientRect();
  setTimeout(function () {
    requestAnimationFrame(function () {
      resultPanel.classList.remove('opacity-0', 'translate-y-6', 'pointer-events-none');
    });
  }, 280);
}

function hideResultPanel() {
  resultPanel.classList.add('hidden', 'opacity-0', 'translate-y-6', 'pointer-events-none');
}

function resultPanelIsVisible() {
  return resultPanel && !resultPanel.classList.contains('hidden');
}

async function pollJob(id, onTick) {
  const MAX_POLLS = Math.ceil(20 * 60 * 1000 / CONFIG.POLL_INTERVAL_MS);  // 20-min ceiling
  for (let polls = 0; polls < MAX_POLLS; polls++) {
    const data = await fetchJobStatus(id);
    if (onTick) onTick(data);
    if (data.status === 'failed') throw new Error(data.error || window.i18n['workspace.err.failed']);
    if (data.status === 'done')   return data;
    await sleep(CONFIG.POLL_INTERVAL_MS);
  }
  throw new Error(window.i18n['workspace.err.timeout']);
}

// ─── Pinch-to-zoom (wheel on pdfPane) ────────────────────────────────────────

if (pdfPane) {
  let _zBgTimer = null;
  let _zBgRendering = false;
  let _zBgPending = false;
  let _zSettleTimer = null;

  function _scheduleBgRender(id) {
    if (_zBgRendering) { _zBgPending = true; return; }
    if (_zBgTimer) clearTimeout(_zBgTimer);
    _zBgTimer = setTimeout(function () {
      _zBgTimer = null;
      _zBgRendering = true;
      _zBgPending = false;
      rerenderPdfZoomBuffered(id).then(function (renderedAt) {
        _zBgRendering = false;
        state._zBaseZoom = renderedAt;
        if (_zBgPending) { _zBgPending = false; _scheduleBgRender(id); }
      }).catch(function () { _zBgRendering = false; });
    }, 16);
  }

  pdfPane.addEventListener('wheel', function (e) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const id = activePdfTabId();
    const s = getPdfState(id);
    if (!s.doc || !s.pages.length || !s.pages[0].vpW) return;
    const scroll = scrollEl(id);
    const stack = pagesStackEl(id);
    if (!scroll || !stack) return;
    let dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 16;
    const oldZoom = s.zoom;
    s.zoom = Math.min(6, Math.max(0.2, s.zoom * Math.pow(2, -dy / 100)));
    const ratio = s.zoom / oldZoom;
    if (Math.abs(ratio - 1) < 1e-6) return;

    // --- Read phase (single reflow) ---
    const rect = scroll.getBoundingClientRect();
    const cursorVpX = e.clientX - rect.left;
    const cursorVpY = e.clientY - rect.top;
    const oldScrollL = scroll.scrollLeft;
    const oldScrollT = scroll.scrollTop;
    const cs = getComputedStyle(scroll);
    const contentW = scroll.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);

    // --- Compute phase (no DOM access) ---
    let oldMaxW = 0, newMaxW = 0;
    const newDims = [];
    for (let i = 0; i < s.pages.length; i++) {
      const pg = s.pages[i];
      const ow = Math.floor(pg.vpW * pg.fit * oldZoom);
      if (ow > oldMaxW) oldMaxW = ow;
      const nw = Math.floor(pg.vpW * pg.fit * s.zoom);
      const nh = Math.floor(pg.vpH * pg.fit * s.zoom);
      newDims.push({ w: nw, h: nh });
      if (nw > newMaxW) newMaxW = nw;
    }
    const oldStackW = Math.max(contentW, oldMaxW);
    const newStackW = Math.max(contentW, newMaxW);
    const oldGap = Math.max(0, (oldStackW - oldMaxW) / 2);
    const newGap = Math.max(0, (newStackW - newMaxW) / 2);
    const focalX = oldScrollL + cursorVpX - oldGap;
    const focalY = oldScrollT + cursorVpY;

    // --- Write phase (no layout reads, batch all writes) ---
    for (let i = 0; i < s.pages.length; i++) {
      const pg = s.pages[i], d = newDims[i];
      pg.wrap.style.width = d.w + 'px';
      pg.wrap.style.height = d.h + 'px';
      pg.canvas.style.width = d.w + 'px';
      pg.canvas.style.height = d.h + 'px';
    }
    stack.style.width = newStackW + 'px';
    scroll.scrollLeft = focalX * ratio + newGap - cursorVpX;
    scroll.scrollTop  = focalY * ratio - cursorVpY;
    _scheduleBgRender(id);
    if (_zSettleTimer) clearTimeout(_zSettleTimer);
    _zSettleTimer = setTimeout(function () {
      _zSettleTimer = null;
      state._zBaseZoom = s.zoom;
      renderPdfContinuous(id, true);
    }, CONFIG.ZOOM_SETTLE_MS);
  }, { passive: false });
}

// ─── Keyboard shortcuts ───────────────────────────────────────────────────────

window.addEventListener('keydown', function (e) {
  if (!workspace.classList.contains('preview-mode')) return;
  const el = e.target;
  if (el && el.closest && (el.closest('textarea') || el.closest('input') || el.closest('[contenteditable="true"]'))) return;
  if (!e.ctrlKey && !e.metaKey) {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      const sc = scrollEl(activePdfTabId());
      if (sc) { e.preventDefault(); sc.scrollLeft += e.key === 'ArrowLeft' ? -40 : 40; }
    }
    return;
  }
  if (e.key === '=' || e.key === '+') { e.preventDefault(); void zoomActivePdf(1.12); return; }
  if (e.key === '-' || e.key === '_') { e.preventDefault(); void zoomActivePdf(1 / 1.12); return; }
  if (e.key === '0') { e.preventDefault(); void resetActivePdfZoom(); return; }
  if (e.code === 'NumpadAdd') { e.preventDefault(); void zoomActivePdf(1.12); return; }
  if (e.code === 'NumpadSubtract') { e.preventDefault(); void zoomActivePdf(1 / 1.12); return; }
});

// ─── Resize observer ─────────────────────────────────────────────────────────

if (window.ResizeObserver && pdfPane) {
  pdfResizeObserver = new ResizeObserver(function () {
    if (!workspace.classList.contains('preview-mode')) return;
    if (document.visibilityState === 'hidden') {
      pdfLayoutStaleWhileHidden = true;
      return;
    }
    if (pdfResizeDebounce) clearTimeout(pdfResizeDebounce);
    pdfResizeDebounce = setTimeout(function () {
      pdfResizeDebounce = null;
      syncSidePill();
      const id = activePdfTabId();
      if (tabEnabled(id) && getPdfState(id).doc) {
        void renderPdfContinuous(id).then(function () {
          updateHeaderGlassFromPdfScroll();
        });
      }
    }, CONFIG.RESIZE_DEBOUNCE_MS);
  });
  pdfResizeObserver.observe(pdfPane);
}

document.addEventListener('visibilitychange', function () {
  if (document.visibilityState !== 'visible') return;
  if (!pdfLayoutStaleWhileHidden) return;
  pdfLayoutStaleWhileHidden = false;
  if (!workspace.classList.contains('preview-mode')) return;
  const id = activePdfTabId();
  if (tabEnabled(id) && getPdfState(id).doc) {
    void renderPdfContinuous(id).then(function () {
      updateHeaderGlassFromPdfScroll();
    });
  }
});

// ─── Back button and Backspace ────────────────────────────────────────────────

if (pdfTabBackBtn) {
  pdfTabBackBtn.addEventListener('click', function () { exitPreviewMode(); });
}

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Backspace') return;
  if (!document.body.classList.contains('preview-mode-active')) return;
  if (isTextEntryElement(e.target)) return;
  e.preventDefault();
  exitPreviewMode();
});

function isTextEntryElement(el) {
  if (!el || el.nodeType !== 1) return false;
  const tag = el.tagName;
  if (tag === 'TEXTAREA') return true;
  if (tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el.type || '').toLowerCase();
    if (type === 'button' || type === 'submit' || type === 'reset' || type === 'checkbox' ||
        type === 'radio' || type === 'file' || type === 'range' || type === 'color' || type === 'hidden') {
      return false;
    }
    return true;
  }
  if (el.isContentEditable) return true;
  if (el.getAttribute('role') === 'textbox') return true;
  return false;
}

// ─── Prompt Enter key & quick-fill buttons ───────────────────────────────────

if (promptEl && form) {
  promptEl.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    if (e.shiftKey) return;   // Shift+Enter → newline
    e.preventDefault();
    if (submitBtn && submitBtn.disabled) return;
    if (resultPanelIsVisible() && state.lastDownloadAllUrls.length) {
      errorPanel.classList.add('hidden');
      triggerDownloadAllPdfs().catch(function (err) {
        errorPanel.textContent = err.message || String(err);
        errorPanel.classList.remove('hidden');
      });
      return;
    }
    if (!promptEl.value.trim()) return;
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit();
    } else {
      submitBtn.click();
    }
  });
}

// ─── Prompt char counter (fades in past 50% of maxlength) ────────────────────
(function () {
  if (!promptEl) return;
  const counter = document.getElementById('prompt-char-counter');
  if (!counter) return;
  const max = parseInt(promptEl.getAttribute('maxlength') || '12000', 10);
  const showAt = Math.floor(max * 0.5);
  const amberAt = Math.floor(max * 0.9);
  const colorClasses = ['text-slate-400/70', 'text-amber-300/85', 'text-red-400'];
  function setColor(cls) {
    colorClasses.forEach(function (c) { counter.classList.remove(c); });
    counter.classList.add(cls);
  }
  function update() {
    const n = promptEl.value.length;
    counter.textContent = n.toLocaleString() + ' / ' + max.toLocaleString();
    counter.classList.toggle('opacity-0', n < showAt);
    if (n >= max) setColor('text-red-400');
    else if (n >= amberAt) setColor('text-amber-300/85');
    else setColor('text-slate-400/70');
  }
  promptEl.addEventListener('input', update);
  update();
}());

document.querySelectorAll('.quick-fill').forEach(function (btn) {
  btn.addEventListener('click', function () {
    promptEl.value = btn.getAttribute('data-prompt');
    promptEl.focus();
    btn.classList.remove('quick-fill-just-clicked');
    void btn.offsetWidth; // restart animation if class re-added immediately
    btn.classList.add('quick-fill-just-clicked');
    setTimeout(function () {
      btn.classList.remove('quick-fill-just-clicked');
    }, 400);
  });
});

// ─── Preview entry (shared) ───────────────────────────────────────────────────

// Apply a /api/jobs/<id> "done" payload and enter the tabbed PDF preview.
// Shared by the form-submit success path, the ?job=<id> deep link, and session restore.
function applyAndEnterPreview(data) {
  applyDoneData(data);
  showResultPanel();
  return enterPreviewMode(data, true)
    .then(function () { if (!data.ranking_url) setRankingIdle(); });
}

// ─── Deep link: /?job=<id> opens that run's preview (e.g. from the dashboard) ──

(function () {
  const jobId = new URLSearchParams(window.location.search).get('job');
  if (!jobId) return;
  state.currentJobId = jobId;
  fetchJobStatus(jobId)
    .then(function (data) {
      if (data && data.status === 'done' && data.download_url) return applyAndEnterPreview(data);
      throw new Error(window.i18n['workspace.err.preview_unavailable']);
    })
    .catch(function (err) {
      if (errorPanel) {
        errorPanel.textContent = (err && err.message) ? err.message : String(err);
        errorPanel.classList.remove('hidden');
      }
    });
})();

// ─── Session restore ──────────────────────────────────────────────────────────

(function () {
  // A ?job=<id> deep link takes precedence over a stored preview.
  if (new URLSearchParams(window.location.search).get('job')) return;
  const savedStr = sessionStorage.getItem('previewState');
  if (!savedStr) return;
  let saved;
  try { saved = JSON.parse(savedStr); } catch (e) { sessionStorage.removeItem('previewState'); return; }
  if (!saved || !saved.doneData || !saved.prompt) { sessionStorage.removeItem('previewState'); return; }
  promptEl.value = saved.prompt;
  if (saved.jobId) state.currentJobId = saved.jobId;
  applyAndEnterPreview(saved.doneData).catch(function () {});
})();

// ─── Form submit ──────────────────────────────────────────────────────────────

form.addEventListener('submit', async function (e) {
  e.preventDefault();
  // A fresh run supersedes any ?job=<id> deep link still in the URL.
  if (window.location.search) { try { history.replaceState({}, '', window.location.pathname); } catch (_) {} }
  const prompt = promptEl.value.trim();
  if (!prompt) return;

  errorPanel.classList.add('hidden');
  hideResultPanel();
  state.lastDownloadAllUrls = [];
  if (workspace.classList.contains('preview-mode')) {
    await refreshPreviewMode();
  }
  submitBtn.disabled = true;
  runIndicator.classList.remove('hidden');
  runIndicator.classList.add('flex');
  applyLogLine({ status: 'pending', log_line: '' });

  try {
    const res = await fetch('/api/jobs', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const body = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      const d = body.detail;
      const msg = typeof d === 'string' ? d
        : Array.isArray(d) ? d.map(function (x) { return x.msg || JSON.stringify(x); }).join(' ')
        : JSON.stringify(d || body);
      throw new Error(msg || ('HTTP ' + res.status));
    }
    const id = body.id;
    if (!id) throw new Error(window.i18n['workspace.err.no_job_id']);
    state.currentJobId = id;

    applyLogLine(await fetchJobStatus(id));
    const done = await pollJob(id, applyLogLine);

    try {
      sessionStorage.setItem('previewState', JSON.stringify({ doneData: done, prompt: prompt, jobId: id }));
    } catch (e) {}
    await applyAndEnterPreview(done);
  } catch (err) {
    errorPanel.textContent = err.message || String(err);
    errorPanel.classList.remove('hidden');
    if (workspace.classList.contains('preview-mode')) {
      hideAllPdfSpinners();
    }
  } finally {
    submitBtn.disabled = false;
    runIndicator.classList.add('hidden');
    runIndicator.classList.remove('flex');
  }
});
