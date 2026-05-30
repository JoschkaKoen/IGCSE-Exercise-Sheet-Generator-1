/**
 * state.js — shared state; no imports.
 * Every other module imports from here. Nothing imports from workspace.js.
 *
 * Adding a new PDF tab requires:
 *   1. state.js              — add entry to TABS array and TAB_ORDER
 *   2. pdf_tab_bar.html      — add <button id="tab-btn-{id}">
 *   3. workspace_pdf_panels.html — clone panel from #pdf-panel-template
 *   4. workspace_left.html   — add download card if the tab has a download
 *   5. 08-workspace-pdf-preview.css — add :has() tint rule if tab needs a border colour
 *   6. applyDoneData() in downloads.js — wire the download link
 */

export const TAB_ORDER = [
  'exercise', 'two-up', 'four-up', 'answers', 'answers-two-up', 'answers-four-up', 'ranking',
];

export const PDF_OUTPUT_PAGE_H_PT = 842;

export const TABS = [
  { id: 'exercise',        urlKey: 'download_url' },
  { id: 'answers',         urlKey: 'answers_url' },
  { id: 'two-up',          urlKey: 'two_up_url' },
  { id: 'four-up',         urlKey: 'four_up_url' },
  { id: 'answers-two-up',  urlKey: 'answers_two_up_url' },
  { id: 'answers-four-up', urlKey: 'answers_four_up_url' },
  { id: 'ranking',         urlKey: 'ranking_url' },
];

/**
 * Mutable shared state object. All modules read/write properties here.
 * Using a single object avoids the ES-module live-binding restriction
 * (exported `let` cannot be reassigned by importers).
 */
export const state = {
  /** Two-axis display: which side × which layout. */
  currentSide: 'sheet',     // 'sheet' | 'answers'
  currentLayout: 'exercise', // 'exercise' | 'two-up' | 'four-up'
  /** True while the ranking tab is in the foreground. */
  rankingActive: false,
  /** Set of tab IDs that have a PDF available for this run. */
  enabledTabs: new Set(),
  /** Per-tab PDF.js document + rendering state, keyed by tab id. */
  pdfTabState: {},
  /** URLs last passed to "download all". */
  lastDownloadAllUrls: [],
  /** Job ID of the most recent run, used for on-demand ranking start. */
  currentJobId: null,
  /** Zoom level at which canvases are currently rendered (used by pinch-zoom). */
  _zBaseZoom: 1,
};

// ─── Pure utility functions ──────────────────────────────────────────────────

/** Map (side, layout) → tab id. */
export function resolveTabId(side, layout) {
  if (side === 'answers') {
    if (layout === 'two-up')  return 'answers-two-up';
    if (layout === 'four-up') return 'answers-four-up';
    return 'answers';
  }
  return layout; // 'exercise' | 'two-up' | 'four-up'
}

/** Inverse: tab id → { side, layout }. */
export function tabIdToSideLayout(id) {
  if (id === 'answers')         return { side: 'answers', layout: 'exercise' };
  if (id === 'answers-two-up')  return { side: 'answers', layout: 'two-up' };
  if (id === 'answers-four-up') return { side: 'answers', layout: 'four-up' };
  if (id === 'two-up')          return { side: 'sheet',   layout: 'two-up' };
  if (id === 'four-up')         return { side: 'sheet',   layout: 'four-up' };
  return { side: 'sheet', layout: 'exercise' };
}

export function activePdfTabId() {
  if (state.rankingActive) return 'ranking';
  return resolveTabId(state.currentSide, state.currentLayout);
}

export function clampZoom(z) {
  return Math.min(6, Math.max(0.2, z));
}

export function tabEnabled(id) {
  return state.enabledTabs.has(id);
}

export function firstEnabledExerciseLayoutTab() {
  const layouts = ['exercise', 'two-up', 'four-up'];
  for (const l of layouts) {
    if (tabEnabled(l)) return l;
  }
  return 'exercise';
}

export function firstEnabledTab() {
  for (const id of TAB_ORDER) {
    if (tabEnabled(id)) return id;
  }
  return 'exercise';
}

/**
 * Lazily initialise per-tab state; returns existing state if already present.
 * This lives in state.js so pdf-render.js can import it without a cycle.
 */
export function getPdfState(id) {
  if (!state.pdfTabState[id]) {
    state.pdfTabState[id] = {
      doc: null, zoom: 1, loadingTask: null,
      fitBox: null, dpr: 1,
      pages: [],   // [{wrap, canvas, pageNum, vpW, vpH, rendered, rendering, canvasRendered}]
      observer: null,
    };
  }
  return state.pdfTabState[id];
}

export function sleep(ms) {
  return new Promise(function (r) { setTimeout(r, ms); });
}

// ─── DOM helpers (getElementById lookups — always fresh) ─────────────────────

export const scrollEl    = id => document.getElementById('pdf-scroll-'  + id);
export const pagesStackEl = id => document.getElementById('pdf-pages-'  + id);
export const loadingEl   = id => document.getElementById('pdf-loading-' + id);
export const emptyEl     = id => document.getElementById('pdf-empty-'   + id);
export const tabBtn      = id => document.getElementById('tab-btn-'     + id);
export const panelEl     = id => document.getElementById('tab-panel-'   + id);
