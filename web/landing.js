/* Landing page behaviour.
 *
 * Everything numeric on this page is either read from the running instance or left as a
 * dash. Nothing is invented in the browser, because a plausible-looking number rendered
 * here would be indistinguishable from a measured one.
 *
 * The sample request is deliberately NOT fired on page load. A complaint the cache cannot
 * answer runs the full pipeline, which costs a paid model call, so a visitor has to ask
 * for it. The complaint used is one of the official ones, which the pre-warm step has
 * normally already cached. */

'use strict';

const $ = (id) => document.getElementById(id);

function ms(value) {
  if (value === null || value === undefined) return '—';
  if (value >= 10000) return (value / 1000).toFixed(1) + ' s';
  if (value >= 100) return Math.round(value) + ' ms';
  return value.toFixed(1) + ' ms';
}

/* ----------------------------------------------------------------- health */

async function loadHealth() {
  const dot = $('nav-health').querySelector('.dot');
  const prevDot = $('prev-state').querySelector('.dot');
  try {
    const res = await fetch('/health');
    const body = await res.json().catch(() => ({}));
    const ok = res.ok && body.status === 'ok';
    dot.dataset.state = ok ? 'ok' : 'warn';
    prevDot.dataset.state = ok ? 'ok' : 'warn';
    $('nav-health-text').textContent = ok ? 'ready' : 'not ready';
    $('prev-state-text').textContent = ok ? 'session active' : 'not ready';
    $('foot-health').textContent = ok
      ? 'instance ready · database, model, indexes and catalog all live'
      : 'instance not ready — ' + (body.detail || 'see /health');
  } catch (err) {
    dot.dataset.state = 'down';
    prevDot.dataset.state = 'down';
    $('nav-health-text').textContent = 'offline';
    $('prev-state-text').textContent = 'api unreachable';
    $('foot-health').textContent = 'api unreachable';
  }
}

/* ------------------------------------------------------------ cache stats */

async function loadStats() {
  try {
    const res = await fetch('/cache/stats');
    if (!res.ok) return;
    const s = await res.json();
    $('ls-plans').textContent = s.cached_plans;
    $('ls-vectors').textContent = s.cached_vectors;
    $('ls-hitrate').textContent =
      s.hit_rate === null || s.hit_rate === undefined
        ? '—'
        : Math.round(s.hit_rate * 100) + '%';
    $('ls-p50').textContent = ms(s.hit_latency_p50_ms);
  } catch (err) {
    /* The strip is informational; a failure here must not disturb the page. */
  }
}

/* ----------------------------------------------------------- sample trace */

const SAMPLE_INDEX = 19; // the S24 Ultra black-screen complaint

async function runSample() {
  const btn = $('prev-run');
  btn.disabled = true;
  btn.textContent = 'running';
  $('prev-total').textContent = 'working…';

  try {
    const exRes = await fetch('/v1/examples');
    const examples = (await exRes.json()).examples || [];
    const pick = examples[SAMPLE_INDEX] || examples[0];
    if (!pick) throw new Error('no examples available');

    $('prev-query').textContent = '“' + pick.query + '”';

    const res = await fetch('/v1/troubleshoot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: pick.query, siis_response: pick.siis_response || null }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'HTTP ' + res.status);

    renderTrace(body);
    loadStats();
  } catch (err) {
    $('prev-total').textContent = 'failed';
    $('prev-valid').textContent = 'ERROR';
    $('prev-valid').classList.remove('ok');
  } finally {
    btn.disabled = false;
    btn.textContent = 'run sample';
  }
}

function renderTrace(body) {
  const meta = body.meta || {};
  const total = meta.latency_ms || 0;

  $('prev-total').textContent = ms(total) + (meta.cache_hit ? ' · cache hit' : ' · cache miss');

  // The third bar is whatever remains after embedding and lookup: on a miss that is the
  // pipeline, on a hit it is re-validation and the metrics write.
  const measured = [meta.embed_ms || 0, meta.lookup_ms || 0];
  const rest = Math.max(0, total - measured[0] - measured[1]);
  const parts = [measured[0], measured[1], meta.pipeline_ms !== null && meta.pipeline_ms !== undefined ? meta.pipeline_ms : rest];
  const labels = [
    '1. Vector embedding',
    '2. Cache lookup',
    meta.pipeline_ms !== null && meta.pipeline_ms !== undefined
      ? '3. Pipeline'
      : '3. Validate & record',
  ];

  Array.prototype.forEach.call($('prev-bars').children, (li, i) => {
    const value = parts[i];
    const pct = total > 0 ? Math.min(100, Math.max(2, (value / total) * 100)) : 0;
    li.querySelector('.tbars__k').textContent = labels[i];
    li.querySelector('i').style.width = pct.toFixed(1) + '%';
    li.querySelector('.tbars__v').textContent = ms(value);
  });

  $('prev-sim').textContent = meta.similarity ? meta.similarity.toFixed(4) : '—';

  const contexts = (body.response && body.response.contexts) || [];
  const valid = $('prev-valid');
  if (contexts.length) {
    valid.textContent = 'PASSED';
    valid.classList.add('ok');
  } else {
    valid.textContent = meta.fallback || 'NO PLAN';
    valid.classList.remove('ok');
  }

  renderPlanRows(contexts);
}

function renderPlanRows(contexts) {
  const host = $('prev-plan');
  const actions = contexts.length ? contexts[0].actions || [] : [];
  if (!actions.length) return;

  host.textContent = '';
  actions.slice(0, 4).forEach((action, i) => {
    const row = document.createElement('div');
    row.className = 'prevplan__row';

    const num = document.createElement('span');
    num.className = 'prevplan__num';
    num.textContent = String(i + 1);
    row.appendChild(num);

    const name = document.createElement('span');
    name.className = 'prevplan__name';
    name.textContent = action.actionName || 'Action';
    row.appendChild(name);

    const category = action.category || 'manual';
    const chip = document.createElement('span');
    chip.className = 'chip chip--' + category;
    chip.textContent = category;
    row.appendChild(chip);

    host.appendChild(row);
  });
}

/* ------------------------------------------------------------------ copy */

function wireCopy() {
  document.querySelectorAll('.copybtn[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const text = btn.dataset.copy;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});
      }
      const was = btn.textContent;
      btn.textContent = 'copied';
      setTimeout(() => { btn.textContent = was; }, 1400);
    });
  });

  const codeBtn = $('copy-code');
  if (codeBtn) {
    codeBtn.addEventListener('click', () => {
      const code = document.querySelector('.codecard__body');
      if (navigator.clipboard && navigator.clipboard.writeText && code) {
        navigator.clipboard.writeText(code.textContent).catch(() => {});
      }
      codeBtn.textContent = 'copied';
      setTimeout(() => { codeBtn.textContent = 'copy'; }, 1400);
    });
  }
}

/* --------------------------------------------------------------- start up */

$('prev-run').addEventListener('click', runSample);
wireCopy();
loadHealth();
loadStats();
setInterval(loadHealth, 20000);
