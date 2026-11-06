'use strict';
(() => {
  const WINDOW_MS = 300000;
  const MAX_POINTS = 151;
  const POLL_MS = 2000;
  const history = [];
  let lastSample = null;
  let lastNewAt = null;
  let sampleClockOffset = null;
  let leftEdgePoint = null;
  const $ = (id) => document.getElementById(id);
  const number = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const percent = (value) => number(value) && value <= 100 ? value : null;
  const numeric = (value) => number(value) ? value : null;
  const decimal = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  const pct = (value) => percent(value) === null ? '—' : `${decimal.format(value)} %`;
  function bytes(value, rate = false) {
    if (!number(value)) return '—';
    const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ', 'ПиБ'];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${decimal.format(value)} ${units[unit]}${rate ? '/с' : ''}`;
  }
  function prune(now) {
    let changed = false;
    while (history.length && (history[0].time < now - WINDOW_MS || history.length > MAX_POINTS)) {
      leftEdgePoint = history.shift();
      changed = true;
    }
    return changed;
  }
  function addPoint(point) { history.push(point); prune(point.time); }
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function setText(id, text) {
    const node = $(id);
    if (node.textContent === text) return;
    node.textContent = text;
    if (!reduceMotion.matches && node.animate) {
      node.getAnimations().forEach((animation) => animation.cancel());
      node.animate([{ opacity: .65 }, { opacity: 1 }], { duration: 220 });
    }
  }
  function setDial(name, value) {
    const used = percent(value), meter = $(`${name}-meter`), arc = $(`${name}-arc`);
    setText(`${name}-value`, pct(value));
    arc.toggleAttribute('hidden', used === null || used === 0);
    arc.style.strokeDashoffset = used === null ? '100' : String(100 - used);
    meter.setAttribute('role', used === null ? 'img' : 'meter');
    const labels = { cpu: 'Загрузка CPU', ram: 'Использование RAM', disk: 'Заполнение Disk' };
    meter.setAttribute('aria-label', `${labels[name]}: ${used === null ? 'нет данных' : pct(used)}`);
    meter.setAttribute('aria-valuetext', used === null ? 'Нет данных' : pct(used));
    if (used === null) meter.removeAttribute('aria-valuenow');
    else meter.setAttribute('aria-valuenow', used);
  }
  function showValues(data) {
    const cpu = data?.cpu || {}, ram = data?.ram || {};
    const disk = data?.disk || {}, net = data?.network || {};
    setDial('cpu', cpu.usage_percent);
    setDial('ram', ram.usage_percent);
    setDial('disk', disk.usage_percent);
    for (const [id, value] of Object.entries({
      'ram-total': ram.total_bytes, 'ram-used': ram.used_bytes, 'ram-available': ram.available_bytes,
      'disk-total': disk.total_bytes, 'disk-used': disk.used_bytes, 'disk-free': disk.free_bytes,
      'disk-available': disk.available_bytes,
    })) $(id).textContent = bytes(value);
    setText('network-rx', bytes(net.rx_bytes_per_second, true));
    setText('network-tx', bytes(net.tx_bytes_per_second, true));
    $('network-interface').textContent = typeof net.interface === 'string' ? net.interface : 'Интерфейс —';
    $('network-tcp').textContent = Number.isInteger(net.tcp_established_total) && net.tcp_established_total >= 0
      ? new Intl.NumberFormat('ru-RU').format(net.tcp_established_total) : '—';
  }
  function svgElement(tag, attributes) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }
  // Keep raw paths in immutable time/value coordinates. One shared frame clock
  // pans the viewport; only a genuine range change changes the affine Y map.
  const charts = new Map();
  const scaleFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
  let timeOrigin = performance.now();
  const SCALE_HOLD_MS = 30000;
  const SCALE_TRANSITION_MS = 800;
  function upperSpeed(value) {
    const needed = Math.max(1, value * 1.25);
    const power = 10 ** Math.floor(Math.log10(needed));
    return [1, 2, 5, 10].find((step) => step * power >= needed) * power;
  }
  function desiredRange(chart, values) {
    const low = Math.min(...values), high = Math.max(...values);
    if (chart.key === 'network') return { min: 0, max: upperSpeed(high) };
    const padding = Math.max(.5, (high - low) * .2);
    let min = Math.max(0, low - padding), max = Math.min(100, high + padding);
    if (max - min < 1) {
      min = Math.max(0, Math.min(99, (min + max) / 2 - .5));
      max = min + 1;
    }
    return { min: Math.floor(min * 100) / 100, max: Math.ceil(max * 100) / 100 };
  }
  function affine(range) {
    const a = 112 / (range.max - range.min);
    return { a, b: 116 + a * range.min };
  }
  function scaleAt(chart, now) {
    const state = chart.scale;
    const progress = reduceMotion.matches ? 1 : Math.min(1, Math.max(0, (now - state.started) / SCALE_TRANSITION_MS));
    const ease = progress * progress * (3 - 2 * progress);
    // Interpolate a single affine transform, not individual point projections.
    return { a: state.from.a + (state.to.a - state.from.a) * ease,
      b: state.from.b + (state.to.b - state.from.b) * ease };
  }
  function setRange(chart, range, now) {
    const to = affine(range);
    const from = chart.scale ? scaleAt(chart, now) : to;
    chart.scale = { range, from, to, started: now };
    chart.svg.dataset.targetMin = range.min;
    chart.svg.dataset.targetMax = range.max;
    chart.lowSince = null;
  }
  function updateRange(chart, now) {
    const values = history.flatMap((point) => chart.series.map((key) => point[key])).filter(number);
    chart.hasData = values.length > 0;
    if (!chart.hasData) {
      chart.scale = null;
      chart.lowSince = null;
      return;
    }
    const wanted = desiredRange(chart, values);
    if (!chart.scale) { setRange(chart, wanted, now); return; }
    const current = chart.scale.range;
    const low = Math.min(...values), high = Math.max(...values);
    if (low < current.min || high > current.max) {
      setRange(chart, { min: Math.min(current.min, wanted.min), max: Math.max(current.max, wanted.max) }, now);
      return;
    }
    // Ignore ordinary extrema changes. Shrink only after a materially smaller
    // five-minute range has remained suitable for at least thirty seconds.
    if (wanted.max - wanted.min <= .65 * (current.max - current.min)) {
      if (chart.lowSince === null) chart.lowSince = now;
      if (now - chart.lowSince >= SCALE_HOLD_MS) setRange(chart, wanted, now);
    } else chart.lowSince = null;
  }
  function initChart(key, series) {
    const svg = $(`${key}-chart`);
    const horizontal = [4, 32, 60, 88, 116].map((y) => `M4 ${y}H596`).join(' ');
    svg.append(svgElement('path', { d: horizontal, class: 'grid' }));
    if (key === 'network') {
      const vertical = [4, 122.4, 240.8, 359.2, 477.6, 596].map((x) => `M${x} 4V116`).join(' ');
      svg.append(svgElement('path', { d: vertical, class: 'grid vertical' }));
    }
    const clipId = `crdn-${key}-clip`;
    const defs = svgElement('defs', {}), clip = svgElement('clipPath', { id: clipId });
    clip.append(svgElement('rect', { x: 4, y: 3, width: 592, height: 114 }));
    defs.append(clip);
    const viewport = svgElement('g', { 'clip-path': `url(#${clipId})` });
    const plot = svgElement('g', { class: 'plot' });
    viewport.append(plot);
    svg.append(defs, viewport);
    const traces = new Map(), dots = new Map();
    for (const name of series) {
      const trace = svgElement('path', { class: `trace ${name}` });
      const dot = svgElement('circle', { r: 2, class: `point ${name}` });
      plot.append(trace);
      viewport.append(dot);
      traces.set(name, trace);
      dots.set(name, dot);
    }
    const label = svgElement('text', { x: 300, y: 65, 'text-anchor': 'middle' });
    label.textContent = 'Нет данных';
    svg.append(label);
    charts.set(key, { key, series, svg, plot, label, traces, dots, latest: new Map(), scale: null, lowSince: null });
  }
  function rebuildPaths(chart) {
    const points = leftEdgePoint ? [leftEdgePoint, ...history] : history;
    for (const key of chart.series) {
      let path = '', previous = null, latest = null;
      for (const point of points) {
        const value = point[key];
        if (value == null) { previous = null; continue; }
        const x = 596 + 592 * (point.time - timeOrigin) / WINDOW_MS;
        const connect = previous && point.time - previous.time <= 5000 && point.time > previous.time
          && (!['rx', 'tx'].includes(key) || point.interface === previous.interface);
        path += `${connect ? 'L' : 'M'}${x.toFixed(4)} ${-value} `;
        previous = point;
        if (point !== leftEdgePoint) latest = { x, value };
      }
      chart.traces.get(key).setAttribute('d', path);
      chart.latest.set(key, latest);
    }
  }
  function paintCharts(now) {
    const shift = -592 * (now - timeOrigin) / WINDOW_MS;
    for (const chart of charts.values()) {
      chart.label.setAttribute('visibility', chart.hasData ? 'hidden' : 'visible');
      if (!chart.scale) {
        if (chart.empty) continue;
        chart.empty = true;
        chart.lastA = null;
        chart.plot.setAttribute('visibility', 'hidden');
        for (const dot of chart.dots.values()) dot.setAttribute('visibility', 'hidden');
        if (chart.key !== 'network') {
          $(`${chart.key}-range`).textContent = 'Нет данных';
          chart.svg.removeAttribute('data-scale-min');
          chart.svg.removeAttribute('data-scale-max');
        } else for (const id of ['net-max', 'net-mid', 'net-zero']) $(id).textContent = '—';
        continue;
      }
      chart.empty = false;
      const { a, b } = scaleAt(chart, now);
      chart.plot.setAttribute('visibility', 'visible');
      chart.plot.setAttribute('transform', `matrix(1 0 0 ${a} ${shift} ${b})`);
      if (chart.lastA !== a || chart.lastB !== b) {
        const min = (b - 116) / a, max = min + 112 / a;
        chart.svg.dataset.scaleMin = min;
        chart.svg.dataset.scaleMax = max;
        if (chart.key === 'network') {
          $('net-max').textContent = bytes(max, true);
          $('net-mid').textContent = bytes(max / 2, true);
          $('net-zero').textContent = '0 Б/с';
        } else $(`${chart.key}-range`).textContent = `${scaleFormat.format(Math.max(0, min))}–${scaleFormat.format(Math.min(100, max))} %`;
        chart.lastA = a;
        chart.lastB = b;
      }
      for (const key of chart.series) {
        const latest = chart.latest.get(key), dot = chart.dots.get(key);
        dot.setAttribute('visibility', latest ? 'visible' : 'hidden');
        if (latest) {
          dot.setAttribute('cx', latest.x + shift);
          dot.setAttribute('cy', b - a * latest.value);
        }
      }
    }
  }
  const cpuStatsFormat = new Intl.NumberFormat('ru-RU', {
    minimumFractionDigits: 1, maximumFractionDigits: 1,
  });
  function showCpuHistoryStats() {
    const values = history.map((point) => point.cpu).filter((value) => percent(value) !== null);
    const stats = values.length ? {
      average: values.reduce((sum, value) => sum + value, 0) / values.length,
      maximum: Math.max(...values),
      minimum: Math.min(...values),
    } : { average: null, maximum: null, minimum: null };
    for (const [key, value] of Object.entries(stats)) {
      $(`cpu-${key}`).textContent = value === null ? '—' : `${cpuStatsFormat.format(value)} %`;
    }
  }
  function draw(now) {
    prune(now);
    showCpuHistoryStats();
    if (now - timeOrigin > 2 * WINDOW_MS) timeOrigin = now;
    for (const chart of charts.values()) {
      updateRange(chart, now);
      rebuildPaths(chart);
    }
    paintCharts(now);
  }
  function frame() {
    const now = performance.now();
    if (prune(now)) draw(now);
    paintCharts(now);
    requestAnimationFrame(frame);
  }
  function validate(data) {
    if (!data || !number(data.sampled_at_unix_seconds)
        || !['cpu', 'ram', 'disk', 'network'].every((key) => data[key] && typeof data[key] === 'object' && !Array.isArray(data[key]))) {
      throw new Error('Invalid metrics response');
    }
  }
  async function poll() {
    const started = performance.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch('/api/metrics', { cache: 'no-store', signal: controller.signal });
      if (response.status === 401) { location.replace('/login'); return; }
      if (!response.ok) throw new Error('Metrics unavailable');
      const data = await response.json();
      validate(data);
      const now = performance.now();
      if (data.sampled_at_unix_seconds !== lastSample) {
        const net = data.network;
        const sampledMs = data.sampled_at_unix_seconds * 1000;
        if (sampleClockOffset === null) sampleClockOffset = now - sampledMs;
        addPoint({ time: sampledMs + sampleClockOffset, sampledAt: data.sampled_at_unix_seconds,
          cpu: percent(data.cpu.usage_percent), ram: percent(data.ram.usage_percent),
          rx: numeric(net.rx_bytes_per_second), tx: numeric(net.tx_bytes_per_second), interface: net.interface });
        lastSample = data.sampled_at_unix_seconds;
        lastNewAt = now;
      } else if (now - lastNewAt > 6000) throw new Error('Stale snapshot');
      showValues(data);
      $('connection').textContent = 'Подключено';
      $('connection').dataset.state = 'online';
      $('updated').textContent = `Снимок: ${new Date(data.sampled_at_unix_seconds * 1000).toLocaleTimeString('ru-RU')}`;
      draw(now);
    } catch {
      showValues(null);
      $('connection').textContent = 'Нет свежих данных · повторяем подключение';
      $('connection').dataset.state = 'offline';
      const now = performance.now();
      addPoint({ time: now, cpu: null, ram: null, rx: null, tx: null });
      draw(now);
    } finally {
      clearTimeout(timeout);
      // A single chain: even a slow request cannot overlap with the next one.
      setTimeout(poll, Math.max(0, POLL_MS - (performance.now() - started)));
    }
  }
  initChart('cpu', ['cpu']);
  initChart('ram', ['ram']);
  initChart('network', ['rx', 'tx']);
  showValues(null);
  draw(performance.now());
  requestAnimationFrame(frame);
  poll();
})();
