/* Smart Guided Troubleshooting Engine - demo client.
 *
 * Talks to the same origin that served this file, so there is no configuration and no
 * CORS: uvicorn serves both the API and this page.
 *
 * Two rules shaped this file.
 *
 * Nothing is computed here that the API already measured. Latency, similarity, cost and
 * the fallback reason are rendered exactly as the response reports them, because a
 * plausible-looking number invented in the browser would be indistinguishable from a
 * measured one and would misrepresent the system.
 *
 * Plan content is written with textContent and created nodes, never by interpolating into
 * innerHTML. The text comes from a language model by way of the API, so treating it as
 * markup would be both a rendering bug and an injection path.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const el = {
  examples: $('examples'),
  query: $('query'),
  siis: $('siis'),
  refWrap: $('ref-wrap'),
  refState: $('ref-state'),
  form: $('ask'),
  go: $('go'),
  again: $('again'),
  reset: $('reset'),
  result: $('result'),
  empty: $('empty'),
  working: $('working'),
  workingText: $('working-text'),
  planhead: $('planhead'),
  planCount: $('plan-count'),
  planTarget: $('plan-target'),

  health: $('health'),
  healthText: $('health-text'),

  verdict: $('verdict'),
  verdictTag: $('verdict-tag'),
  verdictWhy: $('verdict-why'),
  latTotal: $('lat-total'),
  latRail: $('lat-rail'),
  bars: $('bars'),
  mSim: $('m-sim'),
  mModel: $('m-model'),
  mCost: $('m-cost'),
  mVer: $('m-ver'),
  mFallback: $('m-fallback'),
  mReq: $('m-req'),
  gate: $('gate'),
  gateTitle: $('gate-title'),
  gateNote: $('gate-note'),
  vars: $('vars'),
  varsCount: $('vars-count'),
  varsList: $('vars-list'),
  raw: $('raw'),

  refreshStats: $('refresh-stats'),
  cachePill: $('cache-pill-text'),
  toast: $('toast'),
};

/* One source of truth for whether this client should animate. Checked at call time
   rather than cached, because a user can change the OS setting mid-session. */
function reducedMotion() {
  return window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

let lastPayload = null;
let inFlight = false;
let phaseTimers = [];

/* ------------------------------------------------------------- formatting */

function ms(value) {
  if (value === null || value === undefined) return '—';
  if (value >= 10000) return (value / 1000).toFixed(1) + ' s';
  if (value >= 100) return Math.round(value) + ' ms';
  return value.toFixed(1) + ' ms';
}

function num(value, digits) {
  if (value === null || value === undefined) return '—';
  return Number(value).toFixed(digits === undefined ? 0 : digits);
}

function toast(message, uri) {
  el.toast.textContent = '';
  el.toast.appendChild(document.createTextNode(message));
  if (uri) {
    el.toast.appendChild(document.createTextNode('  '));
    const code = document.createElement('span');
    code.className = 'mono';
    code.textContent = uri;
    el.toast.appendChild(code);
  }
  el.toast.dataset.show = 'yes';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.toast.dataset.show = 'no'; }, 3600);
}

/* ------------------------------------------------------------ the examples */

async function loadExamples() {
  try {
    const res = await fetch('/v1/examples');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    el.examples.textContent = '';
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = data.count
      ? 'Choose one of ' + data.count + '…'
      : 'none available';
    el.examples.appendChild(blank);

    (data.examples || []).forEach((item, i) => {
      const opt = document.createElement('option');
      opt.value = String(i);
      // The official complaints run long, so the menu shows a readable prefix.
      const short = item.query.length > 68 ? item.query.slice(0, 66) + '…' : item.query;
      opt.textContent = item.id + ' — ' + short;
      el.examples.appendChild(opt);
    });

    loadExamples.data = data.examples || [];
  } catch (err) {
    el.examples.textContent = '';
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'could not load examples';
    el.examples.appendChild(opt);
  }
}

el.examples.addEventListener('change', () => {
  const idx = el.examples.value;
  if (idx === '') return;
  const item = (loadExamples.data || [])[Number(idx)];
  if (!item) return;
  el.query.value = item.query;
  el.siis.value = item.siis_response || '';
  autogrow(el.query);
  markRef();
  el.again.disabled = true;
});

/* The official complaints run to several lines, so the box grows to fit rather than
   hiding the end of the sentence behind a scrollbar. */
function autogrow(node) {
  node.style.height = 'auto';
  node.style.height = Math.min(node.scrollHeight + 2, 260) + 'px';
}

function markRef() {
  const has = el.siis.value.trim().length > 0;
  el.refState.textContent = has ? 'loaded' : 'none';
  el.refState.dataset.on = has ? 'yes' : 'no';
}

el.siis.addEventListener('input', markRef);
el.query.addEventListener('input', () => autogrow(el.query));

/* --------------------------------------------------------------- the plan */

function deeplinkButton(link, verify) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'deeplink' + (verify ? ' deeplink--verify' : '');

  const icon = document.createElement('span');
  icon.className = 'deeplink__icon';
  btn.appendChild(icon);

  const text = document.createElement('span');
  text.className = 'deeplink__text';

  const label = document.createElement('span');
  label.className = 'deeplink__label';
  label.textContent = verify
    ? 'Verify: ' + (link.key || 'setting applied')
    : (link.message || link.description || 'Open on device');
  text.appendChild(label);

  const uri = document.createElement('span');
  uri.className = 'deeplink__uri';
  uri.textContent = link.deeplink;
  text.appendChild(uri);

  btn.appendChild(text);

  // A bixby:// URI resolves on a Galaxy device, not in a desktop browser. Rather than
  // pretend otherwise, the click copies the URI and says what would happen on-device.
  btn.addEventListener('click', () => {
    const kind = verify ? 'Validation deeplink copied' : 'Deeplink copied';
    const tail = verify
      ? ' — on-device this reads back the setting to confirm the step worked.'
      : ' — on-device this opens the screen directly.';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link.deeplink).catch(() => {});
    }
    toast(kind + tail, link.deeplink);
  });

  return btn;
}

function renderAction(action, index) {
  const wrap = document.createElement('div');
  wrap.className = 'action';

  const top = document.createElement('div');
  top.className = 'action__top';

  const nb = document.createElement('span');
  nb.className = 'action__num';
  nb.textContent = String(index + 1);
  top.appendChild(nb);

  const name = document.createElement('span');
  name.className = 'action__name';
  name.textContent = action.actionName || 'Action';
  top.appendChild(name);

  const cat = document.createElement('span');
  cat.className = 'cat';
  const category = action.category || 'manual';
  cat.dataset.c = category;
  cat.textContent = category;
  top.appendChild(cat);

  wrap.appendChild(top);

  if (action.description) {
    const desc = document.createElement('p');
    desc.className = 'action__desc';
    desc.textContent = action.description;
    wrap.appendChild(desc);
  }

  let links = 0;

  (action.stepGroups || []).forEach((group) => {
    const sg = document.createElement('div');
    sg.className = 'sg';

    const steps = document.createElement('ol');
    steps.className = 'steps';
    (group.steps || []).forEach((step) => {
      const li = document.createElement('li');
      li.textContent = step;
      steps.appendChild(li);
    });
    sg.appendChild(steps);

    const holder = document.createElement('div');
    holder.className = 'links';
    if (group.actionableDeeplink && group.actionableDeeplink.deeplink) {
      holder.appendChild(deeplinkButton(group.actionableDeeplink, false));
      links += 1;
    }
    if (group.validationDeeplink && group.validationDeeplink.deeplink) {
      holder.appendChild(deeplinkButton(group.validationDeeplink, true));
    }

    if (holder.children.length) {
      sg.appendChild(holder);
    } else {
      const none = document.createElement('p');
      none.className = 'no-link';
      none.textContent = 'No deeplink for this step — the catalog has no screen for it.';
      sg.appendChild(none);
    }

    wrap.appendChild(sg);
  });

  return { node: wrap, links: links };
}

function renderPlan(data) {
  el.result.textContent = '';
  const contexts = (data.response && data.response.contexts) || [];

  if (!contexts.length) {
    const box = document.createElement('div');
    box.className = 'fallback';

    const title = document.createElement('strong');
    const reason = (data.meta && data.meta.fallback) || 'no_match';
    title.textContent = reason === 'no_siis_context'
      ? 'No reference text, so no plan'
      : 'No viable plan for this complaint';
    box.appendChild(title);

    const p = document.createElement('p');
    p.appendChild(document.createTextNode('The engine returned '));
    const code = document.createElement('code');
    code.textContent = 'contexts: []';
    p.appendChild(code);
    p.appendChild(document.createTextNode(' with fallback '));
    const code2 = document.createElement('code');
    code2.textContent = reason;
    p.appendChild(code2);
    p.appendChild(document.createTextNode(
      reason === 'no_siis_context'
        ? '. Steps are only ever derived from supplied reference text, never invented, so an empty answer is the correct one here. Open the reference-text panel and load an official complaint.'
        : '. Guessing a plan would be worse than declining one, so nothing is served and nothing is cached.'
    ));
    box.appendChild(p);

    el.result.appendChild(box);
    if (el.planhead) el.planhead.hidden = true;
    return { goals: 0, actions: 0, links: 0 };
  }

  let actionCount = 0;
  let linkCount = 0;

  contexts.forEach((ctx) => {
    const goal = document.createElement('section');
    goal.className = 'goal';

    const top = document.createElement('div');
    top.className = 'goal__top';

    const heads = document.createElement('div');
    const h = document.createElement('h3');
    h.className = 'goal__title';
    h.textContent = ctx.title || 'Troubleshooting';
    heads.appendChild(h);

    if (ctx.goal) {
      const sub = document.createElement('p');
      sub.className = 'goal__sub';
      sub.textContent = ctx.goal;
      heads.appendChild(sub);
    }
    top.appendChild(heads);

    if (typeof ctx.score === 'number') {
      const score = document.createElement('span');
      score.className = 'score';
      score.textContent = ctx.score.toFixed(2);
      top.appendChild(score);
    }

    goal.appendChild(top);

    (ctx.actions || []).forEach((action, i) => {
      const built = renderAction(action, i);
      goal.appendChild(built.node);
      actionCount += 1;
      linkCount += built.links;
    });

    el.result.appendChild(goal);
  });

  showPlanHead(contexts, actionCount);
  return { goals: contexts.length, actions: actionCount, links: linkCount };
}

function showPlanHead(contexts, actionCount) {
  if (!el.planhead) return;
  if (!contexts.length) {
    el.planhead.hidden = true;
    return;
  }
  el.planhead.hidden = false;
  el.planCount.textContent =
    'Validated diagnostic plan (' + actionCount +
    (actionCount === 1 ? ' action)' : ' actions)');
  // The goal card below already carries the title, so the header reports the shape of
  // the plan instead: how many actions of each category, disruptive ones included.
  const tally = {};
  contexts.forEach((ctx) => {
    (ctx.actions || []).forEach((a) => {
      const c = a.category || 'manual';
      tally[c] = (tally[c] || 0) + 1;
    });
  });
  el.planTarget.textContent = ['auto', 'manual', 'critical']
    .filter((c) => tally[c])
    .map((c) => c + ' ×' + tally[c])
    .join('  ·  ');
}

/* ------------------------------------------------------------- evidence */

/* The rail is a proportional stack of the measured stages, not decoration: each colour
   spans exactly its share of the total, and whatever is unaccounted for stays grey. */
function paintRail(total, parts) {
  if (!el.latRail) return;
  if (!total) {
    el.latRail.style.width = '0%';
    return;
  }
  const share = (v) => ((v || 0) / total) * 100;
  const a = share(parts.embed);
  const b = a + share(parts.lookup);
  const c = b + share(parts.pipeline);
  el.latRail.style.width = '100%';
  el.latRail.style.background =
    'linear-gradient(90deg,' +
    ' var(--cyan) 0 ' + a.toFixed(2) + '%,' +
    ' var(--violet) ' + a.toFixed(2) + '% ' + b.toFixed(2) + '%,' +
    ' var(--violet-bright) ' + b.toFixed(2) + '% ' + c.toFixed(2) + '%,' +
    ' var(--line-2) ' + c.toFixed(2) + '% 100%)';
}

function renderEvidence(data, counts) {
  const meta = data.meta || {};

  const hit = meta.cache_hit === true;
  const empty = !((data.response && data.response.contexts) || []).length;

  if (empty) {
    el.verdict.dataset.state = 'warn';
    el.verdictTag.textContent = 'No plan served';
    el.verdictWhy.textContent = meta.fallback === 'no_siis_context'
      ? 'The complaint arrived without reference text, so there was nothing to derive steps from.'
      : 'The pipeline could not build a plan that passed validation, so an empty answer was served.';
  } else if (hit) {
    el.verdict.dataset.state = 'hit';
    el.verdictTag.textContent = 'Cache hit';
    const sim = meta.similarity;
    el.verdictWhy.textContent = sim
      ? 'A stored plan matched this wording at ' + sim.toFixed(4) +
        ' cosine similarity, so no model was called and nothing was paid for.'
      : 'Answered from a stored plan, so no model was called.';
  } else {
    el.verdict.dataset.state = 'miss';
    el.verdictTag.textContent = 'Cache miss';
    el.verdictWhy.textContent = meta.cache_reject_reason
      ? 'The cache declined to answer (' + meta.cache_reject_reason +
        '), so the full pipeline ran and the result is now cached.'
      : 'First time this complaint has been seen, so the full pipeline ran and the result is now cached.';
  }

  el.latTotal.textContent = meta.latency_ms !== undefined && meta.latency_ms !== null
    ? (meta.latency_ms >= 10000
        ? (meta.latency_ms / 1000).toFixed(1) + ' s'
        : Math.round(meta.latency_ms).toLocaleString())
    : '—';

  const total = meta.latency_ms || 0;
  const parts = {
    embed: meta.embed_ms,
    lookup: meta.lookup_ms,
    pipeline: meta.pipeline_ms,
  };
  Array.prototype.forEach.call(el.bars.children, (li) => {
    const value = parts[li.dataset.k];
    const out = li.querySelector('.bars__v');
    out.textContent =
      value === null || value === undefined
        ? (li.dataset.k === 'pipeline' ? 'not called' : '—')
        : ms(value);
  });
  paintRail(total, parts);

  el.mSim.textContent = meta.similarity ? meta.similarity.toFixed(4) : '—';
  el.mModel.textContent = meta.model || 'none called';
  el.mCost.textContent = meta.cost_usd ? '$' + Number(meta.cost_usd).toFixed(5) : '$0.00';
  el.mVer.textContent = meta.cache_version !== undefined ? meta.cache_version : '—';
  el.mFallback.textContent = meta.fallback || 'none';
  el.mReq.textContent = meta.request_id ? meta.request_id.slice(0, 8) : '—';

  if (empty) {
    el.gate.dataset.state = 'idle';
    el.gateTitle.textContent = 'Nothing to validate';
    el.gateNote.textContent =
      'No plan was produced, so the gate had nothing to check. An empty answer is a ' +
      'specified outcome, not a failure.';
  } else {
    el.gate.dataset.state = 'pass';
    el.gateTitle.textContent = 'Validated before serving';
    const bits = [
      counts.goals + (counts.goals === 1 ? ' goal' : ' goals'),
      counts.actions + (counts.actions === 1 ? ' action' : ' actions'),
      counts.links + ' one-tap ' + (counts.links === 1 ? 'deeplink' : 'deeplinks'),
    ];
    el.gateNote.textContent =
      bits.join(' · ') + '. Schema, wording rules and every URI checked against the ' +
      'catalog' + (hit ? ' again on this cache read' : '') + '. No free-text URLs.';
  }

  const variations = data.query_variations || [];
  if (variations.length) {
    el.vars.hidden = false;
    el.varsCount.textContent = '(' + variations.length + ')';
    el.varsList.textContent = '';
    variations.forEach((v) => {
      const li = document.createElement('li');
      li.textContent = v;
      el.varsList.appendChild(li);
    });
  } else {
    el.vars.hidden = true;
  }

  el.raw.textContent = JSON.stringify(data, null, 2);
}

/* ---------------------------------------------------------------- request */

/* The backend exposes a single pending state, so these are explanatory labels cycled on
   a timer rather than progress the server reported. They are ordered to match the work
   it actually does, and under reduced motion only the first is shown. */
const PHASES = [
  [0, 'Understanding complaint…'],
  [900, 'Retrieving relevant context…'],
  [2200, 'Validating safe actions…'],
  [6000, 'Preparing diagnostic plan…'],
];

function startPhases() {
  el.workingText.textContent = PHASES[0][1];
  if (reducedMotion()) return;
  phaseTimers = PHASES.slice(1).map(([delay, label]) =>
    setTimeout(() => { el.workingText.textContent = label; }, delay)
  );
}

function stopPhases() {
  phaseTimers.forEach(clearTimeout);
  phaseTimers = [];
}

async function send(payload) {
  if (inFlight) return;
  inFlight = true;
  el.go.disabled = true;
  el.again.disabled = true;
  el.empty.hidden = true;
  el.result.hidden = true;
  if (el.planhead) el.planhead.hidden = true;
  if (window.__resolution) window.__resolution.hide();
  el.working.hidden = false;
  el.form.classList.add('is-busy');
  startPhases();

  try {
    const res = await fetch('/v1/troubleshoot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const body = await res.json().catch(() => null);

    if (!res.ok) {
      const detail = body && body.detail
        ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
        : 'HTTP ' + res.status;
      el.verdict.dataset.state = 'error';
      el.verdictTag.textContent = res.status === 503 ? 'Service not ready' : 'Request failed';
      el.verdictWhy.textContent = detail;
      el.result.hidden = true;
      el.empty.hidden = false;
      toast(detail);
      return;
    }

    lastPayload = payload;
    const counts = renderPlan(body);
    renderEvidence(body, counts);
    el.result.hidden = false;
    // Offer the guided resolution loop for a plan that actually has actions to walk.
    if (window.__resolution) window.__resolution.offer(body);
    el.again.disabled = false;
    el.result.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    loadStats();
  } catch (err) {
    el.verdict.dataset.state = 'error';
    el.verdictTag.textContent = 'Could not reach the API';
    el.verdictWhy.textContent = String(err && err.message ? err.message : err);
    el.empty.hidden = false;
    toast('Could not reach the API. Is uvicorn still running?');
  } finally {
    stopPhases();
    el.form.classList.remove('is-busy');
    el.working.hidden = true;
    el.go.disabled = false;
    inFlight = false;
  }
}

el.form.addEventListener('submit', (event) => {
  event.preventDefault();
  const query = el.query.value.trim();
  if (!query) return;
  const siis = el.siis.value.trim();
  send({ query: query, siis_response: siis || null });
});

el.again.addEventListener('click', () => {
  if (lastPayload) send(lastPayload);
});

el.reset.addEventListener('click', () => {
  el.query.value = '';
  el.query.style.height = '';
  el.siis.value = '';
  el.examples.value = '';
  markRef();
  lastPayload = null;
  el.again.disabled = true;
  el.result.hidden = true;
  el.result.textContent = '';
  if (el.planhead) el.planhead.hidden = true;
  if (window.__resolution) window.__resolution.hide();
  el.empty.hidden = false;
  el.verdict.dataset.state = 'idle';
  el.verdictTag.textContent = 'Awaiting a request';
  el.verdictWhy.textContent = 'Send a complaint to see how it was answered.';
  el.latTotal.textContent = '—';
  Array.prototype.forEach.call(el.bars.children, (li) => {
    li.querySelector('.bars__v').textContent = '—';
  });
  if (el.latRail) el.latRail.style.width = '0%';
  ['m-sim', 'm-model', 'm-cost', 'm-ver', 'm-fallback', 'm-req'].forEach((id) => {
    $(id).textContent = '—';
  });
  el.gate.dataset.state = 'idle';
  el.gateTitle.textContent = 'Validation gate';
  el.gateNote.textContent =
    'Every plan is re-checked against the catalog before it is served — on cache ' +
    'reads too, not only on writes.';
  el.vars.hidden = true;
  el.raw.textContent = '—';
});

/* ----------------------------------------------------------------- health */

async function loadHealth() {
  try {
    const res = await fetch('/health');
    const body = await res.json().catch(() => ({}));
    const dot = el.health.querySelector('.dot');
    if (res.ok && body.status === 'ok') {
      dot.dataset.state = 'ok';
      el.healthText.textContent = 'all systems ready';
    } else {
      dot.dataset.state = 'warn';
      el.healthText.textContent = body.detail || 'not ready';
    }
  } catch (err) {
    el.health.querySelector('.dot').dataset.state = 'down';
    el.healthText.textContent = 'API unreachable';
  }
}

/* ------------------------------------------------------------ cache stats */

async function loadStats() {
  try {
    const res = await fetch('/cache/stats');
    if (!res.ok) return;
    const s = await res.json();
    $('s-plans').textContent = s.cached_plans;
    $('s-vectors').textContent = s.cached_vectors;
    $('s-hitrate').textContent = s.hit_rate === null || s.hit_rate === undefined
      ? '—'
      : Math.round(s.hit_rate * 100) + '%';
    $('s-hit-p50').textContent = ms(s.hit_latency_p50_ms);
    $('s-hit-p95').textContent = ms(s.hit_latency_p95_ms);
    $('s-miss-p50').textContent = ms(s.miss_latency_p50_ms);
    $('r-threshold').textContent = 'below threshold ' + num(s.rejected_below_threshold);
    $('r-ambiguous').textContent = 'too ambiguous ' + num(s.rejected_ambiguous);
    $('r-reval').textContent = 'failed re-validation ' + num(s.rejected_failed_revalidation);

    if (el.cachePill) {
      const rate = s.hit_rate === null || s.hit_rate === undefined
        ? '—'
        : Math.round(s.hit_rate * 100) + '%';
      el.cachePill.textContent = `cache ${s.cached_plans} plans · ${rate} hit`;
    }
  } catch (err) {
    /* The stats strip is informational; a failure here must not disturb the page. */
  }
}

el.refreshStats.addEventListener('click', loadStats);

/* -------------------------------------------------------------- start up */

markRef();
loadExamples();
loadHealth();
loadStats();
setInterval(loadHealth, 15000);
