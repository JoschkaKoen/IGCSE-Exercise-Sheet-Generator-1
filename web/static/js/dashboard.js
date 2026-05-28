/* Per-user dashboard wiring.
 *
 * Three things:
 *  1. Change-username + change-password forms — POST to /api/account/*
 *  2. Logout button — POST /api/account/logout then redirect to /
 *  3. Weekly-activity bar chart — Chart.js fed from #dashboard-weekly-data JSON.
 *
 * Vanilla IIFE; no framework, no build step. Mirrors the style of account.js.
 */
(function () {
  'use strict';

  function tl(key) {
    return (window.i18n && window.i18n[key]) || key;
  }

  function showErr(el, key) {
    if (!el) return;
    el.textContent = tl(key);
    el.classList.remove('dashboard-form-ok');
    el.removeAttribute('hidden');
  }
  function showOk(el, key) {
    if (!el) return;
    el.textContent = tl(key);
    el.classList.add('dashboard-form-ok');
    el.removeAttribute('hidden');
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, status: res.status, body: data };
      }, function () {
        return { ok: res.ok, status: res.status, body: {} };
      });
    });
  }

  // ---- username change ----
  var uForm = document.getElementById('dashboard-username-form');
  if (uForm) {
    var uErr = uForm.querySelector('[data-dashboard-username-err]');
    uForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (uErr) uErr.setAttribute('hidden', '');
      var fd = new FormData(uForm);
      var newUsername = String(fd.get('new_username') || '').trim();
      var currentPassword = String(fd.get('current_password') || '');
      if (!newUsername || !currentPassword) return;
      postJson('/api/account/change-username', {
        new_username: newUsername,
        current_password: currentPassword,
      }).then(function (r) {
        if (r.ok) {
          showOk(uErr, 'dashboard.settings.saved');
          // Reload after a short delay so the new username shows in the header.
          setTimeout(function () { window.location.reload(); }, 600);
          return;
        }
        var detail = (r.body && r.body.detail) || 'account.err.unknown';
        showErr(uErr, detail);
      }).catch(function () {
        showErr(uErr, 'account.err.network');
      });
    });
  }

  // ---- password change ----
  var pForm = document.getElementById('dashboard-password-form');
  if (pForm) {
    var pErr = pForm.querySelector('[data-dashboard-password-err]');
    pForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (pErr) pErr.setAttribute('hidden', '');
      var fd = new FormData(pForm);
      var newPassword = String(fd.get('new_password') || '');
      var currentPassword = String(fd.get('current_password') || '');
      if (!newPassword || !currentPassword) return;
      postJson('/api/account/change-password', {
        new_password: newPassword,
        current_password: currentPassword,
      }).then(function (r) {
        if (r.ok) {
          showOk(pErr, 'dashboard.settings.saved');
          pForm.reset();
          return;
        }
        var detail = (r.body && r.body.detail) || 'account.err.unknown';
        showErr(pErr, detail);
      }).catch(function () {
        showErr(pErr, 'account.err.network');
      });
    });
  }

  // ---- logout ----
  var logoutBtn = document.getElementById('dashboard-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      fetch('/api/account/logout', {
        method: 'POST',
        credentials: 'same-origin',
      }).finally(function () {
        window.location.href = '/';
      });
    });
  }

  // ---- weekly activity chart ----
  function initChart() {
    if (typeof window.Chart === 'undefined') return; // chart.umd.min.js not yet loaded
    var node = document.getElementById('dashboard-weekly-data');
    var canvas = document.getElementById('dashboard-weekly-chart');
    if (!node || !canvas) return;
    var rows;
    try { rows = JSON.parse(node.textContent || '[]'); } catch (_) { rows = []; }
    var labels = rows.map(function (r) { return r.week; });
    var values = rows.map(function (r) { return r.n; });
    new window.Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: tl('dashboard.exam.activity_chart'),
          data: values,
          backgroundColor: 'rgba(34, 211, 238, 0.55)',
          borderColor: 'rgba(34, 211, 238, 0.9)',
          borderWidth: 1,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: 'rgb(148, 163, 184)' }, grid: { color: 'rgba(255,255,255,0.04)' } },
          y: { beginAtZero: true, ticks: { color: 'rgb(148, 163, 184)', precision: 0 }, grid: { color: 'rgba(255,255,255,0.04)' } },
        },
      },
    });
  }

  // Chart.js loads with `defer` so it may not be ready when this IIFE runs.
  // Wait for DOMContentLoaded or a microtask if already past it.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initChart);
  } else {
    // Push to a macrotask so the deferred Chart.js bundle has parsed.
    setTimeout(initChart, 0);
  }
})();
