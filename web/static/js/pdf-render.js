/**
 * pdf-render.js — PDF.js loading, rendering, zoom, and glass-header helpers.
 * Imports: state.js only.
 */

import {
  state, TAB_ORDER, TABS,
  getPdfState, activePdfTabId, tabEnabled,
  scrollEl, pagesStackEl, loadingEl, emptyEl,
} from './state.js';

// ─── Module-level render state ───────────────────────────────────────────────

let pdfjsPromise = null;
let pdfHeaderGlassRaf = null;
let pdfScrollGlassListenersBound = false;

// Scroll-phase tracking: suppress expensive work (text layers) during momentum.
let _scrolling = false;
let _scrollSettleTimer = null;
function _markScrollActive() {
  _scrolling = true;
  clearTimeout(_scrollSettleTimer);
  _scrollSettleTimer = setTimeout(function () { _scrolling = false; }, 150);
}

// ─── PDF.js bootstrap ────────────────────────────────────────────────────────

export function ensurePdfJs() {
  if (pdfjsPromise) return pdfjsPromise;
  pdfjsPromise = import('https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.mjs').then(function (m) {
    m.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.mjs';
    return m;
  });
  return pdfjsPromise;
}

// ─── Spinner / empty-state helpers ───────────────────────────────────────────

export function showSpinner(id) {
  const el = loadingEl(id);
  if (el) el.classList.remove('hidden');
}
export function hideSpinner(id) {
  const el = loadingEl(id);
  if (el) el.classList.add('hidden');
}
export function showEmpty(id) {
  const e = emptyEl(id);
  const sc = scrollEl(id);
  if (e) e.classList.remove('hidden');
  if (sc) sc.classList.add('hidden');
}
export function hideEmpty(id) {
  const e = emptyEl(id);
  const sc = scrollEl(id);
  if (e) e.classList.add('hidden');
  if (sc) sc.classList.remove('hidden');
}
export function hideAllPdfSpinners() {
  TABS.forEach(function (tab) { hideSpinner(tab.id); });
}

// ─── Surface management ───────────────────────────────────────────────────────

/** Drop canvas + text layer DOM for one tab; keeps PDF.js document in memory. */
export function clearPdfSurfaces(id) {
  const s = getPdfState(id);
  if (s.observer) { s.observer.disconnect(); s.observer = null; }
  const stack = pagesStackEl(id);
  if (stack) stack.innerHTML = '';
  s.pages = [];
}

/** Free GPU/RAM for all tabs except the active one (documents stay loaded). */
export function clearInactivePdfSurfaces(activeId) {
  TAB_ORDER.forEach(function (tid) {
    if (tid === activeId) return;
    if (getPdfState(tid).doc) clearPdfSurfaces(tid);
  });
}

/** Re-layout only the visible tab (avoids holding 4× full canvas sets in RAM). */
export function rerenderActivePdfTab() {
  const id = activePdfTabId();
  if (!tabEnabled(id) || !getPdfState(id).doc) return Promise.resolve();
  return renderPdfContinuous(id).then(function () {
    clearInactivePdfSurfaces(id);
  });
}

// ─── Tab lifecycle ────────────────────────────────────────────────────────────

export async function destroyPdfTab(id) {
  const s = getPdfState(id);
  if (s.observer) { s.observer.disconnect(); s.observer = null; }
  if (s.loadingTask) {
    try { s.loadingTask.destroy(); } catch (err) {}
    s.loadingTask = null;
  }
  if (s.doc) {
    try { await s.doc.destroy(); } catch (err) {}
    s.doc = null;
  }
  s.zoom = 1;
  s.baseFit = 1;
  s.pages = [];
  const stack = pagesStackEl(id);
  if (stack) stack.innerHTML = '';
}

export async function loadPdf(id, url) {
  await destroyPdfTab(id);
  const s = getPdfState(id);
  const pdfjs = await ensurePdfJs();
  const fetchUrl = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'inline=1';
  const loadingTask = pdfjs.getDocument({ url: fetchUrl, withCredentials: true });
  s.loadingTask = loadingTask;
  const doc = await loadingTask.promise;
  s.loadingTask = null;
  s.doc = doc;
  s.zoom = 1;
}

// ─── Zoom ─────────────────────────────────────────────────────────────────────

export async function zoomActivePdf(factor) {
  if (!document.body.classList.contains('preview-mode-active')) return;
  const id = activePdfTabId();
  const s = getPdfState(id);
  if (!s.doc) return;
  s.zoom = Math.min(6, Math.max(0.2, s.zoom * factor));
  await renderPdfContinuous(id);
}

export async function resetActivePdfZoom() {
  const id = activePdfTabId();
  const s = getPdfState(id);
  if (!s.doc) return;
  s.zoom = 1;
  await renderPdfContinuous(id);
}

// ─── Core rendering ───────────────────────────────────────────────────────────

function _computeBaseFit(s, id) {
  const scroll = scrollEl(id);
  if (!scroll) return s.baseFit || 1;
  const cs = getComputedStyle(scroll);
  const padL = parseFloat(cs.paddingLeft) || 0;
  const padR = parseFloat(cs.paddingRight) || 0;
  const padT = parseFloat(cs.paddingTop) || 0;
  const padB = parseFloat(cs.paddingBottom) || 0;
  const innerW = Math.floor(scroll.clientWidth - padL - padR);
  const innerH = Math.floor(scroll.clientHeight - padT - padB);
  return {
    cw: innerW > 64 ? innerW : 400,
    ch: innerH > 64 ? innerH : 520,
  };
}

// reuseBaseFit: when true, keep s.baseFit as-is instead of re-measuring from
// the scroll container.  Pass true for zoom-triggered re-renders so that
// scrollbar appearance/disappearance at low zoom levels cannot produce a
// visible page-size jump.
async function _renderSinglePage(id, pageIdx) {
  const s = getPdfState(id);
  const pg = s.pages[pageIdx];
  if (!pg || pg.rendered || pg.rendering) return;
  pg.rendering = true;
  const pdfjs = await ensurePdfJs();
  try {
    const page = await s.doc.getPage(pg.pageNum);
    const userScale = s.baseFit * s.zoom;
    const cssVp = page.getViewport({ scale: userScale });
    const scaledVp = page.getViewport({ scale: userScale * s.dpr });
    if (!pg.canvasRendered) {
      const ctx = pg.canvas.getContext('2d', { alpha: false });
      if (ctx) {
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, pg.canvas.width, pg.canvas.height);
        const task = page.render({ canvasContext: ctx, viewport: scaledVp });
        await task.promise;
      }
    }
    if (s.pages[pageIdx] === pg) pg.rendered = true;
    // Defer text layer creation so it never runs during scroll momentum.
    if (pdfjs.TextLayer) {
      _scheduleTextLayer(id, pageIdx, page, cssVp, userScale);
    } else {
      try { page.cleanup(); } catch (cleanErr) {}
    }
  } catch (err) {
    console.error('PDF render failed', err);
  } finally {
    pg.rendering = false;
  }
}

/** Schedule text layer creation during idle time, keeping scroll jank-free. */
function _scheduleTextLayer(id, pageIdx, page, cssVp, userScale) {
  var schedule = window.requestIdleCallback || function (cb) { setTimeout(cb, 150); };
  schedule(async function () {
    // Defer further if the user is still scrolling — text layer DOM work competes
    // with compositor momentum on MacBook trackpads.
    if (_scrolling) {
      setTimeout(function () { _scheduleTextLayer(id, pageIdx, page, cssVp, userScale); }, 200);
      return;
    }
    var s = getPdfState(id);
    var pg = s.pages && s.pages[pageIdx];
    if (!pg || !pg.rendered) {
      try { page.cleanup(); } catch (e) {}
      return;
    }
    try {
      var textContent = await page.getTextContent();
      var existingTl = pg.wrap.querySelector('.textLayer');
      if (existingTl) existingTl.remove();
      var textLayerDiv = document.createElement('div');
      textLayerDiv.className = 'textLayer';
      textLayerDiv.style.setProperty('--scale-factor', String(userScale));
      pg.wrap.appendChild(textLayerDiv);
      var pdfjs = await ensurePdfJs();
      var tl = new pdfjs.TextLayer({
        textContentSource: textContent,
        container: textLayerDiv,
        viewport: cssVp,
      });
      await tl.render();
    } catch (texErr) {
      console.warn('PDF text layer failed', texErr);
    }
    try { page.cleanup(); } catch (e) {}
  }, { timeout: 2000 });
}

function _clearPageCanvas(id, pageIdx) {
  const s = getPdfState(id);
  const pg = s.pages[pageIdx];
  if (!pg || !pg.rendered || pg.rendering) return;
  const ctx = pg.canvas.getContext('2d', { alpha: false });
  if (ctx) { ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, pg.canvas.width, pg.canvas.height); }
  const tl = pg.wrap.querySelector('.textLayer');
  if (tl) tl.remove();
  pg.rendered = false;
}

function _setupPageObserver(id) {
  const s = getPdfState(id);
  if (s.observer) { s.observer.disconnect(); s.observer = null; }
  const scroll = scrollEl(id);
  if (!scroll || !s.pages.length) return;
  const obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      const pageIdx = parseInt(entry.target.dataset.pageIdx, 10);
      if (isNaN(pageIdx)) return;
      const pg = s.pages[pageIdx];
      if (!pg) return;
      if (entry.isIntersecting) {
        if (!pg.rendered && !pg.rendering) _renderSinglePage(id, pageIdx);
      } else {
        // Evict pages that have scrolled far away to keep GPU memory bounded.
        // The observer re-renders them automatically when they come back into view.
        const rect = entry.boundingClientRect;
        const distancePx = Math.min(
          Math.abs(rect.top - scroll.clientHeight),
          Math.abs(rect.bottom)
        );
        if (distancePx > 2400) _clearPageCanvas(id, pageIdx);
      }
    });
  }, { root: scroll, rootMargin: '600px 0px 600px 0px', threshold: 0 });
  s.observer = obs;
  s.pages.forEach(function (pg, idx) {
    pg.wrap.dataset.pageIdx = String(idx);
    obs.observe(pg.wrap);
  });
}

export async function renderPdfContinuous(id, reuseBaseFit) {
  const s = getPdfState(id);
  if (!s.doc) return;
  const scroll = scrollEl(id);
  const stack = pagesStackEl(id);
  if (!scroll || !stack) return;

  const _savedScrollTop  = scroll.scrollTop;
  const _savedScrollLeft = scroll.scrollLeft;
  const _oldScrollH = scroll.scrollHeight;
  const _oldScrollW = scroll.scrollWidth;

  // Build all new pages OFFSCREEN in a DocumentFragment so the visible
  // stack is never empty — avoids the flash/jump to top-left.
  const frag = document.createDocumentFragment();
  const newPages = [];
  const pdfjs = await ensurePdfJs();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  s.dpr = dpr;
  const n = s.doc.numPages;
  let baseFit;
  if (reuseBaseFit && s.baseFit && s.baseFit !== 1) {
    baseFit = s.baseFit;
  } else {
    const fit = _computeBaseFit(s, id);
    const page1 = await s.doc.getPage(1);
    const vp1 = page1.getViewport({ scale: 1 });
    const scaleW = fit.cw / vp1.width;
    const scaleH = fit.ch / vp1.height;
    baseFit = Math.min(scaleW, scaleH) * 1.02;
    s.baseFit = baseFit;
  }
  let maxDispW = 0;
  for (let p = 1; p <= n; p++) {
    const page = await s.doc.getPage(p);
    const userScale = baseFit * s.zoom;
    const cssVp = page.getViewport({ scale: userScale });
    const scaledVp = page.getViewport({ scale: userScale * dpr });
    const pageWrap = document.createElement('div');
    pageWrap.className = 'pdf-page-wrap';
    const dispW = Math.floor(scaledVp.width / dpr);
    const dispH = Math.floor(scaledVp.height / dpr);
    if (dispW > maxDispW) maxDispW = dispW;
    pageWrap.style.width = dispW + 'px';
    pageWrap.style.height = dispH + 'px';
    frag.appendChild(pageWrap);
    const canvas = document.createElement('canvas');
    canvas.className = 'pdf-canvas pdf-canvas-page';
    canvas.width = Math.floor(scaledVp.width);
    canvas.height = Math.floor(scaledVp.height);
    canvas.style.width = dispW + 'px';
    canvas.style.height = dispH + 'px';
    pageWrap.appendChild(canvas);
    // Paint a white placeholder — actual rasterization is deferred entirely to
    // the IntersectionObserver so the PDF.js worker stays idle during scrolling.
    const ctx = canvas.getContext('2d', { alpha: false });
    if (ctx) {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    newPages.push({
      wrap: pageWrap, canvas: canvas, pageNum: p,
      vpW: cssVp.width / userScale, vpH: cssVp.height / userScale,
      rendered: false, rendering: false, canvasRendered: false,
    });
  }
  s.pages = newPages;

  // Atomic DOM swap — page shells in place, observer will drive rasterization.
  stack.innerHTML = '';
  stack.appendChild(frag);
  // Render the first page immediately so the viewer is never blank on load.
  _renderSinglePage(id, 0);

  // Set stack width to max(contentWidth, maxDispW).
  if (maxDispW > 0) {
    const cs0 = getComputedStyle(scroll);
    const contentW = scroll.clientWidth - (parseFloat(cs0.paddingLeft) || 0) - (parseFloat(cs0.paddingRight) || 0);
    stack.style.width = Math.max(contentW, maxDispW) + 'px';
  }
  // Restore scroll position scaled to the new content size, so the same
  // part of the document stays in view after a zoom re-render.
  if (_oldScrollH > scroll.clientHeight && scroll.scrollHeight > scroll.clientHeight) {
    scroll.scrollTop  = _savedScrollTop  * (scroll.scrollHeight / _oldScrollH);
  }
  if (_oldScrollW > scroll.clientWidth  && scroll.scrollWidth  > scroll.clientWidth) {
    scroll.scrollLeft = _savedScrollLeft * (scroll.scrollWidth  / _oldScrollW);
  }
  state._zBaseZoom = s.zoom;
  stack.style.transform = '';
  stack.style.transformOrigin = '';
  _setupPageObserver(id);
  updateHeaderGlassFromPdfScroll();
}

/** Double-buffered zoom render: renders visible pages to OFFSCREEN canvases,
    then swaps them into the DOM atomically (no flicker).
    Returns the zoom level it rendered at. */
export async function rerenderPdfZoomBuffered(id) {
  const s = getPdfState(id);
  if (!s.doc || !s.pages.length || !s.pages[0].vpW) return s.zoom;
  const scroll = scrollEl(id);
  if (!scroll) return s.zoom;
  const targetZoom = s.zoom;
  const pagesSnapshot = s.pages.slice();  // capture for stale-after-settle guard
  const dpr = s.dpr || Math.min(window.devicePixelRatio || 1, 2);
  const renderScale = s.baseFit * targetZoom * dpr;

  const dims = [];
  for (let i = 0; i < pagesSnapshot.length; i++) {
    const pg = pagesSnapshot[i];
    dims.push({
      w: Math.floor(pg.vpW * renderScale),
      h: Math.floor(pg.vpH * renderScale),
    });
  }

  const scrollTop = scroll.scrollTop;
  const scrollBot = scrollTop + scroll.clientHeight;
  const swaps = [];
  const renderPromises = [];
  for (let i = 0; i < pagesSnapshot.length; i++) {
    const pg = pagesSnapshot[i];
    const top = pg.wrap.offsetTop;
    const bot = top + pg.wrap.offsetHeight;
    if (bot < scrollTop - 200 || top > scrollBot + 200) continue;
    (function (idx, dim) {
      renderPromises.push(
        s.doc.getPage(pagesSnapshot[idx].pageNum).then(function (page) {
          const vp = page.getViewport({ scale: renderScale });
          const offCanvas = document.createElement('canvas');
          offCanvas.className = 'pdf-canvas pdf-canvas-page';
          offCanvas.width = dim.w;
          offCanvas.height = dim.h;
          offCanvas.style.width  = pagesSnapshot[idx].wrap.style.width;
          offCanvas.style.height = pagesSnapshot[idx].wrap.style.height;
          const ctx = offCanvas.getContext('2d', { alpha: false });
          if (!ctx) return;
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, dim.w, dim.h);
          return page.render({ canvasContext: ctx, viewport: vp }).promise.then(function () {
            swaps.push({ idx: idx, canvas: offCanvas });
          });
        })
      );
    })(i, dims[i]);
  }
  await Promise.all(renderPromises);

  return new Promise(function (resolve) {
    requestAnimationFrame(function () {
      for (let j = 0; j < swaps.length; j++) {
        const sw = swaps[j];
        // Skip if pages were rebuilt by settle since this render started.
        if (s.pages[sw.idx] !== pagesSnapshot[sw.idx]) continue;
        const pg = pagesSnapshot[sw.idx];
        // Use CURRENT wrap size (wheel events may have resized since render start).
        sw.canvas.style.width  = pg.wrap.style.width;
        sw.canvas.style.height = pg.wrap.style.height;
        pg.wrap.replaceChild(sw.canvas, pg.canvas);
        pg.canvas = sw.canvas;
      }
      resolve(targetZoom);
    });
  });
}

// ─── Header glass scroll effect ──────────────────────────────────────────────

export function updateHeaderGlassFromPdfScroll() {
  const root = document.documentElement;
  if (!document.body.classList.contains('preview-mode-active')) {
    root.style.removeProperty('--header-glass-fill');
    return;
  }
  const id = activePdfTabId();
  const sc = scrollEl(id);
  if (!sc || sc.classList.contains('hidden')) {
    root.style.setProperty('--header-glass-fill', '1');
    return;
  }
  const maxScroll = sc.scrollHeight - sc.clientHeight;
  let t = 0;
  if (maxScroll > 2) {
    t = Math.min(1, sc.scrollTop / Math.min(maxScroll, 300));
  }
  root.style.setProperty('--header-glass-fill', String(1 - 0.68 * t));
}

export function scheduleHeaderGlassFromPdfScroll() {
  if (pdfHeaderGlassRaf != null) return;
  pdfHeaderGlassRaf = requestAnimationFrame(function () {
    pdfHeaderGlassRaf = null;
    updateHeaderGlassFromPdfScroll();
  });
}

export function ensurePdfScrollGlassListeners() {
  if (pdfScrollGlassListenersBound) return;
  pdfScrollGlassListenersBound = true;
  document.querySelectorAll('.pdf-viewport-scroll').forEach(function (el) {
    el.addEventListener('scroll', scheduleHeaderGlassFromPdfScroll, { passive: true });
    el.addEventListener('scroll', _markScrollActive, { passive: true });
  });
}


