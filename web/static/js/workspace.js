(function () {
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
  const dlMain       = document.getElementById('dl-main');
  const dlAnswers    = document.getElementById('dl-answers');
  const dlAnswersMain = document.getElementById('dl-answers-main');
  const dlAnswersTwoUp = document.getElementById('dl-answers-two-up');
  const dlAnswersFourUp = document.getElementById('dl-answers-four-up');
  const dlFourUp     = document.getElementById('dl-four-up');
  const dlTwoUp      = document.getElementById('dl-two-up');
  const dlAll        = document.getElementById('dl-all');
  const jobLogLine   = document.getElementById('job-log-line');
  const pdfTabBarWrap = document.getElementById('pdf-tab-bar-wrap');
  const submitLabel  = document.getElementById('submit-label');
  const submitIconGen = document.getElementById('submit-icon-generate');
  const submitIconUpd = document.getElementById('submit-icon-update');
  const pdfTabPrimaryTrack = document.getElementById('pdf-tab-primary-track');
  const pdfTabLayoutTrack  = document.getElementById('pdf-tab-layout-track');
  const pdfTabSheetColumn  = pdfTabPrimaryTrack ? pdfTabPrimaryTrack.querySelector('.pdf-tab-sheet-column') : null;
  const pdfTabLayoutColumn = pdfTabPrimaryTrack ? pdfTabPrimaryTrack.querySelector('.pdf-tab-layout-column') : null;
  const pdfTabAnswersColumn = pdfTabPrimaryTrack ? pdfTabPrimaryTrack.querySelector('.pdf-tab-answers-column') : null;
  const pdfTabSidePill     = document.getElementById('pdf-tab-side-pill');
  const pdfTabBackBtn      = document.getElementById('pdf-tab-back-btn');

  const TAB_ORDER = ['exercise', 'two-up', 'four-up', 'answers', 'answers-two-up', 'answers-four-up', 'ranking'];
  const PDF_OUTPUT_PAGE_H_PT = 842;
  const TABS = [
    { id: 'exercise',        urlKey: 'download_url' },
    { id: 'answers',         urlKey: 'answers_url' },
    { id: 'two-up',          urlKey: 'two_up_url' },
    { id: 'four-up',         urlKey: 'four_up_url' },
    { id: 'answers-two-up',  urlKey: 'answers_two_up_url' },
    { id: 'answers-four-up', urlKey: 'answers_four_up_url' },
    { id: 'ranking',         urlKey: 'ranking_url' },
  ];

  /* Two-axis state: which side (sheet/answers) × which layout (1up/2up/4up). */
  var currentSide = 'sheet';    // 'sheet' | 'answers'
  var currentLayout = 'exercise'; // 'exercise' | 'two-up' | 'four-up'
  var rankingActive = false;    // true when the ranking tab is in the foreground

  /** Map (side, layout) → tab id. */
  function resolveTabId(side, layout) {
    if (side === 'answers') {
      if (layout === 'two-up')  return 'answers-two-up';
      if (layout === 'four-up') return 'answers-four-up';
      return 'answers';
    }
    return layout; // 'exercise' | 'two-up' | 'four-up'
  }

  /** Inverse: tab id → { side, layout }. */
  function tabIdToSideLayout(id) {
    if (id === 'answers')         return { side: 'answers', layout: 'exercise' };
    if (id === 'answers-two-up')  return { side: 'answers', layout: 'two-up' };
    if (id === 'answers-four-up') return { side: 'answers', layout: 'four-up' };
    if (id === 'two-up')          return { side: 'sheet',   layout: 'two-up' };
    if (id === 'four-up')         return { side: 'sheet',   layout: 'four-up' };
    return { side: 'sheet', layout: 'exercise' };
  }

  var lastDownloadAllUrls = [];

  var pdfTabState = {};
  var pdfjsPromise = null;
  var pdfResizeObserver = null;
  var pdfResizeDebounce = null;
  var pdfHeaderGlassRaf = null;
  var pdfScrollGlassListenersBound = false;
  var pdfSmoothWheelBound = false;
  var pdfLayoutStaleWhileHidden = false;
  var _zBaseZoom = 1;  // zoom level the canvases are currently rendered at

  function bindPdfSmoothWheelScroll() {
    /* Native macOS trackpad scrolling is already smooth with inertia.
       We only attach a passive scroll listener for the header glass effect;
       no wheel event interception, so the browser handles momentum natively. */
    if (pdfSmoothWheelBound) return;
    pdfSmoothWheelBound = true;
    document.querySelectorAll('.pdf-viewport-scroll').forEach(function (el) {
      el.addEventListener(
        'scroll',
        function () { scheduleHeaderGlassFromPdfScroll(); },
        { passive: true }
      );
    });
  }

  function updateHeaderGlassFromPdfScroll() {
    var root = document.documentElement;
    var body = document.body;
    if (!body.classList.contains('preview-mode-active')) {
      root.style.removeProperty('--header-glass-fill');
      body.style.removeProperty('--header-glass-fill');
      return;
    }
    var id = activePdfTabId();
    var sc = scrollEl(id);
    if (!sc || sc.classList.contains('hidden')) {
      root.style.setProperty('--header-glass-fill', '1');
      body.style.setProperty('--header-glass-fill', '1');
      return;
    }
    var maxScroll = sc.scrollHeight - sc.clientHeight;
    var t = 0;
    if (maxScroll > 2) {
      t = Math.min(1, sc.scrollTop / Math.min(maxScroll, 300));
    }
    var fill = 1 - 0.68 * t;
    var fs = String(fill);
    root.style.setProperty('--header-glass-fill', fs);
    body.style.setProperty('--header-glass-fill', fs);
  }

  function scheduleHeaderGlassFromPdfScroll() {
    if (pdfHeaderGlassRaf != null) return;
    pdfHeaderGlassRaf = requestAnimationFrame(function () {
      pdfHeaderGlassRaf = null;
      updateHeaderGlassFromPdfScroll();
    });
  }

  function ensurePdfScrollGlassListeners() {
    if (pdfScrollGlassListenersBound) return;
    pdfScrollGlassListenersBound = true;
    document.querySelectorAll('.pdf-viewport-scroll').forEach(function (el) {
      el.addEventListener('scroll', scheduleHeaderGlassFromPdfScroll, { passive: true });
    });
  }

  function getPdfState(id) {
    if (!pdfTabState[id]) pdfTabState[id] = {
      doc: null, zoom: 1, loadingTask: null,
      baseFit: 1, dpr: 1, pages: [],   // pages: [{wrap, canvas, pageNum, vpW, vpH, rendered, rendering}]
      observer: null
    };
    return pdfTabState[id];
  }

  /** Drop canvas + text layer DOM for one tab; keeps PDF.js document in memory for instant tab switch. */
  function clearPdfSurfaces(id) {
    var s = getPdfState(id);
    if (s.observer) { s.observer.disconnect(); s.observer = null; }
    var stack = pagesStackEl(id);
    if (stack) stack.innerHTML = '';
    s.pages = [];
  }

  /** Free GPU/RAM for all tabs except the active one (documents stay loaded). */
  function clearInactivePdfSurfaces(activeId) {
    TAB_ORDER.forEach(function (tid) {
      if (tid === activeId) return;
      if (getPdfState(tid).doc) clearPdfSurfaces(tid);
    });
  }

  /** Re-layout only the visible tab (avoids holding 4× full canvas sets in RAM). */
  function rerenderActivePdfTab() {
    var id = activePdfTabId();
    if (!tabEnabled(id) || !getPdfState(id).doc) return Promise.resolve();
    return renderPdfContinuous(id).then(function () {
      clearInactivePdfSurfaces(id);
    });
  }

  function ensurePdfJs() {
    if (pdfjsPromise) return pdfjsPromise;
    pdfjsPromise = import('https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.mjs').then(function (m) {
      m.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.mjs';
      return m;
    });
    return pdfjsPromise;
  }

  function scrollEl(id) { return document.getElementById('pdf-scroll-' + id); }
  function pagesStackEl(id) { return document.getElementById('pdf-pages-' + id); }
  function loadingEl(id) { return document.getElementById('pdf-loading-' + id); }
  function emptyEl(id) { return document.getElementById('pdf-empty-' + id); }
  function tabBtn(id) { return document.getElementById('tab-btn-' + id); }
  function panelEl(id) { return document.getElementById('tab-panel-' + id); }

  function showSpinner(id) {
    var el = loadingEl(id);
    if (el) el.classList.remove('hidden');
  }
  function hideSpinner(id) {
    var el = loadingEl(id);
    if (el) el.classList.add('hidden');
  }
  function showEmpty(id) {
    var e = emptyEl(id);
    var sc = scrollEl(id);
    if (e) e.classList.remove('hidden');
    if (sc) sc.classList.add('hidden');
    void destroyPdfTab(id).catch(function () {});
  }
  function hideEmpty(id) {
    var e = emptyEl(id);
    var sc = scrollEl(id);
    if (e) e.classList.add('hidden');
    if (sc) sc.classList.remove('hidden');
  }

  async function destroyPdfTab(id) {
    var s = getPdfState(id);
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
    var stack = pagesStackEl(id);
    if (stack) stack.innerHTML = '';
  }

  function _computeBaseFit(s, id) {
    var scroll = scrollEl(id);
    if (!scroll) return s.baseFit || 1;
    var cs = getComputedStyle(scroll);
    var padL = parseFloat(cs.paddingLeft) || 0;
    var padR = parseFloat(cs.paddingRight) || 0;
    var padT = parseFloat(cs.paddingTop) || 0;
    var padB = parseFloat(cs.paddingBottom) || 0;
    var innerW = Math.floor(scroll.clientWidth - padL - padR);
    var innerH = Math.floor(scroll.clientHeight - padT - padB);
    var cw = innerW > 64 ? innerW : 400;
    var ch = innerH > 64 ? innerH : 520;
    return { cw: cw, ch: ch };
  }

  // reuseBaseFit: when true, keep s.baseFit as-is instead of re-measuring from
  // the scroll container.  Pass true for zoom-triggered re-renders so that
  // scrollbar appearance/disappearance at low zoom levels cannot change the
  // measured container width and produce a visible page-size jump.
  async function _renderSinglePage(id, pageIdx) {
    var s = getPdfState(id);
    var pg = s.pages[pageIdx];
    if (!pg || pg.rendered || pg.rendering) return;
    pg.rendering = true;
    var pdfjs = await ensurePdfJs();
    try {
      var page = await s.doc.getPage(pg.pageNum);
      var userScale = s.baseFit * s.zoom;
      var cssVp = page.getViewport({ scale: userScale });
      var scaledVp = page.getViewport({ scale: userScale * s.dpr });
      if (!pg.canvasRendered) {
        var ctx = pg.canvas.getContext('2d', { alpha: false });
        if (ctx) {
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, pg.canvas.width, pg.canvas.height);
          var task = page.render({ canvasContext: ctx, viewport: scaledVp });
          await task.promise;
        }
      }
      if (pdfjs.TextLayer) {
        try {
          var textContent = await page.getTextContent();
          var existingTl = pg.wrap.querySelector('.textLayer');
          if (existingTl) existingTl.remove();
          var textLayerDiv = document.createElement('div');
          textLayerDiv.className = 'textLayer';
          textLayerDiv.style.setProperty('--scale-factor', String(userScale));
          pg.wrap.appendChild(textLayerDiv);
          var tl = new pdfjs.TextLayer({
            textContentSource: textContent,
            container: textLayerDiv,
            viewport: cssVp
          });
          await tl.render();
        } catch (texErr) {
          console.warn('PDF text layer failed', texErr);
        }
      }
      try { page.cleanup(); } catch (cleanErr) {}
      // Guard: only mark rendered if s.pages hasn't been replaced by a concurrent re-render
      if (s.pages[pageIdx] === pg) pg.rendered = true;
    } catch (err) {
      console.error('PDF render failed', err);
    } finally {
      pg.rendering = false;
    }
  }

  function _clearPageCanvas(id, pageIdx) {
    var s = getPdfState(id);
    var pg = s.pages[pageIdx];
    if (!pg || !pg.rendered || pg.rendering) return;
    var ctx = pg.canvas.getContext('2d', { alpha: false });
    if (ctx) { ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, pg.canvas.width, pg.canvas.height); }
    var tl = pg.wrap.querySelector('.textLayer');
    if (tl) tl.remove();
    pg.rendered = false;
  }

  function _setupPageObserver(id) {
    var s = getPdfState(id);
    if (s.observer) { s.observer.disconnect(); s.observer = null; }
    var scroll = scrollEl(id);
    if (!scroll || !s.pages.length) return;
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var pageIdx = parseInt(entry.target.dataset.pageIdx, 10);
        if (isNaN(pageIdx)) return;
        var pg = s.pages[pageIdx];
        if (!pg) return;
        if (entry.isIntersecting && !pg.rendered && !pg.rendering) {
          _renderSinglePage(id, pageIdx);
        }
      });
    }, {
      root: scroll,
      rootMargin: '1500px 0px 1500px 0px',
      threshold: 0
    });
    s.observer = obs;
    s.pages.forEach(function (pg, idx) {
      pg.wrap.dataset.pageIdx = String(idx);
      obs.observe(pg.wrap);
    });
  }

  async function renderPdfContinuous(id, reuseBaseFit) {
    var s = getPdfState(id);
    if (!s.doc) return;
    var scroll = scrollEl(id);
    var stack = pagesStackEl(id);
    if (!scroll || !stack) return;
    // Save scroll position so we can restore it proportionally after re-render.
    var _savedScrollTop  = scroll.scrollTop;
    var _savedScrollLeft = scroll.scrollLeft;
    var _oldScrollH = scroll.scrollHeight;
    var _oldScrollW = scroll.scrollWidth;
    // Build all new pages OFFSCREEN in a DocumentFragment so the visible
    // stack is never empty — avoids the flash/jump to top-left.
    var frag = document.createDocumentFragment();
    var newPages = [];
    var pdfjs = await ensurePdfJs();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    s.dpr = dpr;
    var n = s.doc.numPages;
    var baseFit;
    if (reuseBaseFit && s.baseFit && s.baseFit !== 1) {
      baseFit = s.baseFit;
    } else {
      var fit = _computeBaseFit(s, id);
      var page1 = await s.doc.getPage(1);
      var vp1 = page1.getViewport({ scale: 1 });
      var scaleW = fit.cw / vp1.width;
      var scaleH = fit.ch / vp1.height;
      baseFit = Math.min(scaleW, scaleH) * 1.02;
      s.baseFit = baseFit;
    }
    var maxDispW = 0;
    var _canvasRenderPromises = [];
    for (var p = 1; p <= n; p++) {
      var page = await s.doc.getPage(p);
      var userScale = baseFit * s.zoom;
      var cssVp = page.getViewport({ scale: userScale });
      var scaledVp = page.getViewport({ scale: userScale * dpr });
      var pageWrap = document.createElement('div');
      pageWrap.className = 'pdf-page-wrap';
      var dispW = Math.floor(scaledVp.width / dpr);
      var dispH = Math.floor(scaledVp.height / dpr);
      if (dispW > maxDispW) maxDispW = dispW;
      pageWrap.style.width = dispW + 'px';
      pageWrap.style.height = dispH + 'px';
      frag.appendChild(pageWrap);
      var canvas = document.createElement('canvas');
      canvas.className = 'pdf-canvas pdf-canvas-page';
      canvas.width = Math.floor(scaledVp.width);
      canvas.height = Math.floor(scaledVp.height);
      canvas.style.width = dispW + 'px';
      canvas.style.height = dispH + 'px';
      pageWrap.appendChild(canvas);
      var ctx = canvas.getContext('2d', { alpha: false });
      if (ctx) {
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        // Start all canvas renders immediately (pdf.js worker queues them).
        // We await the full batch below before DOM swap so no page is ever white.
        _canvasRenderPromises.push(page.render({ canvasContext: ctx, viewport: scaledVp }).promise);
      }
      newPages.push({ wrap: pageWrap, canvas: canvas, pageNum: p,
                      vpW: cssVp.width / userScale, vpH: cssVp.height / userScale,
                      rendered: false, rendering: false, canvasRendered: false });
    }
    // Wait for every canvas to finish rendering before swapping into the DOM —
    // guarantees the user never sees a white placeholder regardless of scroll speed.
    if (_canvasRenderPromises.length) await Promise.all(_canvasRenderPromises);
    for (var _ci = 0; _ci < newPages.length; _ci++) newPages[_ci].canvasRendered = true;
    s.pages = newPages;
    // Atomic DOM swap — all canvases fully rendered.
    stack.innerHTML = '';
    stack.appendChild(frag);
    // Set stack width to max(contentWidth, maxDispW).
    if (maxDispW > 0) {
      var cs0 = getComputedStyle(scroll);
      var contentW = scroll.clientWidth - (parseFloat(cs0.paddingLeft) || 0) - (parseFloat(cs0.paddingRight) || 0);
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
    _zBaseZoom = s.zoom;
    stack.style.transform = '';
    stack.style.transformOrigin = '';
    _setupPageObserver(id);
    updateHeaderGlassFromPdfScroll();
  }

  /** Double-buffered zoom render: renders visible pages to OFFSCREEN canvases,
      then swaps them into the DOM atomically (no flicker).
      Returns the zoom level it rendered at. */
  async function rerenderPdfZoomBuffered(id) {
    var s = getPdfState(id);
    if (!s.doc || !s.pages.length || !s.pages[0].vpW) return s.zoom;
    var scroll = scrollEl(id);
    if (!scroll) return s.zoom;
    var targetZoom = s.zoom;
    var dpr = s.dpr || Math.min(window.devicePixelRatio || 1, 2);
    // Render at full DPR so bg-render quality matches the settle full render —
    // no second visible sharpening step when the settle fires.
    var renderScale = s.baseFit * targetZoom * dpr;
    var cssScale   = s.baseFit * targetZoom;        // layout / CSS size (no DPR)

    // Compute pixel dims instantly from stored native viewport sizes.
    var dims = [];
    for (var i = 0; i < s.pages.length; i++) {
      var pg = s.pages[i];
      dims.push({ w: Math.floor(pg.vpW * renderScale), h: Math.floor(pg.vpH * renderScale),
                  cssW: pg.wrap.style.width, cssH: pg.wrap.style.height });
    }

    // Render visible pages to offscreen canvases in parallel.
    var scrollTop = scroll.scrollTop;
    var scrollBot = scrollTop + scroll.clientHeight;
    var swaps = [];  // [{idx, canvas}]
    var renderPromises = [];
    for (var i = 0; i < s.pages.length; i++) {
      var pg = s.pages[i];
      var top = pg.wrap.offsetTop;
      var bot = top + pg.wrap.offsetHeight;
      if (bot < scrollTop - 200 || top > scrollBot + 200) continue;
      (function (idx, dim) {
        renderPromises.push(
          s.doc.getPage(s.pages[idx].pageNum).then(function (page) {
            var vp = page.getViewport({ scale: renderScale });
            var offCanvas = document.createElement('canvas');
            offCanvas.className = 'pdf-canvas pdf-canvas-page';
            offCanvas.width = dim.w;
            offCanvas.height = dim.h;
            // CSS size matches wrap exactly — no layout shift on swap.
            offCanvas.style.width  = dim.cssW;
            offCanvas.style.height = dim.cssH;
            var ctx = offCanvas.getContext('2d', { alpha: false });
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

    // Atomic swap: CSS sizes are already set to match the wraps exactly.
    return new Promise(function (resolve) {
      requestAnimationFrame(function () {
        for (var j = 0; j < swaps.length; j++) {
          var sw = swaps[j];
          var pg = s.pages[sw.idx];
          pg.wrap.replaceChild(sw.canvas, pg.canvas);
          pg.canvas = sw.canvas;
        }
        resolve(targetZoom);
      });
    });
  }

  async function loadPdf(id, url) {
    await destroyPdfTab(id);
    var s = getPdfState(id);
    var pdfjs = await ensurePdfJs();
    var fetchUrl = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'inline=1';
    var loadingTask = pdfjs.getDocument({ url: fetchUrl, withCredentials: true });
    s.loadingTask = loadingTask;
    var doc = await loadingTask.promise;
    s.loadingTask = null;
    s.doc = doc;
    s.zoom = 1;
  }

  function activePdfTabId() {
    if (rankingActive) return 'ranking';
    return resolveTabId(currentSide, currentLayout);
  }

  function clampZoom(z) {
    return Math.min(6, Math.max(0.2, z));
  }

  async function zoomActivePdf(factor) {
    if (!workspace.classList.contains('preview-mode')) return;
    var id = activePdfTabId();
    var s = getPdfState(id);
    if (!s.doc) return;
    s.zoom = clampZoom(s.zoom * factor);
    await renderPdfContinuous(id);
  }

  async function resetActivePdfZoom() {
    var id = activePdfTabId();
    var s = getPdfState(id);
    if (!s.doc) return;
    s.zoom = 1;
    await renderPdfContinuous(id);
  }

  /** Set of tab IDs that have a PDF available for this run. */
  var enabledTabs = new Set();

  function tabEnabled(id) {
    return enabledTabs.has(id);
  }

  function firstEnabledExerciseLayoutTab() {
    var layouts = ['exercise', 'two-up', 'four-up'];
    for (var i = 0; i < layouts.length; i++) {
      if (tabEnabled(layouts[i])) return layouts[i];
    }
    return 'exercise';
  }

  /** Position the side pill via left + right.
   *  Anchored off the inner layout track so the bridge width is exact and
   *  not affected by flex gap or column padding.
   *  The expanding edge (→ 0) animates faster so the pill stretches both ways. */
  function syncSidePill() {
    if (!pdfTabSidePill || !pdfTabPrimaryTrack || !pdfTabLayoutTrack) return;
    var outerRect = pdfTabPrimaryTrack.getBoundingClientRect();
    var innerRect = pdfTabLayoutTrack.getBoundingClientRect();
    var bw = 1;                /* track border-width */
    var bridge = 3;           /* px of coloured pill visible on each side of the inner track */
    var padL = outerRect.left + bw;
    var padR = outerRect.right - bw;
    var ease = 'cubic-bezier(0.16, 1, 0.3, 1)';
    var fast = '0.26s ' + ease;
    var slow = '0.42s ' + ease;
    var color = 'background 0.32s ease, box-shadow 0.32s ease';

    if (currentSide === 'sheet') {
      pdfTabSidePill.style.transition = 'left ' + fast + ', right ' + slow + ', ' + color;
      pdfTabSidePill.style.left = '0';
      pdfTabSidePill.style.right = Math.max(0, padR - (innerRect.right + bridge)) + 'px';
    } else {
      pdfTabSidePill.style.transition = 'left ' + slow + ', right ' + fast + ', ' + color;
      pdfTabSidePill.style.left = Math.max(0, (innerRect.left - bridge) - padL) + 'px';
      pdfTabSidePill.style.right = '0';
    }
    pdfTabSidePill.style.width = '';
  }

  function syncPdfTabChrome(activeId) {
    if (!pdfTabPrimaryTrack || !pdfTabLayoutTrack) return;
    var sl = tabIdToSideLayout(activeId);
    currentSide = sl.side;
    currentLayout = sl.layout;
    pdfTabPrimaryTrack.setAttribute('data-side', currentSide);
    pdfTabLayoutTrack.setAttribute('data-layout', currentLayout);
    syncSidePill();
  }

  function selectTab(id) {
    if (!tabEnabled(id)) return Promise.resolve();
    // Do NOT clear inactive surfaces yet — keep their canvases alive in the DOM
    // so the outgoing tab remains visible until the incoming tab's render completes.

    // Update all tab panels (show only the active one)
    TAB_ORDER.forEach(function (tid) {
      var pan = panelEl(tid);
      if (!pan) return;
      var on = tid === id;
      pan.classList.toggle('is-active', on);
      if (on) pan.removeAttribute('hidden');
      else pan.setAttribute('hidden', '');
    });

    var rankingBtn = document.getElementById('tab-btn-ranking');

    if (id === 'ranking') {
      rankingActive = true;
      if (rankingBtn) {
        rankingBtn.setAttribute('aria-selected', 'true');
        rankingBtn.tabIndex = 0;
      }
      // Deselect all two-axis buttons while ranking is active
      ['exercise', 'two-up', 'four-up'].forEach(function (lid) {
        var btn = tabBtn(lid);
        if (!btn) return;
        btn.setAttribute('aria-selected', 'false');
        btn.tabIndex = -1;
      });
      var ansBtn2 = tabBtn('answers');
      if (ansBtn2) { ansBtn2.setAttribute('aria-selected', 'false'); ansBtn2.tabIndex = -1; }
    } else {
      rankingActive = false;
      if (rankingBtn) {
        rankingBtn.setAttribute('aria-selected', 'false');
        rankingBtn.tabIndex = -1;
      }

      var sl = tabIdToSideLayout(id);
      // Update button states: layout buttons reflect the layout axis,
      // answers button reflects the side axis.
      ['exercise', 'two-up', 'four-up'].forEach(function (lid) {
        var btn = tabBtn(lid);
        if (!btn) return;
        var on = lid === sl.layout;
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
        btn.tabIndex = on ? 0 : -1;
      });
      var ansBtn = tabBtn('answers');
      if (ansBtn) {
        var ansOn = sl.side === 'answers';
        ansBtn.setAttribute('aria-selected', ansOn ? 'true' : 'false');
        ansBtn.tabIndex = ansOn ? 0 : -1;
      }
      syncPdfTabChrome(id);
    }

    var sc = scrollEl(id);
    if (sc) sc.scrollTop = 0;
    updateHeaderGlassFromPdfScroll();
    if (sc && workspace.classList.contains('preview-mode')) {
      try { sc.focus({ preventScroll: true }); } catch (e) { sc.focus(); }
    }
    // Render first, then free RAM from tabs that are no longer visible.
    // This eliminates the dark-background flash: the incoming tab's old canvases
    // (if any) remain visible during the render; the atomic DocumentFragment swap
    // in renderPdfContinuous replaces them without any empty frame in between.
    return renderPdfContinuous(id).then(function () {
      clearInactivePdfSurfaces(id);
    }).catch(function () {});
  }

  function buildOverviewPanel(overview) {
    if (!overviewPanel || !overviewBody) return;
    overviewBody.innerHTML = '';
    if (!overview || !overview.papers || !overview.papers.length) {
      overviewPanel.classList.add('hidden');
      return;
    }
    var hasAny = false;
    overview.papers.forEach(function (paper) {
      if (!paper.exercises || !paper.exercises.length) return;
      hasAny = true;
      var block = document.createElement('div');
      block.className = 'overview-paper-block';
      var plabel = (paper.label && String(paper.label).trim()) ? paper.label : 'This sheet';
      var paperBtn = document.createElement('button');
      paperBtn.type = 'button';
      paperBtn.className = 'overview-paper-btn';
      paperBtn.textContent = plabel;
      var firstEx = paper.exercises[0];
      paperBtn.addEventListener('click', function () {
        scrollPreviewToExercise(firstEx);
      });
      var row = document.createElement('div');
      row.className = 'overview-q-row';
      paper.exercises.forEach(function (ex) {
        var qb = document.createElement('button');
        qb.type = 'button';
        qb.className = 'overview-q-btn' + (ex.mcq ? ' overview-q-btn-mcq' : '');
        qb.textContent = String(ex.q);
        qb.setAttribute('aria-label', 'Go to exercise ' + ex.q);
        qb.addEventListener('click', function () {
          scrollPreviewToExercise(ex);
        });
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

  function resolveNavPageYForTab(ex, tabId) {
    var sl = tabIdToSideLayout(tabId);
    if (sl.side === 'answers') {
      if (ex.answers_page != null && ex.answers_y_pt != null) {
        return { page: ex.answers_page, yPt: ex.answers_y_view_pt != null ? ex.answers_y_view_pt : ex.answers_y_pt };
      }
      return { page: 0, yPt: 0 };
    }
    return { page: ex.page, yPt: ex.y_view_pt != null ? ex.y_view_pt : ex.y_pt };
  }

  function mapSrcPageYToOffsetInCanvas(tabId, srcPage, srcYPt, canvases) {
    var n = canvases.length;
    if (!n) return null;
    var SRC_H = PDF_OUTPUT_PAGE_H_PT;
    var ySrc = Number(srcYPt);
    var layout = tabIdToSideLayout(tabId).layout;
    if (layout === 'exercise') {
      var pg = Math.max(0, Math.min(parseInt(srcPage, 10) || 0, n - 1));
      var c = canvases[pg];
      var ch = c.offsetHeight;
      if (ch <= 0) return null;
      return { pageIdx: pg, yPx: (ySrc / SRC_H) * ch };
    }
    if (layout === 'four-up') {
      var p = parseInt(srcPage, 10) || 0;
      var per = 4;
      var outIdx = Math.max(0, Math.min(Math.floor(p / per), n - 1));
      var slot = ((p % per) + per) % per;
      var row = Math.floor(slot / 2);
      var c4 = canvases[outIdx];
      var H = c4.offsetHeight;
      if (H <= 0) return null;
      var cellH = H / 2;
      return { pageIdx: outIdx, yPx: row * cellH + (ySrc / SRC_H) * cellH };
    }
    if (layout === 'two-up') {
      var p2 = parseInt(srcPage, 10) || 0;
      var per2 = 2;
      var outIdx2 = Math.max(0, Math.min(Math.floor(p2 / per2), n - 1));
      var c2 = canvases[outIdx2];
      var H2 = c2.offsetHeight;
      if (H2 <= 0) return null;
      return { pageIdx: outIdx2, yPx: (ySrc / SRC_H) * H2 };
    }
    return null;
  }

  function stickyHeaderHeight() {
    var hdr = document.querySelector('.site-header');
    return hdr ? hdr.getBoundingClientRect().height : 0;
  }

  function scrollPreviewToExercise(ex) {
    if (!workspace.classList.contains('preview-mode')) return;
    var tabId = activePdfTabId();
    if (!tabEnabled(tabId)) tabId = firstEnabledTab();
    void selectTab(tabId).then(function () {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          var sc = scrollEl(tabId);
          var stack = pagesStackEl(tabId);
          if (!sc || !stack || sc.classList.contains('hidden')) return;
          var canvases = stack.querySelectorAll('canvas.pdf-canvas-page');
          var nav = resolveNavPageYForTab(ex, tabId);
          var mapped = mapSrcPageYToOffsetInCanvas(tabId, nav.page, nav.yPt, canvases);
          if (!mapped) return;
          var c = canvases[mapped.pageIdx];
          var scRect = sc.getBoundingClientRect();
          var cRect = c.getBoundingClientRect();
          var topInScrollContent = cRect.top - scRect.top + sc.scrollTop;
          var targetY = topInScrollContent + mapped.yPx;
          var headerH = stickyHeaderHeight();
          sc.scrollTop = Math.max(0, targetY - headerH - 8);
          updateHeaderGlassFromPdfScroll();
          try { sc.focus({ preventScroll: true }); } catch (e2) { sc.focus(); }
        });
      });
    });
  }

  function firstEnabledTab() {
    for (var i = 0; i < TAB_ORDER.length; i++) {
      if (tabEnabled(TAB_ORDER[i])) return TAB_ORDER[i];
    }
    return 'exercise';
  }

  function focusAdjacentTab(fromLayoutId, delta) {
    /* Navigate among the 3 layout buttons + answers, wrapping with delta */
    var btnOrder = ['exercise', 'two-up', 'four-up', 'answers'];
    var idx = btnOrder.indexOf(fromLayoutId);
    if (idx < 0) return;
    for (var step = 1; step <= btnOrder.length; step++) {
      var j = (idx + delta * step + btnOrder.length * 4) % btnOrder.length;
      var bid = btnOrder[j];
      if (bid === 'answers') {
        var target = resolveTabId('answers', currentLayout);
        if (tabEnabled(target) || tabEnabled('answers')) {
          selectTab(tabEnabled(target) ? target : 'answers');
          tabBtn('answers').focus();
          return;
        }
      } else {
        var target = resolveTabId(currentSide, bid);
        if (tabEnabled(target)) {
          selectTab(target);
          tabBtn(bid).focus();
          return;
        }
      }
    }
  }

  function setSubmitPreviewMode(on) {
    if (!submitLabel) return;
    submitLabel.textContent = on ? 'Update preview' : 'Generate sheet';
    if (submitIconGen) submitIconGen.classList.toggle('hidden', !!on);
    if (submitIconUpd) submitIconUpd.classList.toggle('hidden', !on);
  }

  function applyDoneData(done) {
    dlMain.href = done.download_url || '#';
    lastDownloadAllUrls = [
      done.download_url, done.answers_url, done.two_up_url,
      done.four_up_url, done.answers_two_up_url, done.answers_four_up_url,
    ].filter(Boolean);
    if (dlAll) dlAll.href = '#';
    if (done.answers_url) {
      dlAnswersMain.href = done.answers_url;
      dlAnswers.classList.remove('hidden');
    } else { dlAnswers.classList.add('hidden'); }
    if (done.answers_two_up_url) {
      dlAnswersTwoUp.href = done.answers_two_up_url;
      dlAnswersTwoUp.classList.remove('hidden');
    } else { dlAnswersTwoUp.classList.add('hidden'); }
    if (done.answers_four_up_url) {
      dlAnswersFourUp.href = done.answers_four_up_url;
      dlAnswersFourUp.classList.remove('hidden');
    } else { dlAnswersFourUp.classList.add('hidden'); }
    if (done.four_up_url) {
      dlFourUp.href = done.four_up_url;
      dlFourUp.classList.remove('hidden');
    } else { dlFourUp.classList.add('hidden'); }
    if (done.two_up_url) {
      dlTwoUp.href = done.two_up_url;
      dlTwoUp.classList.remove('hidden');
    } else { dlTwoUp.classList.add('hidden'); }
  }

  function exitPreviewMode() {
    rankingActive = false;
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

  async function enterPreviewMode(doneData, instant) {
    var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var fadeDuration = (instant || reducedMotion) ? 0 : 20;

    /* Phase 1: fade out the landing page */
    workspace.style.transition = 'opacity ' + fadeDuration + 'ms ease';
    workspace.style.opacity = '0';
    await new Promise(function (r) { setTimeout(r, fadeDuration); });

    /* Phase 2: switch layout while invisible */
    workspace.classList.add('preview-mode');
    document.body.classList.add('preview-mode-active');
    if (pdfTabBarWrap) pdfTabBarWrap.removeAttribute('hidden');
    pdfPane.removeAttribute('aria-hidden');

    /* Start loading PDFs */
    enabledTabs.clear();
    var loadPromises = [];
    TABS.forEach(function (tab) {
      var url = doneData[tab.urlKey];
      var btn = tabBtn(tab.id);  // null for answers-two-up / answers-four-up
      hideSpinner(tab.id);
      if (url) {
        enabledTabs.add(tab.id);
        if (btn) {
          btn.disabled = false;
          btn.removeAttribute('aria-disabled');
          btn.title = '';
        }
        hideEmpty(tab.id);
        loadPromises.push(
          loadPdf(tab.id, url)
            .catch(function (err) {
              console.error(err);
              showEmpty(tab.id);
              enabledTabs.delete(tab.id);
            })
        );
      } else {
        if (btn) {
          btn.disabled = true;
          btn.setAttribute('aria-disabled', 'true');
          btn.title = 'Not generated for this run';
        }
        hideSpinner(tab.id);
        showEmpty(tab.id);
      }
    });
    await Promise.all(loadPromises);
    await selectTab(firstEnabledTab());

    setSubmitPreviewMode(true);
    buildOverviewPanel(doneData.overview || { papers: [] });

    /* Let the browser lay out the new grid before fading in */
    await new Promise(function (resolve) {
      requestAnimationFrame(function () { requestAnimationFrame(resolve); });
    });

    /* Phase 3: fade in the preview page */
    workspace.classList.add('preview-mode--settled');
    workspace.style.opacity = '1';

    ensurePdfScrollGlassListeners();
    bindPdfSmoothWheelScroll();

    /* Re-render PDF at final container size once layout is stable.
       We wait for the fade-in to complete AND verify the scroll
       container has a non-trivial height before rendering. */
    function rerenderWhenReady(attempts) {
      if (attempts > 20) return; // give up after ~1s
      var id = activePdfTabId();
      var sc = scrollEl(id);
      if (sc && sc.clientHeight > 100) {
        void rerenderActivePdfTab().then(function () {
          updateHeaderGlassFromPdfScroll();
        });
      } else {
        setTimeout(function () { rerenderWhenReady(attempts + 1); }, 50);
      }
    }
    setTimeout(function () { rerenderWhenReady(0); }, fadeDuration + 50);
  }

  async function refreshPreviewMode() {
    rankingActive = false;
    if (overviewPanel) {
      overviewPanel.classList.add('hidden');
      if (overviewBody) overviewBody.innerHTML = '';
    }
    enabledTabs.clear();
    await Promise.all(TABS.map(function (tab) { return destroyPdfTab(tab.id); }));
    TABS.forEach(function (tab) {
      var btn = tabBtn(tab.id);
      var sc = scrollEl(tab.id);
      if (sc) sc.classList.remove('hidden');
      var emp = emptyEl(tab.id);
      if (emp) emp.classList.add('hidden');
      showSpinner(tab.id);
      if (btn) {
        btn.disabled = true;
        btn.setAttribute('aria-disabled', 'true');
      }
    });
  }

  function hideAllPdfSpinners() {
    TABS.forEach(function (tab) { hideSpinner(tab.id); });
  }

  function parseFilenameFromContentDisposition(header) {
    if (!header) return 'download.pdf';
    var m = header.match(/filename\*=UTF-8''([^;]+)|filename="([^"]*)"|filename=([^;\s]+)/i);
    if (!m) return 'download.pdf';
    var raw = (m[1] || m[2] || m[3] || '').trim();
    try {
      return decodeURIComponent(raw.replace(/\+/g, ' '));
    } catch (e) {
      return raw || 'download.pdf';
    }
  }

  async function triggerDownloadAllPdfs() {
    var urls = lastDownloadAllUrls;
    if (!urls.length) return;
    for (var i = 0; i < urls.length; i++) {
      var res = await fetch(urls[i], { credentials: 'same-origin', cache: 'no-store' });
      if (!res.ok) throw new Error('Could not download file (' + res.status + ').');
      var blob = await res.blob();
      var name = parseFilenameFromContentDisposition(res.headers.get('Content-Disposition'));
      var u = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = u;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(u);
      if (i < urls.length - 1) await sleep(250);
    }
  }

  if (dlAll) {
    dlAll.addEventListener('click', function (e) {
      e.preventDefault();
      triggerDownloadAllPdfs().catch(function (err) {
        errorPanel.textContent = err.message || String(err);
        errorPanel.classList.remove('hidden');
      });
    });
  }

  if (pdfPane) {
    /* Smooth pinch-to-zoom: instantly resizes page wraps and stretches canvases
       to give immediate layout-correct feedback (scroll positions stay natural).
       A background double-buffered render swaps in crisp canvases, and when the
       gesture settles a final full-quality render with text layers runs. */
    var _zBgTimer = null;         // background quality refresh schedule
    var _zBgRendering = false;    // true while an offscreen render is in flight
    var _zBgPending = false;      // zoom changed while a render was in flight
    var _zSettleTimer = null;     // debounce for final full render
    function _scheduleBgRender(id) {
      if (_zBgRendering) { _zBgPending = true; return; }
      if (_zBgTimer) clearTimeout(_zBgTimer);
      _zBgTimer = setTimeout(function () {
        _zBgTimer = null;
        _zBgRendering = true;
        _zBgPending = false;
        rerenderPdfZoomBuffered(id).then(function (renderedAt) {
          _zBgRendering = false;
          _zBaseZoom = renderedAt;
          if (_zBgPending) { _zBgPending = false; _scheduleBgRender(id); }
        }).catch(function () { _zBgRendering = false; });
      }, 16);
    }
    pdfPane.addEventListener('wheel', function (e) {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      var id = activePdfTabId();
      var s = getPdfState(id);
      if (!s.doc || !s.pages.length || !s.pages[0].vpW) return;
      var scroll = scrollEl(id);
      var stack = pagesStackEl(id);
      if (!scroll || !stack) return;
      var dy = e.deltaY;
      if (e.deltaMode === 1) dy *= 16;
      var oldZoom = s.zoom;
      s.zoom = clampZoom(s.zoom * Math.pow(2, -dy / 100));
      var ratio = s.zoom / oldZoom;
      if (Math.abs(ratio - 1) < 1e-6) return;

      // --- Read phase (single reflow) ---
      var rect = scroll.getBoundingClientRect();
      var cursorVpX = e.clientX - rect.left;
      var cursorVpY = e.clientY - rect.top;
      var oldScrollL = scroll.scrollLeft;
      var oldScrollT = scroll.scrollTop;
      var cs = getComputedStyle(scroll);
      var contentW = scroll.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);

      // --- Compute phase (no DOM access) ---
      var bf = s.baseFit;
      var oldMaxW = 0, newMaxW = 0;
      var newDims = [];
      for (var i = 0; i < s.pages.length; i++) {
        var pg = s.pages[i];
        var ow = Math.floor(pg.vpW * bf * oldZoom);
        if (ow > oldMaxW) oldMaxW = ow;
        var nw = Math.floor(pg.vpW * bf * s.zoom);
        var nh = Math.floor(pg.vpH * bf * s.zoom);
        newDims.push({ w: nw, h: nh });
        if (nw > newMaxW) newMaxW = nw;
      }
      var oldStackW = Math.max(contentW, oldMaxW);
      var newStackW = Math.max(contentW, newMaxW);
      // Centering gap: pages are centered in stack via align-items:center
      var oldGap = Math.max(0, (oldStackW - oldMaxW) / 2);
      var newGap = Math.max(0, (newStackW - newMaxW) / 2);
      // Focal point in page-content coords (strip centering offset)
      var focalX = oldScrollL + cursorVpX - oldGap;
      var focalY = oldScrollT + cursorVpY;

      // --- Write phase (no layout reads, batch all writes) ---
      for (var i = 0; i < s.pages.length; i++) {
        var pg = s.pages[i], d = newDims[i];
        pg.wrap.style.width = d.w + 'px';
        pg.wrap.style.height = d.h + 'px';
        pg.canvas.style.width = d.w + 'px';
        pg.canvas.style.height = d.h + 'px';
      }
      stack.style.width = newStackW + 'px';
      // Restore scroll: map focal point through new centering gap
      scroll.scrollLeft = focalX * ratio + newGap - cursorVpX;
      scroll.scrollTop  = focalY * ratio - cursorVpY;
      // Fire a background double-buffered render every ~16 ms during the gesture.
      _scheduleBgRender(id);
      // Debounce full hi-dpi + text layer render for when gesture ends.
      if (_zSettleTimer) clearTimeout(_zSettleTimer);
      _zSettleTimer = setTimeout(function () {
        _zSettleTimer = null;
        _zBaseZoom = s.zoom;
        renderPdfContinuous(id, true);  // reuseBaseFit: keep page sizes stable
      }, 50);
    }, { passive: false });
  }

  window.addEventListener('keydown', function (e) {
    if (!workspace.classList.contains('preview-mode')) return;
    var el = e.target;
    if (el && el.closest && (el.closest('textarea') || el.closest('input') || el.closest('[contenteditable="true"]'))) return;
    if (!e.ctrlKey && !e.metaKey) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        var sc = scrollEl(activePdfTabId());
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
        var id = activePdfTabId();
        if (tabEnabled(id) && getPdfState(id).doc) {
          void renderPdfContinuous(id).then(function () {
            updateHeaderGlassFromPdfScroll();
          });
        }
      }, 100);
    });
    pdfResizeObserver.observe(pdfPane);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    if (!pdfLayoutStaleWhileHidden) return;
    pdfLayoutStaleWhileHidden = false;
    if (!workspace.classList.contains('preview-mode')) return;
    var id = activePdfTabId();
    if (tabEnabled(id) && getPdfState(id).doc) {
      void renderPdfContinuous(id).then(function () {
        updateHeaderGlassFromPdfScroll();
      });
    }
  });

  /* Layout buttons: switch layout, keep current side */
  ['exercise', 'two-up', 'four-up'].forEach(function (layoutId) {
    var b = tabBtn(layoutId);
    if (!b) return;
    b.addEventListener('click', function () {
      if (b.disabled) return;
      var target = resolveTabId(currentSide, layoutId);
      if (!tabEnabled(target)) target = layoutId; // fall back to sheet side
      selectTab(target);
    });
    b.addEventListener('keydown', function (e) {
      if (b.disabled) return;
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        focusAdjacentTab(layoutId, e.key === 'ArrowRight' ? 1 : -1);
      } else if (e.key === 'Home') {
        e.preventDefault();
        var first = firstEnabledTab();
        selectTab(first);
        tabBtn(tabIdToSideLayout(first).layout).focus();
      } else if (e.key === 'End') {
        e.preventDefault();
        var ansBtn = tabBtn('answers');
        if (ansBtn && !ansBtn.disabled) {
          var target = resolveTabId('answers', currentLayout);
          if (tabEnabled(target)) { selectTab(target); ansBtn.focus(); return; }
        }
        for (var i = 2; i >= 0; i--) {
          var lid = ['exercise', 'two-up', 'four-up'][i];
          var tid = resolveTabId(currentSide, lid);
          if (tabEnabled(tid)) { selectTab(tid); tabBtn(lid).focus(); return; }
        }
      }
    });
  });

  /* Answers button: switch to answers side, keep current layout */
  (function () {
    var b = tabBtn('answers');
    if (!b) return;
    b.addEventListener('click', function () {
      if (b.disabled) return;
      var target = resolveTabId('answers', currentLayout);
      if (!tabEnabled(target)) target = 'answers';
      selectTab(target);
    });
    b.addEventListener('keydown', function (e) {
      if (b.disabled) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        var back = resolveTabId('sheet', currentLayout);
        if (tabEnabled(back)) { selectTab(back); tabBtn(currentLayout).focus(); }
      } else if (e.key === 'Home') {
        e.preventDefault();
        var first = firstEnabledTab();
        selectTab(first);
        var sl = tabIdToSideLayout(first);
        tabBtn(sl.side === 'answers' ? 'answers' : sl.layout).focus();
      }
    });
  })();

  /* Ranking button: standalone tab, outside the two-axis system */
  (function () {
    var b = document.getElementById('tab-btn-ranking');
    if (!b) return;
    b.addEventListener('click', function () {
      if (b.disabled) return;
      selectTab('ranking');
      b.blur();
    });
  })();

  if (pdfTabSheetColumn) {
    pdfTabSheetColumn.addEventListener('click', function (e) {
      if (e.target.closest('.pdf-tab-layout-btn')) return;
      /* Switch to sheet side, keeping current layout */
      var target = resolveTabId('sheet', currentLayout);
      if (!tabEnabled(target)) target = firstEnabledExerciseLayoutTab();
      void selectTab(target);
      var nb = tabBtn(tabIdToSideLayout(target).layout);
      if (nb) try { nb.focus(); } catch (e2) {}
    });
  }

  function resultPanelIsVisible() {
    return resultPanel && !resultPanel.classList.contains('hidden');
  }

  if (promptEl && form) {
    promptEl.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      if (e.shiftKey) return;
      e.preventDefault();
      if (submitBtn && submitBtn.disabled) return;
      if (resultPanelIsVisible() && lastDownloadAllUrls.length) {
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

  document.querySelectorAll('.quick-fill').forEach(function (btn) {
    btn.addEventListener('click', function () {
      promptEl.value = btn.getAttribute('data-prompt');
      promptEl.focus();
    });
  });

  function isTextEntryElement(el) {
    if (!el || el.nodeType !== 1) return false;
    var tag = el.tagName;
    if (tag === 'TEXTAREA') return true;
    if (tag === 'SELECT') return true;
    if (tag === 'INPUT') {
      var type = (el.type || '').toLowerCase();
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

  if (pdfTabBackBtn) {
    pdfTabBackBtn.addEventListener('click', function () {
      exitPreviewMode();
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Backspace') return;
    if (!document.body.classList.contains('preview-mode-active')) return;
    if (isTextEntryElement(e.target)) return;
    e.preventDefault();
    exitPreviewMode();
  });

  /* Restore preview state after a page reload */
  (function () {
    var savedStr = sessionStorage.getItem('previewState');
    if (!savedStr) return;
    var saved;
    try { saved = JSON.parse(savedStr); } catch (e) { sessionStorage.removeItem('previewState'); return; }
    if (!saved || !saved.doneData || !saved.prompt) { sessionStorage.removeItem('previewState'); return; }
    promptEl.value = saved.prompt;
    applyDoneData(saved.doneData);
    showResultPanel();
    enterPreviewMode(saved.doneData, true).catch(function () {});
  })();

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function applyLogLine(data) {
    if (!jobLogLine) return;
    let line = (data.log_line != null && data.log_line !== '') ? String(data.log_line) : '';
    if (!line) {
      if      (data.status === 'pending') line = 'Queued for processing\u2026';
      else if (data.status === 'running') line = 'Starting\u2026';
      else                                line = 'Working\u2026';
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
    if (!res.ok) throw new Error('Could not load job status.');
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

  async function pollJob(id, onTick) {
    while (true) {
      const data = await fetchJobStatus(id);
      if (onTick) onTick(data);
      if (data.status === 'failed') throw new Error(data.error || 'Generation failed.');
      if (data.status === 'done')   return data;
      await sleep(50);
    }
  }

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    const prompt = promptEl.value.trim();
    if (!prompt) return;

    errorPanel.classList.add('hidden');
    hideResultPanel();
    lastDownloadAllUrls = [];
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
      if (!id) throw new Error('No job id returned.');

      applyLogLine(await fetchJobStatus(id));
      const done = await pollJob(id, applyLogLine);

      applyDoneData(done);
      try {
        sessionStorage.setItem('previewState', JSON.stringify({ doneData: done, prompt: prompt }));
      } catch (e) {}
      showResultPanel();
      await enterPreviewMode(done);
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
})();
