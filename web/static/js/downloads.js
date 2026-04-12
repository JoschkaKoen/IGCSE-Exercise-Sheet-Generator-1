/**
 * downloads.js — download card management and "download all" logic.
 * Imports: state.js only.
 * Wires the "Download all" button event at module load time.
 */

import { state, sleep } from './state.js';
import { loadPdf } from './pdf-render.js';

// ─── DOM refs private to this module ─────────────────────────────────────────

const dlMain        = document.getElementById('dl-main');
const dlAnswers     = document.getElementById('dl-answers');
const dlAnswersMain = document.getElementById('dl-answers-main');
const dlAnswersTwoUp   = document.getElementById('dl-answers-two-up');
const dlAnswersFourUp  = document.getElementById('dl-answers-four-up');
const dlFourUp      = document.getElementById('dl-four-up');
const dlTwoUp       = document.getElementById('dl-two-up');
const dlAll         = document.getElementById('dl-all');
const dlRanking          = document.getElementById('dl-ranking');
const rankingIconChart   = document.getElementById('ranking-icon-chart');
const rankingGenSpinner  = document.getElementById('ranking-gen-spinner');
const rankingLog         = document.getElementById('ranking-log');
const rankingTabIconChart  = document.getElementById('ranking-tab-icon-chart');
const rankingTabGenSpinner = document.getElementById('ranking-tab-gen-spinner');
const tabBtnRanking        = document.getElementById('tab-btn-ranking');

// ─── Download card population ─────────────────────────────────────────────────

export function applyDoneData(done) {
  dlMain.href = done.download_url || '#';
  state.lastDownloadAllUrls = [
    done.download_url, done.answers_url, done.two_up_url,
    done.four_up_url, done.answers_two_up_url, done.answers_four_up_url,
    done.ranking_url,
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
  if (dlRanking) {
    if (done.ranking_url) {
      dlRanking.href = done.ranking_url;
      dlRanking.classList.remove('hidden');
      dlRanking.classList.remove('dl-card--ranking-generating');
      if (rankingIconChart) rankingIconChart.classList.remove('hidden');
      if (rankingGenSpinner) rankingGenSpinner.classList.add('hidden');
      if (rankingLog) rankingLog.classList.add('hidden');
    } else {
      // ranking_url absent: either deferred (pollRankingReady will handle it) or skipped
      dlRanking.classList.add('hidden');
    }
  }
}

// ─── Deferred ranking UI helpers ─────────────────────────────────────────────

export function showRankingGenerating() {
  if (!dlRanking) return;
  dlRanking.classList.remove('hidden');
  dlRanking.classList.add('dl-card--ranking-generating');
  if (rankingIconChart) rankingIconChart.classList.add('hidden');
  if (rankingGenSpinner) rankingGenSpinner.classList.remove('hidden');
  if (rankingTabIconChart)  rankingTabIconChart.classList.add('hidden');
  if (rankingTabGenSpinner) rankingTabGenSpinner.classList.remove('hidden');
  if (tabBtnRanking)        tabBtnRanking.classList.add('pdf-tab-ranking-btn--generating');
  if (rankingLog) rankingLog.classList.remove('hidden');
}

export function applyRankingUrl(url) {
  if (!dlRanking) return;
  dlRanking.href = url;
  dlRanking.classList.remove('dl-card--ranking-generating');
  if (rankingIconChart) rankingIconChart.classList.remove('hidden');
  if (rankingGenSpinner) rankingGenSpinner.classList.add('hidden');
  if (rankingTabIconChart)  rankingTabIconChart.classList.remove('hidden');
  if (rankingTabGenSpinner) rankingTabGenSpinner.classList.add('hidden');
  if (tabBtnRanking) {
    tabBtnRanking.classList.remove('pdf-tab-ranking-btn--generating');
    tabBtnRanking.disabled = false;
    tabBtnRanking.removeAttribute('aria-disabled');
  }
  if (rankingLog) rankingLog.classList.add('hidden');
  if (!state.lastDownloadAllUrls.includes(url))
    state.lastDownloadAllUrls.push(url);
  // Load the ranking PDF into its tab panel so clicking the button works.
  state.enabledTabs.add('ranking');
  loadPdf('ranking', url).catch(function () {});
}

export function updateRankingLog(text) {
  if (rankingLog) rankingLog.textContent = text;
}

// ─── Download all ─────────────────────────────────────────────────────────────

function parseFilenameFromContentDisposition(header) {
  if (!header) return 'download.pdf';
  const m = header.match(/filename\*=UTF-8''([^;]+)|filename="([^"]*)"|filename=([^;\s]+)/i);
  if (!m) return 'download.pdf';
  const raw = (m[1] || m[2] || m[3] || '').trim();
  try {
    return decodeURIComponent(raw.replace(/\+/g, ' '));
  } catch (e) {
    return raw || 'download.pdf';
  }
}

export async function triggerDownloadAllPdfs() {
  const urls = state.lastDownloadAllUrls;
  if (!urls.length) return;
  for (let i = 0; i < urls.length; i++) {
    const res = await fetch(urls[i], { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) throw new Error('Could not download file (' + res.status + ').');
    const blob = await res.blob();
    const name = parseFilenameFromContentDisposition(res.headers.get('Content-Disposition'));
    const u = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = u;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(u);
    if (i < urls.length - 1) await sleep(250);
  }
}

// ─── Event wiring ─────────────────────────────────────────────────────────────

if (dlAll) {
  dlAll.addEventListener('click', function (e) {
    e.preventDefault();
    const errorPanel = document.getElementById('error-panel');
    triggerDownloadAllPdfs().catch(function (err) {
      if (errorPanel) {
        errorPanel.textContent = err.message || String(err);
        errorPanel.classList.remove('hidden');
      }
    });
  });
}
