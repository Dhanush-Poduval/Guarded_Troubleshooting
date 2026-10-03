/* The verified resolution loop, client side.
 *
 * This file renders what the server reports and nothing more. In particular it never
 * decides that a problem is solved: the status shown always comes from the session's
 * verification_status, so clicking a deeplink cannot turn into "issue resolved" through
 * an optimistic update here.
 *
 * Three labelling rules are enforced in this file because they are what a user actually
 * reads:
 *
 *   - "System verified" appears only for verification_status === 'system_verified',
 *     which the server grants only for evidence it obtained itself.
 *   - A value typed by the user is submitted as an observation and comes back as
 *     client-reported; the page says so rather than implying a check took place.
 *   - When verification fails, the next action offered is the next one already in the
 *     validated plan. No replacement step is generated here.
 *
 * Plan text originates from a language model by way of the API, so it is rendered with
 * created nodes and textContent, never by interpolating into innerHTML.
 */

'use strict';

(function () {
  const $ = (id) => document.getElementById(id);

  const el = {
    start: $('startres'),
    startBtn: $('start-resolution'),
    panel: $('res'),
    progress: $('res-progress'),

    banner: $('res-banner'),
    badge: $('res-badge'),
    why: $('res-why'),

    num: $('res-num'),
    name: $('res-name'),
    cat: $('res-cat'),
    desc: $('res-desc'),
    steps: $('res-steps'),
    links: $('res-links'),

    check: $('res-check'),
    expect: $('res-expect'),
    verify: $('res-verify'),
    report: $('res-report'),
    fixed: $('res-fixed'),
    notfixed: $('res-notfixed'),
    observe: $('res-observe'),
    observeLabel: $('res-observe-label'),
    observed: $('res-observed'),
    submit: $('res-submit'),
    next: $('res-next'),
    abandon: $('res-abandon'),

    receipt: $('receipt'),
    toast: $('toast'),
  };

  let session = null;
  let busy = false;

  /* The exact words a user sees for each status. Kept in one place so a status can
     never be described one way in the banner and another in the receipt. */
  const LABEL = {
    pending: 'Not checked yet',
    system_verified: 'System verified',
    user_confirmed: 'User confirmed',
    verification_failed: 'Verification failed',
    verification_unavailable: 'Verification unavailable',
    inconclusive: 'Inconclusive',
  };

  const WHY = {
    pending: 'Perform the action below, then check the result.',
    system_verified:
      'The server read the setting back from the device and it matches the expected state.',
    user_confirmed:
      'You reported this fixed the problem. The server did not read the device itself.',
    verification_failed:
      'The observed state does not match what this action should have produced.',
    verification_unavailable:
      'This deployment cannot read Galaxy settings back, so an automatic check is not possible here.',
    inconclusive:
      'The result could not be decided from the evidence available.',
  };

  function toast(message) {
    if (!el.toast) return;
    el.toast.textContent = message;
    el.toast.dataset.show = 'yes';
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.toast.dataset.show = 'no'; }, 3600);
  }

  async function call(path, options) {
    if (busy) return null;
    busy = true;
    try {
      const res = await fetch(path, options);
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = body && body.detail;
        toast(typeof detail === 'string' ? detail : 'That step could not be completed.');
        return null;
      }
      return body;
    } catch (err) {
      toast('Could not reach the API.');
      return null;
    } finally {
      busy = false;
    }
  }

  /* ------------------------------------------------------------- rendering */

  function deeplinkButton(uri, label) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'deeplink';

    const icon = document.createElement('span');
    icon.className = 'deeplink__icon';
    btn.appendChild(icon);

    const text = document.createElement('span');
    text.className = 'deeplink__text';

    const top = document.createElement('span');
    top.className = 'deeplink__label';
    top.textContent = label || 'Open on device';
    text.appendChild(top);

    const sub = document.createElement('span');
    sub.className = 'deeplink__uri';
    sub.textContent = uri;
    text.appendChild(sub);

    btn.appendChild(text);

    btn.addEventListener('click', async () => {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(uri).catch(() => {});
      }
      toast('Deeplink copied — on-device this opens the screen directly.');
      // Recording that the action was presented is all this does. The server moves no
      // verification status for a click, and neither does this page.
      const body = await call(`/v1/resolution/sessions/${session.session_id}/presented`, {
        method: 'POST',
      });
      if (body) render(body);
    });

    return btn;
  }

  function renderStep(step) {
    el.num.textContent = String(step.step_number);
    el.name.textContent = step.action_name;
    el.cat.textContent = step.category;
    el.cat.dataset.c = step.category;
    el.desc.textContent = step.description || '';

    el.steps.textContent = '';
    (step.steps || []).forEach((line) => {
      const li = document.createElement('li');
      li.textContent = line;
      el.steps.appendChild(li);
    });

    el.links.textContent = '';
    if (step.actionable_deeplink) {
      el.links.appendChild(
        deeplinkButton(step.actionable_deeplink, step.actionable_label)
      );
    } else {
      const none = document.createElement('p');
      none.className = 'no-link';
      none.textContent =
        'No deeplink for this step — the catalog has no screen for it. Follow the steps manually.';
      el.links.appendChild(none);
    }

    el.expect.textContent = '';
    if (step.verification_available && step.expected) {
      const label = document.createElement('span');
      label.className = 'res__expectk mono';
      label.textContent = 'EXPECTED STATE';
      el.expect.appendChild(label);

      const value = document.createElement('span');
      value.className = 'res__expectv mono';
      value.textContent = step.expected.summary;
      el.expect.appendChild(value);
    } else {
      const label = document.createElement('span');
      label.className = 'res__expectk mono';
      label.textContent = 'NO CHECKABLE CONTRACT';
      el.expect.appendChild(label);

      const value = document.createElement('span');
      value.className = 'res__expectv';
      value.textContent =
        step.verification_unavailable_reason === 'no_validation_deeplink'
          ? 'The catalog entry for this step carries no validation deeplink.'
          : 'The catalog entry carries a validation deeplink but not a complete contract.';
      el.expect.appendChild(value);
    }
  }

  function renderObserved(view) {
    const last = (view.attempts || [])[view.attempts.length - 1];
    if (!last) return;

    const row = document.createElement('div');
    row.className = 'res__observed';

    const k = document.createElement('span');
    k.className = 'res__expectk mono';
    k.textContent = 'OBSERVED';
    row.appendChild(k);

    const v = document.createElement('span');
    v.className = 'res__expectv mono';
    v.textContent =
      last.observed_value === null || last.observed_value === undefined
        ? 'nothing was read'
        : last.observed_value;
    row.appendChild(v);

    const src = document.createElement('span');
    src.className = 'res__src';
    src.dataset.s = last.evidence_source;
    src.textContent =
      last.evidence_source === 'trusted_adapter'
        ? 'read by the server'
        : last.evidence_source === 'client_reported'
          ? 'reported by you'
          : 'your confirmation';
    row.appendChild(src);

    el.expect.appendChild(row);
  }

  function render(view) {
    session = view;
    el.panel.hidden = false;
    el.start.hidden = true;

    const status = view.verification_status;
    el.banner.dataset.state = status;
    el.badge.textContent = LABEL[status] || status;
    el.why.textContent = WHY[status] || '';

    if (view.current) {
      renderStep(view.current);
      el.progress.textContent =
        `Action ${view.current.step_number} of ${view.current.step_total}`;
      renderObserved(view);
    }

    // Automatic checking is offered only when this deployment can actually do it.
    const canAuto = view.automatic_verification_possible &&
      view.current && view.current.verification_available;
    el.verify.disabled = !canAuto;
    el.verify.title = canAuto
      ? 'Ask the server to read the setting back'
      : 'This deployment has no channel to the device, so it cannot read settings back';

    el.report.disabled = !(view.current && view.current.verification_available);

    const finished = ['resolved', 'unresolved', 'inconclusive', 'abandoned']
      .indexOf(view.status) !== -1;

    [el.fixed, el.notfixed, el.next, el.abandon, el.submit].forEach((b) => {
      b.disabled = finished;
    });
    if (finished) {
      el.verify.disabled = true;
      el.report.disabled = true;
      el.observe.hidden = true;
      el.check.classList.add('is-done');
      loadReceipt(view.session_id);
      return;
    }

    el.check.classList.remove('is-done');
    el.next.textContent = view.has_next
      ? (view.next_is_critical ? 'Next action (critical)' : 'Next action')
      : 'No further actions';

    if (view.current && view.current.verification_available && view.current.expected) {
      el.observeLabel.textContent =
        'Value shown on your device for "' + view.current.expected.key + '"';
    }
  }

  /* -------------------------------------------------------------- receipt */

  async function loadReceipt(sessionId) {
    const res = await fetch(`/v1/resolution/sessions/${sessionId}/receipt`);
    if (!res.ok) return;
    const r = await res.json();

    el.receipt.textContent = '';
    el.receipt.hidden = false;
    el.receipt.dataset.kind = r.system_verified ? 'system' : 'user';

    const head = document.createElement('div');
    head.className = 'receipt__head';

    const title = document.createElement('strong');
    title.textContent = 'Verification receipt';
    head.appendChild(title);

    const badge = document.createElement('span');
    badge.className = 'receipt__badge';
    badge.dataset.kind = r.system_verified ? 'system' : 'user';
    badge.textContent = r.system_verified
      ? 'System verified'
      : r.verification_method === 'user_confirmed'
        ? 'User confirmed'
        : 'Not verified';
    head.appendChild(badge);
    el.receipt.appendChild(head);

    const rows = [
      ['Session', r.session_id],
      ['Problem', r.query],
      ['Actions attempted', String(r.actions_attempted)],
      ['Successful action', r.successful_action || 'none'],
      ['Verification method', r.verification_method],
      ['Expected state', r.expected_state || 'not applicable'],
      ['Observed state', r.observed_state || 'not read'],
      ['Final status', r.final_status],
      ['Completed', r.completed_at ? new Date(r.completed_at).toLocaleString() : ''],
    ];

    const dl = document.createElement('dl');
    dl.className = 'receipt__grid';
    rows.forEach(([k, v]) => {
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v;
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
    el.receipt.appendChild(dl);

    if (r.caveat) {
      const caveat = document.createElement('p');
      caveat.className = 'receipt__caveat';
      caveat.textContent = r.caveat;
      el.receipt.appendChild(caveat);
    }
  }

  /* --------------------------------------------------------------- wiring */

  el.startBtn.addEventListener('click', async () => {
    const payload = window.__lastPlan;
    if (!payload) {
      toast('Generate a plan first.');
      return;
    }
    const body = await call('/v1/resolution/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: payload.query,
        plan: payload.response,
        request_id: payload.meta ? payload.meta.request_id : null,
      }),
    });
    if (body) {
      el.receipt.hidden = true;
      render(body);
      el.panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  });

  el.verify.addEventListener('click', async () => {
    const body = await call(`/v1/resolution/sessions/${session.session_id}/verify`, {
      method: 'POST',
    });
    if (body) render(body);
  });

  el.report.addEventListener('click', () => {
    el.observe.hidden = !el.observe.hidden;
    if (!el.observe.hidden) el.observed.focus();
  });

  el.submit.addEventListener('click', async () => {
    const value = el.observed.value.trim();
    if (!value) {
      toast('Enter the value your device shows.');
      return;
    }
    const body = await call(
      `/v1/resolution/sessions/${session.session_id}/observations`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // A fresh token per submission, so a retry after a timeout cannot record twice.
        body: JSON.stringify({
          observed_value: value,
          observation_token: `${session.session_id}:${Date.now()}`,
        }),
      }
    );
    if (body) {
      el.observed.value = '';
      el.observe.hidden = true;
      render(body);
    }
  });

  el.fixed.addEventListener('click', async () => {
    const body = await call(`/v1/resolution/sessions/${session.session_id}/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolved: true }),
    });
    if (body) render(body);
  });

  el.notfixed.addEventListener('click', async () => {
    const body = await call(`/v1/resolution/sessions/${session.session_id}/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolved: false }),
    });
    if (body) render(body);
  });

  el.next.addEventListener('click', async () => {
    let ack = false;
    if (session.next_is_critical) {
      // Critical actions are disruptive — a forced restart, a reset, a service visit.
      ack = window.confirm(
        'The next action is marked critical and may be disruptive. Continue?'
      );
      if (!ack) return;
    }
    const body = await call(`/v1/resolution/sessions/${session.session_id}/advance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ acknowledge_critical: ack }),
    });
    if (body) render(body);
  });

  el.abandon.addEventListener('click', async () => {
    const body = await call(`/v1/resolution/sessions/${session.session_id}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'abandoned' }),
    });
    if (body) render(body);
  });

  /* app.js publishes the last served plan here and shows the start control. */
  window.__resolution = {
    offer(payload) {
      window.__lastPlan = payload;
      const contexts = (payload.response && payload.response.contexts) || [];
      el.start.hidden = contexts.length === 0;
      el.panel.hidden = true;
      el.receipt.hidden = true;
      session = null;
    },
    hide() {
      el.start.hidden = true;
      el.panel.hidden = true;
      el.receipt.hidden = true;
      session = null;
    },
  };
})();
