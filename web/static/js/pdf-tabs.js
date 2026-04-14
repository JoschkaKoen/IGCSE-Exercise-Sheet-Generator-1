/**
 * pdf-tabs.js — tab-bar state machine and pill animation.
 * Imports: state.js, pdf-render.js.
 * Wires all tab-bar button events at module load time.
 */

import {
  state, TAB_ORDER,
  tabEnabled, activePdfTabId, resolveTabId, tabIdToSideLayout,
  firstEnabledExerciseLayoutTab, firstEnabledTab,
  scrollEl, tabBtn, panelEl,
} from './state.js';

import {
  renderPdfContinuous,
  clearInactivePdfSurfaces,
  updateHeaderGlassFromPdfScroll,
} from './pdf-render.js';

import { startAndPollRanking } from './downloads.js';

// ─── DOM refs private to this module ─────────────────────────────────────────

const pdfTabPrimaryTrack = document.getElementById('pdf-tab-primary-track');
const pdfTabLayoutTrack  = document.getElementById('pdf-tab-layout-track');
const pdfTabSheetColumn  = pdfTabPrimaryTrack
  ? pdfTabPrimaryTrack.querySelector('.pdf-tab-sheet-column') : null;
const pdfTabSidePill = document.getElementById('pdf-tab-side-pill');

// ─── Pill animation ──────────────────────────────────────────────────────────

/**
 * Measure the pill insets from getBoundingClientRect() and write them as CSS
 * custom properties. CSS owns left/right/transition via [data-side] rules.
 */
export function syncSidePill() {
  if (!pdfTabSidePill || !pdfTabPrimaryTrack || !pdfTabLayoutTrack) return;
  const outerRect = pdfTabPrimaryTrack.getBoundingClientRect();
  const innerRect = pdfTabLayoutTrack.getBoundingClientRect();
  const bw = 1;     // track border-width (px)
  const bridge = 3; // px of coloured pill visible on each side of the inner track
  const padL = outerRect.left + bw;
  const padR = outerRect.right - bw;
  pdfTabSidePill.style.setProperty('--pill-right-inset', Math.max(0, padR - (innerRect.right + bridge)) + 'px');
  pdfTabSidePill.style.setProperty('--pill-left-inset',  Math.max(0, (innerRect.left - bridge) - padL) + 'px');
}

// ─── Tab chrome sync ──────────────────────────────────────────────────────────

export function syncPdfTabChrome(activeId) {
  if (!pdfTabPrimaryTrack || !pdfTabLayoutTrack) return;
  const sl = tabIdToSideLayout(activeId);
  state.currentSide   = sl.side;
  state.currentLayout = sl.layout;
  pdfTabPrimaryTrack.setAttribute('data-side',   state.currentSide);
  pdfTabLayoutTrack.setAttribute('data-layout',  state.currentLayout);
  syncSidePill();
}

// ─── Tab selection ────────────────────────────────────────────────────────────

export function selectTab(id) {
  if (!tabEnabled(id)) return Promise.resolve();
  // Do NOT clear inactive surfaces yet — keep their canvases alive in the DOM
  // so the outgoing tab remains visible until the incoming tab's render completes.

  TAB_ORDER.forEach(function (tid) {
    const pan = panelEl(tid);
    if (!pan) return;
    const on = tid === id;
    pan.classList.toggle('is-active', on);
    if (on) pan.removeAttribute('hidden');
    else pan.setAttribute('hidden', '');
  });

  const rankingBtn = document.getElementById('tab-btn-ranking');

  if (id === 'ranking') {
    state.rankingActive = true;
    if (rankingBtn) {
      rankingBtn.setAttribute('aria-selected', 'true');
      rankingBtn.tabIndex = 0;
    }
    ['exercise', 'two-up', 'four-up'].forEach(function (lid) {
      const btn = tabBtn(lid);
      if (!btn) return;
      btn.setAttribute('aria-selected', 'false');
      btn.tabIndex = -1;
    });
    const ansBtn2 = tabBtn('answers');
    if (ansBtn2) { ansBtn2.setAttribute('aria-selected', 'false'); ansBtn2.tabIndex = -1; }
  } else {
    state.rankingActive = false;
    if (rankingBtn) {
      rankingBtn.setAttribute('aria-selected', 'false');
      rankingBtn.tabIndex = -1;
    }
    const sl = tabIdToSideLayout(id);
    ['exercise', 'two-up', 'four-up'].forEach(function (lid) {
      const btn = tabBtn(lid);
      if (!btn) return;
      const on = lid === sl.layout;
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
      btn.tabIndex = on ? 0 : -1;
    });
    const ansBtn = tabBtn('answers');
    if (ansBtn) {
      const ansOn = sl.side === 'answers';
      ansBtn.setAttribute('aria-selected', ansOn ? 'true' : 'false');
      ansBtn.tabIndex = ansOn ? 0 : -1;
    }
    syncPdfTabChrome(id);
  }

  const sc = scrollEl(id);
  if (sc) sc.scrollTop = 0;
  updateHeaderGlassFromPdfScroll();
  if (sc && document.body.classList.contains('preview-mode-active')) {
    try { sc.focus({ preventScroll: true }); } catch (e) { sc.focus(); }
  }
  // Render first, then free RAM from tabs that are no longer visible.
  return renderPdfContinuous(id).then(function () {
    clearInactivePdfSurfaces(id);
  }).catch(function () {});
}

// ─── Keyboard navigation among tab buttons ───────────────────────────────────

export function focusAdjacentTab(fromLayoutId, delta) {
  const btnOrder = ['exercise', 'two-up', 'four-up', 'answers'];
  const idx = btnOrder.indexOf(fromLayoutId);
  if (idx < 0) return;
  for (let step = 1; step <= btnOrder.length; step++) {
    const j = (idx + delta * step + btnOrder.length * 4) % btnOrder.length;
    const bid = btnOrder[j];
    if (bid === 'answers') {
      const target = resolveTabId('answers', state.currentLayout);
      if (tabEnabled(target) || tabEnabled('answers')) {
        selectTab(tabEnabled(target) ? target : 'answers');
        tabBtn('answers').focus();
        return;
      }
    } else {
      const target = resolveTabId(state.currentSide, bid);
      if (tabEnabled(target)) {
        selectTab(target);
        tabBtn(bid).focus();
        return;
      }
    }
  }
}

// ─── Event wiring (runs immediately at module load) ──────────────────────────

/* Layout buttons: switch layout, keep current side */
['exercise', 'two-up', 'four-up'].forEach(function (layoutId) {
  const b = tabBtn(layoutId);
  if (!b) return;
  b.addEventListener('click', function () {
    if (b.disabled) return;
    let target = resolveTabId(state.currentSide, layoutId);
    if (!tabEnabled(target)) target = layoutId;
    selectTab(target);
  });
  b.addEventListener('keydown', function (e) {
    if (b.disabled) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      focusAdjacentTab(layoutId, e.key === 'ArrowRight' ? 1 : -1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      const first = firstEnabledTab();
      selectTab(first);
      const sl = tabIdToSideLayout(first);
      tabBtn(sl.side === 'answers' ? 'answers' : sl.layout).focus();
    } else if (e.key === 'End') {
      e.preventDefault();
      const answersTarget = resolveTabId('answers', state.currentLayout);
      const tid = tabEnabled(answersTarget) ? answersTarget
                : tabEnabled('answers')     ? 'answers'
                : null;
      if (tid) { selectTab(tid); tabBtn('answers').focus(); }
    }
  });
});

/* Answers button: switch to answers side, keep current layout */
(function () {
  const b = tabBtn('answers');
  if (!b) return;
  b.addEventListener('click', function () {
    if (b.disabled) return;
    let target = resolveTabId('answers', state.currentLayout);
    if (!tabEnabled(target)) target = 'answers';
    selectTab(target);
  });
  b.addEventListener('keydown', function (e) {
    if (b.disabled) return;
    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const back = resolveTabId('sheet', state.currentLayout);
      if (tabEnabled(back)) { selectTab(back); tabBtn(state.currentLayout).focus(); }
    } else if (e.key === 'Home') {
      e.preventDefault();
      const first = firstEnabledTab();
      selectTab(first);
      const sl = tabIdToSideLayout(first);
      tabBtn(sl.side === 'answers' ? 'answers' : sl.layout).focus();
    }
  });
})();

/* Ranking button: standalone tab, outside the two-axis system */
(function () {
  const b = document.getElementById('tab-btn-ranking');
  if (!b) return;
  b.addEventListener('click', function () {
    if (b.disabled) return;
    if (tabEnabled('ranking')) {
      // Ranking PDF is ready — switch to tab.
      selectTab('ranking');
    } else if (!b.classList.contains('pdf-tab-ranking-btn--generating')) {
      // Idle — start ranking on demand.
      startAndPollRanking(state.currentJobId);
    }
    // If generating, ignore the click (spinner shows progress).
    b.blur();
  });
})();

/* Sheet column click: switch to sheet side, keeping current layout */
if (pdfTabSheetColumn) {
  pdfTabSheetColumn.addEventListener('click', function (e) {
    if (e.target.closest('.pdf-tab-layout-btn')) return;
    let target = resolveTabId('sheet', state.currentLayout);
    if (!tabEnabled(target)) target = firstEnabledExerciseLayoutTab();
    void selectTab(target);
    const nb = tabBtn(tabIdToSideLayout(target).layout);
    if (nb) try { nb.focus(); } catch (e2) {}
  });
}
