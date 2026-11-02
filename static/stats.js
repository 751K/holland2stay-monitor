// Site statistics: key numbers, two daily trend lines, and the make-up of listings
// first seen in the selected range. Nominal breakdowns are ranked HTML bars;
// ordered bins and the trends are Chart.js. Everything uses one mark colour
// (--stat-mark) read from CSS, so a theme switch only needs a redraw.
(() => {
  const config = JSON.parse(document.getElementById('stats-config').textContent);
  const zh = config.lang === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const MAX_ROWS = 8;   // past this, the tail folds into "Other"
  const STATUS = {'available to book': '可预订', 'available in lottery': '抽签中', 'reserved': '已预留', 'occupied': '已入住', 'not available': '不可申请'};
  const charts = new Map();
  let days = config.days;
  let latest = null;

  const theme = () => {
    const style = getComputedStyle(document.querySelector('.mail-page'));
    const v = name => style.getPropertyValue(name).trim();
    return {mark: v('--stat-mark'), wash: v('--stat-wash'), text: v('--mail-text'), muted: v('--mail-muted'), grid: v('--stat-grid'), surface: v('--mail-bg')};
  };

  // Display names: platform keys and upstream feature values are data, not copy
  const names = {
    source_dist: row => window.sourceLabel ? window.sourceLabel(row.source) : row.source,
    city_dist: row => row.city,
    status_dist: row => (zh && STATUS[String(row.status || '').toLowerCase()]) || row.status,
    tenant_dist: row => config.features[String(row.label || '').toLowerCase().trim()] || row.label,
    contract_dist: row => config.features[String(row.label || '').toLowerCase().trim()] || row.label,
  };
  const nameOf = (key, row) => (names[key] || (r => r.label))(row) || label('未知', 'Unknown');

  function ranked(list, key, rows) {
    list.replaceChildren();
    const total = rows.reduce((sum, row) => sum + (row.count || 0), 0);
    if (!total) { list.append(emptyNote()); return; }
    // Energy labels are a scale (A+++ best … G): keep that order instead of ranking
    const order = key === 'energy_dist' ? (a, b) => energyRank(a.label) - energyRank(b.label) : (a, b) => b.count - a.count;
    let shown = rows.filter(row => row.count > 0).sort(order);
    if (shown.length > MAX_ROWS) {
      const rest = shown.slice(MAX_ROWS - 1).reduce((sum, row) => sum + row.count, 0);
      shown = shown.slice(0, MAX_ROWS - 1).map(row => ({name: nameOf(key, row), count: row.count}))
        .concat({name: label('其他', 'Other'), count: rest, other: true});
    } else {
      shown = shown.map(row => ({name: nameOf(key, row), count: row.count}));
    }
    const top = Math.max(...shown.map(row => row.count));
    shown.forEach(row => {
      const item = document.createElement('li');
      if (row.other) item.className = 'is-other';
      const share = Math.round(row.count / total * 100);
      item.title = `${row.name}: ${row.count} (${share}%)`;
      // The count sits right at the end of its bar, so the eye never travels across the row
      item.innerHTML = '<span class="stats-name"></span><span class="stats-bar"><i></i><b class="stats-count"></b></span>';
      item.querySelector('.stats-name').textContent = row.name;
      item.querySelector('.stats-bar').style.setProperty('--share', Math.max(.02, row.count / top));
      item.querySelector('.stats-count').textContent = row.count;
      list.append(item);
    });
  }

  function energyRank(value) {
    const grade = String(value || '').trim().toUpperCase();
    const letter = 'ABCDEFG'.indexOf(grade[0]);
    if (letter < 0) return 99;
    return letter * 10 - (grade.match(/\+/g) || []).length;
  }

  function emptyNote() {
    const note = document.createElement('li');
    note.className = 'stats-empty';
    note.textContent = config.empty;
    return note;
  }

  function plotEmpty(canvas, empty) {
    const box = canvas.parentElement;
    box.querySelector('.stats-empty')?.remove();
    canvas.style.visibility = empty ? 'hidden' : '';   // Chart.js owns display
    if (empty) {
      const note = document.createElement('p');
      note.className = 'stats-empty';
      note.textContent = config.empty;
      box.append(note);
    }
  }

  function axes(c, {xGrid = false} = {}) {
    const ticks = {color: c.muted, font: {size: 12}};
    return {
      x: {grid: {display: xGrid, color: c.grid}, border: {color: c.grid}, ticks: {...ticks, maxRotation: 0, autoSkipPadding: 12}},
      y: {beginAtZero: true, grid: {color: c.grid}, border: {display: false}, ticks: {...ticks, precision: 0, maxTicksLimit: 5}},
    };
  }
  const tooltip = c => ({backgroundColor: c.surface, titleColor: c.muted, bodyColor: c.text, borderColor: c.grid, borderWidth: 1, padding: 10, displayColors: false});

  function draw(canvas, spec) {
    charts.get(canvas)?.destroy();
    charts.set(canvas, new Chart(canvas, spec));
  }

  function line(canvas, rows, seriesName) {
    const c = theme();
    const counts = rows.map(row => row.count);
    plotEmpty(canvas, false);
    draw(canvas, {
      type: 'line',
      data: {labels: rows.map(row => row.date.slice(5)), datasets: [{label: seriesName, data: counts,
        borderColor: c.mark, backgroundColor: c.wash, fill: true, cubicInterpolationMode: 'monotone', borderWidth: 2,
        pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: c.mark, pointHoverBorderColor: c.surface, pointHoverBorderWidth: 2}]},
      options: {responsive: true, maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
        plugins: {legend: {display: false}, tooltip: tooltip(c)}, scales: axes(c)},
    });
  }

  function columns(canvas, key, rows) {
    const c = theme();
    const counts = rows.map(row => row.count);
    const empty = !counts.some(Boolean);
    plotEmpty(canvas, empty);
    if (empty) { charts.get(canvas)?.destroy(); charts.delete(canvas); return; }
    const labels = rows.map(row => key === 'hourly_dist' ? String(row.hour).padStart(2, '0') : (key === 'price_dist' ? row.range : row.label));
    draw(canvas, {
      type: 'bar',
      data: {labels, datasets: [{label: label('房源', 'Listings'), data: counts, backgroundColor: c.mark,
        borderRadius: {topLeft: 4, topRight: 4}, borderSkipped: 'bottom', barPercentage: .7, categoryPercentage: .9, maxBarThickness: 28}]},
      options: {responsive: true, maintainAspectRatio: false,
        plugins: {legend: {display: false}, tooltip: tooltip(c)}, scales: axes(c)},
    });
  }

  function render(data) {
    latest = data;
    const s = data.summary || {};
    document.querySelectorAll('[data-kpi]').forEach(el => { el.textContent = s[el.dataset.kpi] ?? 0; });
    const n = s.days || days;
    document.querySelector('[data-kpi-label="new_range"]').textContent = label(`近 ${n} 天新增`, `New in ${n} days`);
    document.querySelector('[data-kpi-label="changes_range"]').textContent = label(`近 ${n} 天状态变更`, `Status changes in ${n} days`);
    document.querySelectorAll('[data-line]').forEach(canvas => line(canvas, data[canvas.dataset.line] || [],
      canvas.dataset.line === 'daily_new' ? label('新增', 'New') : label('变更', 'Changes')));
    document.querySelectorAll('[data-ranked]').forEach(list => ranked(list, list.dataset.ranked, data[list.dataset.ranked] || []));
    document.querySelectorAll('[data-columns]').forEach(canvas => columns(canvas, canvas.dataset.columns, data[canvas.dataset.columns] || []));
  }

  function load(range) {
    days = range;
    document.querySelector('.stats-body').setAttribute('aria-busy', 'true');
    // Same scope as the sidebar: scope / temporary filters come from this page's URL
    const params = new URLSearchParams(location.search);
    params.set('days', range);
    fetch(`/stats/data?${params}`, {cache: 'no-store'})
      .then(r => r.json())
      .then(render)
      .catch(error => console.error('Statistics failed to load:', error))
      .finally(() => document.querySelector('.stats-body').removeAttribute('aria-busy'));
  }

  document.querySelector('.stats-range').addEventListener('click', event => {
    const button = event.target.closest('[data-days]');
    if (!button || button.getAttribute('aria-pressed') === 'true') return;
    document.querySelectorAll('.stats-range [data-days]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    load(Number(button.dataset.days));
  });

  new MutationObserver(() => { if (latest) render(latest); })
    .observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});

  load(days);
})();
