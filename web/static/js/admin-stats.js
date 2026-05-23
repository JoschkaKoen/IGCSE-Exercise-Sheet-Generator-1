// web/static/js/admin-stats.js
// Renders two Chart.js line charts (pageviews + jobs over time) from the
// JSON payload embedded by the admin/stats.html template. The page also
// reloads on date-range form submit via plain GET — no JS required for that.

(function () {
  'use strict';

  const payloadEl = document.getElementById('chart-payload');
  if (!payloadEl) return;

  let payload;
  try {
    payload = JSON.parse(payloadEl.textContent || '{}');
  } catch (err) {
    console.error('admin-stats: bad chart payload', err);
    return;
  }

  // payload.series is a flat list of {day, kind, n}. Pivot into per-kind
  // {day -> n} maps, then build a sorted unique day axis spanning the window.
  const series = Array.isArray(payload.series) ? payload.series : [];
  const days = Number(payload.days || 30);

  const byKind = {};
  const daysSet = new Set();
  for (const row of series) {
    if (!row || !row.day || !row.kind) continue;
    if (!byKind[row.kind]) byKind[row.kind] = {};
    byKind[row.kind][row.day] = Number(row.n || 0);
    daysSet.add(row.day);
  }

  // Build the day axis. If a span was passed (>0), pad missing days with zero
  // so the chart spans the full window evenly.
  const axis = buildDayAxis(daysSet, days);

  function buildDayAxis(seen, span) {
    const today = new Date();
    const allDays = [];
    const cap = Math.max(span || 0, 1);
    for (let i = cap - 1; i >= 0; i--) {
      const d = new Date(today);
      d.setUTCDate(today.getUTCDate() - i);
      allDays.push(d.toISOString().slice(0, 10));
    }
    return allDays;
  }

  function valuesFor(kindMaps) {
    const summed = {};
    for (const m of kindMaps) {
      for (const day of Object.keys(m)) {
        summed[day] = (summed[day] || 0) + m[day];
      }
    }
    return axis.map((d) => summed[d] || 0);
  }

  const reqs = valuesFor([byKind.pageview || {}, byKind.api_call || {}]);
  const jobs = valuesFor([
    byKind.grade_job_finished || {},
    byKind.nl_job_finished || {},
  ]);

  // Cost-by-day series: payload.cost_by_day is [{day, pipeline, cost_rmb}, ...].
  // Pivot into {pipeline → {day → cost}} for the stacked area chart.
  const costByPipeline = { xscore: {}, nl: {}, eXam: {} };
  for (const row of (Array.isArray(payload.cost_by_day) ? payload.cost_by_day : [])) {
    if (!row || !row.day || !row.pipeline) continue;
    const bucket = costByPipeline[row.pipeline] || (costByPipeline[row.pipeline] = {});
    bucket[row.day] = (bucket[row.day] || 0) + Number(row.cost_rmb || 0);
  }

  // Round to 4 decimals for display tooltips without floating-point noise.
  const costFor = (kindMap) => axis.map((d) => Math.round((kindMap[d] || 0) * 10000) / 10000);

  // Match the site theme: light strokes on dark background, no fill by default.
  const lineOpts = (label, color, { stacked = false } = {}) => ({
    type: 'line',
    data: {
      labels: axis,
      datasets: [
        {
          label: label,
          data: null, // filled below
          borderColor: color,
          backgroundColor: color + '33',
          tension: 0.25,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#cbd5e1' } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: {
          stacked: stacked,
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: { color: '#64748b', maxTicksLimit: 8 },
        },
        y: {
          stacked: stacked,
          beginAtZero: true,
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: { color: '#64748b', precision: stacked ? 2 : 0 },
        },
      },
    },
  });

  function renderChart(canvasId, label, color, values) {
    const el = document.getElementById(canvasId);
    if (!el || typeof Chart === 'undefined') return;
    const cfg = lineOpts(label, color);
    cfg.data.datasets[0].data = values;
    new Chart(el.getContext('2d'), cfg);
  }

  function renderStackedCostChart(canvasId) {
    const el = document.getElementById(canvasId);
    if (!el || typeof Chart === 'undefined') return;
    // Use the lineOpts factory to get axis + theme, then swap in 3 datasets.
    const cfg = lineOpts(window.i18n['admin.stats.chart.cost'], '#22d3ee', { stacked: true });
    cfg.data.datasets = [
      makeCostDataset('xScore', '#22d3ee', costFor(costByPipeline.xscore || {})),
      makeCostDataset('NL',     '#a78bfa', costFor(costByPipeline.nl || {})),
      makeCostDataset('eXam',   '#34d399', costFor(costByPipeline.eXam || {})),
    ];
    new Chart(el.getContext('2d'), cfg);
  }

  function makeCostDataset(label, color, data) {
    return {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: color + '55',
      tension: 0.25,
      fill: true,
      pointRadius: 0,
      pointHoverRadius: 4,
      borderWidth: 2,
    };
  }

  renderChart('chart-pageviews', window.i18n['admin.stats.chart.requests'], '#22d3ee', reqs);
  renderChart('chart-jobs', window.i18n['admin.stats.chart.jobs'], '#a78bfa', jobs);
  renderStackedCostChart('chart-cost');
})();
