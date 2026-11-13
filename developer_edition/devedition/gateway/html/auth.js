// Developer Control Plane: first-run sign-up and sign-in against the hosted Registration service
// (Plan of Execution: "First-run sign-up, email verification and passwords", "Technical limits").
//
//   sign-up      POST /v1/registrations            → 6-digit code emailed
//   verify       POST /v1/registrations/verify     → set-password token (15 min)
//   resend       POST /v1/registrations/resend-code
//   password     POST /v1/password                 → bcrypt hash stored, then we sign in
//   sign-in      POST /v1/sessions                 → session token + RS256 license token
//   sign-out     DELETE /v1/sessions/current
//   forgot/reset POST /v1/password/forgot, /v1/password/reset
//   check-in     POST /v1/activations/check-in     → every 7 days; renews the license token
//   offline      POST /v1/events/login-batch       → sign-outs queued while offline
//
// The license token is verified here with the public key (signature, audience, this install_id and
// machine fingerprint, expiry). Offline, the app keeps working until the check-in grace period ends.
//
// Exposes window.DEAuth = { start({ onSignedIn(user), onSignedOut() }), signOut(), user() }.
(() => {
  const K = {
    session: 'inaiera.de.session',      // { session_token, license_token, next_check_in_at, user, install_id, … }
    pending: 'inaiera.de.pending',      // the sign-up / reset step to resume after a reload
    queue: 'inaiera.de.loginQueue',     // events waiting for /events/login-batch
    known: 'inaiera.de.hasAccount',     // show "Sign in" instead of "Create account" first
    key: 'inaiera.de.licenseKey',       // cached license public key (PEM), for offline checks
  };
  const ISS = 'inaiera-dev-registration';
  const AUD = 'inaiera-developer-edition';
  const PASSWORD_MIN = 12;
  const PAUSED = ['account_blocked', 'license_revoked', 'license_invalid'];   // shown as the paused screen
  const DAY = 86400_000;

  const ls = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch { /* blocked */ } },
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, (c) => (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)));
  const timeOf = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  let cfg = null;          // /install.json
  let hooks = {};
  let current = null;      // signed-in session
  let overlay, root;

  // ── Registration service client ───────────────────────────────────────────
  async function api(method, path, body, token) {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = `Bearer ${token}`;
    let r;
    try {
      r = await fetch(cfg.registration_url.replace(/\/+$/, '') + path,
        { method, headers, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
    } catch {
      return { ok: false, status: 0, network: true,
        error: { code: 'network', message: `Can't reach the Developer Edition registration service at ${cfg.registration_url}. Check that it's running and that you're online, then try again.` } };
    }
    let data = null;
    try { data = await r.json(); } catch { /* 204 or non-JSON */ }
    const out = { ok: r.ok, status: r.status, body: data };
    if (!r.ok) {
      out.error = data?.error || { code: `http_${r.status}`, message: `The registration service returned HTTP ${r.status}.` };
      const ra = Number(r.headers.get('Retry-After'));
      if (ra && !out.error.retry_after) out.error.retry_after = ra;
    }
    return out;
  }

  function errText(e) {
    if (!e) return '';
    if (e.code === 'rate_limited' && e.retry_after) {
      const s = Number(e.retry_after);
      return `Too many attempts. Try again in ${s >= 90 ? `${Math.ceil(s / 60)} minutes` : `${s} seconds`}.`;
    }
    if (e.code === 'code_invalid' && e.attempts_left != null) {
      return e.attempts_left > 0
        ? `That code is not correct. ${e.attempts_left} ${e.attempts_left === 1 ? 'try' : 'tries'} left.`
        : 'That code is not correct, and no tries are left. Ask for a new code.';
    }
    return e.message || 'Something went wrong. Try again.';
  }

  // ── License token (RS256 JWT) ─────────────────────────────────────────────
  const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), (c) => c.charCodeAt(0));
  const pemBody = (pem) => String(pem || '').replace(/-----[^-]+-----/g, '').replace(/\s+/g, '');

  async function licenseKey(force) {
    if (cfg.license_public_key) return pemBody(cfg.license_public_key);   // embedded at build time
    const cached = ls.get(K.key);
    if (cached && !force) return cached;
    try {
      const r = await fetch(cfg.registration_url.replace(/\/+$/, '') + '/license/public-key', { cache: 'no-store' });
      if (!r.ok) return cached;
      const key = pemBody(await r.text());
      ls.set(K.key, key);
      return key;
    } catch { return cached; }
  }

  async function verifySig(token, keyB64) {
    const [h, p, s] = token.split('.');
    if (JSON.parse(new TextDecoder().decode(b64(h))).alg !== 'RS256') return false;
    const key = await crypto.subtle.importKey('spki', b64(keyB64), { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
    return crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, b64(s), new TextEncoder().encode(`${h}.${p}`));
  }

  /** { ok, claims, reason }: signature, issuer, audience, this install, this machine, not expired. */
  async function checkLicense(token) {
    let claims;
    try { claims = JSON.parse(new TextDecoder().decode(b64(token.split('.')[1]))); } catch { return { ok: false, reason: 'malformed' }; }
    let key = await licenseKey(false);
    if (!key) return { ok: false, claims, reason: 'no_key' };
    let good = false;
    try { good = await verifySig(token, key); } catch { good = false; }
    if (!good) {                                   // the signing key may have rotated
      const fresh = await licenseKey(true);
      if (fresh && fresh !== key) { key = fresh; try { good = await verifySig(token, key); } catch { good = false; } }
    }
    if (!good) return { ok: false, claims, reason: 'signature' };
    if (claims.iss !== ISS || claims.aud !== AUD) return { ok: false, claims, reason: 'issuer' };
    if (claims.iid !== cfg.install_id) return { ok: false, claims, reason: 'other_install' };
    if (claims.fph && cfg.fingerprint_hash && claims.fph !== cfg.fingerprint_hash) return { ok: false, claims, reason: 'other_machine' };
    if (!claims.exp || claims.exp * 1000 < Date.now()) return { ok: false, claims, reason: 'expired' };
    return { ok: true, claims };
  }

  // ── Offline queue (login-batch) ───────────────────────────────────────────
  function enqueue(event) {
    const q = ls.get(K.queue) || [];
    q.push({ event, event_id: uuid(), occurred_at: new Date().toISOString(), install_id: cfg.install_id, app_version: cfg.app_version || undefined });
    ls.set(K.queue, q.slice(-500));
  }
  async function flushQueue(s) {
    const q = ls.get(K.queue) || [];
    if (!q.length) return;
    const r = await api('POST', '/events/login-batch', { events: q }, s.session_token);
    if (r.ok || r.status === 400) ls.set(K.queue, null);   // 400: nothing in it is acceptable any more
  }

  // ── Session lifecycle ─────────────────────────────────────────────────────
  const device = () => ({
    install_id: cfg.install_id,
    app_version: cfg.app_version || undefined,
    fingerprint_hash: cfg.fingerprint_hash || undefined,
    os: cfg.os || undefined,
  });

  async function saveAndEnter(body, prev) {
    const lic = await checkLicense(body.license_token);
    if (!lic.ok) {
      return { error: { code: 'license_invalid', message: lic.reason === 'other_machine' || lic.reason === 'other_install'
        ? 'The license returned for this sign-in belongs to another installation.'
        : "The license returned by the registration service couldn't be verified. Try again." } };
    }
    const s = {
      ...(prev || {}),
      ...(body.session_token ? { session_token: body.session_token, session_expires_at: body.session_expires_at } : {}),
      license_token: body.license_token,
      license_expires_at: body.license_expires_at,
      next_check_in_at: body.next_check_in_at,
      grace_days: lic.claims.grc ?? 7,
      user: body.user || prev?.user,
      install_id: cfg.install_id,
    };
    ls.set(K.session, s);
    ls.set(K.known, true);
    ls.set(K.pending, null);
    const wasIn = !!current;
    current = s;
    flushQueue(s).catch(() => {});
    if (!wasIn) { overlay.hidden = true; hooks.onSignedIn?.(s.user || {}); }
    return { ok: true };
  }

  function endLocal() {
    const was = current;
    current = null;
    ls.set(K.session, null);
    if (was) hooks.onSignedOut?.();
    overlay.hidden = false;
    return was;
  }

  async function signIn(email, password) {
    const r = await api('POST', '/sessions', { email, password, ...device() });
    if (!r.ok) return r;
    return saveAndEnter(r.body);
  }

  async function checkIn(s) {
    const r = await api('POST', '/activations/check-in', device(), s.session_token);
    if (!r.ok) return r;
    return saveAndEnter(r.body, s);
  }

  /** Called on start and periodically while signed in. */
  async function validate(s, { interactive }) {
    const lic = await checkLicense(s.license_token);
    const due = !s.next_check_in_at || Date.now() >= Date.parse(s.next_check_in_at);
    if (lic.ok && !due) {
      if (!current) { current = s; overlay.hidden = true; hooks.onSignedIn?.(s.user || {}); flushQueue(s).catch(() => {}); }
      return;
    }
    const r = await checkIn(s);
    if (r.ok) return;
    if (r.network) {
      const graceEnds = Date.parse(s.next_check_in_at || 0) + (s.grace_days ?? 7) * DAY;
      if (lic.ok && Date.now() < graceEnds) {                // offline, still within the grace period
        if (!current) { current = s; overlay.hidden = true; hooks.onSignedIn?.(s.user || {}); }
        return;
      }
      endLocal();
      ls.set(K.session, s);                                  // keep it: going online lets it renew
      return screens.offline();
    }
    if (r.status === 401) {
      endLocal();
      return screens.signin({ email: s.user?.email, notice: 'Your session has ended. Sign in again.' });
    }
    if (r.status === 403) {
      endLocal();
      ls.set(K.session, s);
      return screens.paused(r.error);
    }
    if (interactive || !current) { endLocal(); return screens.signin({ email: s.user?.email, error: r.error }); }
  }

  async function signOut() {
    const s = endLocal() || ls.get(K.session);
    ls.set(K.session, null);
    screens.signin({ email: s?.user?.email, notice: "You're signed out." });
    if (s?.session_token) {
      const r = await api('DELETE', '/sessions/current', null, s.session_token);
      if (r.network) enqueue('logout');                       // uploaded after the next sign-in
    }
  }

  // ── Screens ───────────────────────────────────────────────────────────────
  const field = (name, label, o = {}) => `
    <label class="af">
      <span class="af-l">${esc(label)}${o.optional ? ' <i>optional</i>' : ''}</span>
      <input name="${name}" type="${o.type || 'text'}" ${o.auto ? `autocomplete="${o.auto}"` : ''} ${o.optional ? '' : 'required'}
             value="${esc(o.value || '')}" ${o.attrs || ''} aria-describedby="err-${name}">
      ${o.hint ? `<small class="af-hint">${esc(o.hint)}</small>` : ''}
      <small class="af-err" id="err-${name}" data-err="${name}"></small>
    </label>`;

  function card({ title, sub = '', body = '', submit, links = '', notice = '' }) {
    root.innerHTML = `
      <form class="auth-card" novalidate>
        <div class="auth-brand"><span class="logo" aria-hidden="true">In</span><span>InaiEra <b>Developer Edition</b></span></div>
        <h1 id="so-title">${esc(title)}</h1>
        ${sub ? `<p class="auth-sub">${sub}</p>` : ''}
        <div class="af-note" role="status" ${notice ? '' : 'hidden'}>${esc(notice)}</div>
        <div class="af-alert" role="alert" hidden></div>
        ${body}
        ${submit ? `<button class="so-btn" type="submit">${esc(submit)}</button>` : ''}
        ${links ? `<div class="auth-links">${links}</div>` : ''}
      </form>`;
    const form = root.querySelector('form');
    const first = form.querySelector('input:not([type=hidden]):not([value]), input[value=""]:not([type=hidden]):not([type=checkbox])')
      || form.querySelector('input:not([type=hidden]), button');
    setTimeout(() => first?.focus(), 0);
    return form;
  }

  const link = (id, text) => `<button type="button" class="link" data-go="${id}">${esc(text)}</button>`;
  const on = (form, id, fn) => form.querySelectorAll(`[data-go="${id}"]`).forEach((b) => b.addEventListener('click', fn));

  function showErr(form, e, map = {}) {
    form.querySelectorAll('.af-err').forEach((x) => (x.textContent = ''));
    form.querySelectorAll('[aria-invalid]').forEach((x) => x.removeAttribute('aria-invalid'));
    const alert = form.querySelector('.af-alert');
    alert.hidden = !e;
    if (!e) return;
    const fields = e.fields || {};
    let placed = 0;
    for (const [k, msg] of Object.entries(fields)) {
      const name = map[k] || k;
      const slot = form.querySelector(`[data-err="${name}"]`);
      if (slot) { slot.textContent = msg[0].toUpperCase() + msg.slice(1) + '.'; form.elements[name]?.setAttribute('aria-invalid', 'true'); placed++; }
    }
    alert.textContent = placed && e.code === 'validation_failed' ? 'Check the highlighted fields.' : errText(e);
    (form.querySelector('[aria-invalid]') || alert).focus?.();
  }
  function note(form, text) { const n = form.querySelector('.af-note'); n.textContent = text; n.hidden = !text; }

  /** Run fn with the submit button busy; fn returns an error object to show, or nothing. */
  function onSubmit(form, fn, map) {
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector('[type=submit]');
      if (btn.disabled) return;
      const label = btn.textContent;
      btn.disabled = true; btn.textContent = 'Please wait…';
      try {
        const e = await fn(Object.fromEntries(new FormData(form)));
        if (e) showErr(form, e, map); else showErr(form, null);
      } finally { if (btn.isConnected) { btn.disabled = false; btn.textContent = label; } }
    });
  }

  const local = (fields, message = 'Check the highlighted fields.') => ({ code: 'validation_failed', message, fields });

  const screens = {
    loading() {
      root.innerHTML = `<div class="auth-card"><div class="auth-brand"><span class="logo" aria-hidden="true">In</span><span>InaiEra <b>Developer Edition</b></span></div><p class="auth-sub">Checking your license…</p></div>`;
    },

    setup(msg) {
      card({ title: 'Installation is not set up', sub: esc(msg) });
    },

    signup(o = {}) {
      const lic = cfg.license_url
        ? `<a href="${esc(cfg.license_url)}" target="_blank" rel="noopener">Developer Edition License</a>`
        : 'Developer Edition License';
      const form = card({
        title: 'Create your account',
        sub: 'One account for this installation. We’ll email you a 6-digit code to confirm your address.',
        notice: o.notice,
        body: field('name', 'Full name', { auto: 'name', value: o.name, attrs: 'maxlength="200"' })
          + field('email', 'Work email', { type: 'email', auto: 'email', value: o.email })
          + `<div class="af-row">${field('company', 'Company', { auto: 'organization', optional: true, value: o.company, attrs: 'maxlength="200"' })}
             ${field('role', 'Role', { auto: 'organization-title', optional: true, value: o.role, attrs: 'maxlength="200"' })}</div>`
          + `<label class="ac"><input type="checkbox" name="license_accepted" value="1" aria-describedby="err-license_accepted">
               <span>I accept the ${lic} (v${esc(cfg.license_version || '1.0')}): personal development and evaluation only, one user, one machine, no production use.</span></label>
             <small class="af-err" id="err-license_accepted" data-err="license_accepted"></small>
             <label class="ac"><input type="checkbox" name="marketing_consent" value="1"><span>Send me product news by email.</span></label>`,
        submit: 'Create account',
        links: `<span>Already have an account?</span> ${link('signin', 'Sign in')}`,
      });
      on(form, 'signin', () => screens.signin({ email: form.elements.email.value }));
      onSubmit(form, async (v) => {
        const miss = {};
        if (!v.name?.trim()) miss.name = 'required';
        if (!v.email?.trim()) miss.email = 'required';
        if (!v.license_accepted) miss.license_accepted = 'accept the license to continue';
        if (Object.keys(miss).length) return local(miss);
        const r = await api('POST', '/registrations', {
          name: v.name.trim(), email: v.email.trim(), company: v.company?.trim() || undefined, role: v.role?.trim() || undefined,
          install_id: cfg.install_id, license_version: cfg.license_version || '1.0', license_accepted: true,
          marketing_consent: !!v.marketing_consent, source: 'first_run',
        });
        if (r.ok) {
          ls.set(K.known, true);
          ls.set(K.pending, { step: 'verify', email: v.email.trim(), verification_id: r.body.verification_id, code_expires_at: r.body.code_expires_at });
          return void screens.verify();
        }
        if (r.error.code === 'email_taken') {
          ls.set(K.known, true);
          return void screens.signin({ email: v.email.trim(), notice: 'An account with this email already exists. Sign in instead.' });
        }
        if (r.error.code === 'license_not_accepted') return local({ license_accepted: 'accept the license to continue' }, r.error.message);
        return r.error;
      });
    },

    verify(o = {}) {
      const p = ls.get(K.pending);
      if (!p?.verification_id) return screens.signup();
      const form = card({
        title: 'Check your email',
        sub: `Enter the 6-digit code we sent to <b>${esc(p.email)}</b>.${p.code_expires_at ? ` It expires at ${esc(timeOf(p.code_expires_at))}.` : ''}`,
        notice: o.notice,
        body: field('code', 'Verification code', { auto: 'one-time-code', attrs: 'inputmode="numeric" pattern="[0-9]{6}" maxlength="6" class="code"' }),
        submit: 'Verify email',
        links: `${link('resend', 'Send a new code')} · ${link('back', 'Use a different email')}`,
      });
      on(form, 'back', () => { ls.set(K.pending, null); screens.signup({ email: p.email }); });
      on(form, 'resend', async () => {
        const r = await api('POST', '/registrations/resend-code', { verification_id: p.verification_id });
        if (!r.ok) return showErr(form, r.error);
        if (!r.body?.verification_id) {          // already verified (or blocked): nothing to resend
          ls.set(K.pending, null);
          return screens.signin({ email: p.email, notice: 'This email is already verified. Sign in.' });
        }
        ls.set(K.pending, { ...p, verification_id: r.body.verification_id, code_expires_at: r.body.code_expires_at });
        screens.verify({ notice: `We sent a new code to ${p.email}.` });
      });
      onSubmit(form, async (v) => {
        const code = String(v.code || '').replace(/\D/g, '');
        if (code.length !== 6) return local({ code: 'enter the 6 digits from the email' });
        const r = await api('POST', '/registrations/verify', { verification_id: p.verification_id, code, install_id: cfg.install_id });
        if (r.ok) {
          ls.set(K.pending, { step: 'password', email: p.email, token: r.body.set_password_token, token_expires_at: r.body.set_password_token_expires_at });
          return void screens.password();
        }
        if (r.error.code === 'code_invalid') return { ...r.error, fields: { code: errText(r.error).replace(/\.$/, '').replace(/^./, (c) => c.toLowerCase()) } };
        if (r.error.code === 'code_expired') return { ...r.error, message: 'This code has expired or had too many wrong tries. Send a new code.' };
        return r.error;
      });
    },

    password(o = {}) {
      const p = ls.get(K.pending);
      if (!p?.token || Date.parse(p.token_expires_at) < Date.now()) {
        ls.set(K.pending, null);
        return screens.signin({ email: p?.email, notice: 'That step timed out. Sign in, and we’ll send you a new code.' });
      }
      const form = card({
        title: 'Set your password',
        sub: `Your email <b>${esc(p.email)}</b> is verified. Choose a password of at least ${PASSWORD_MIN} characters.`,
        notice: o.notice,
        body: `<input type="hidden" name="username" autocomplete="username" value="${esc(p.email)}">`
          + field('password', 'Password', { type: 'password', auto: 'new-password', attrs: `minlength="${PASSWORD_MIN}" maxlength="128"` })
          + field('confirm', 'Confirm password', { type: 'password', auto: 'new-password' }),
        submit: 'Set password and sign in',
      });
      onSubmit(form, async (v) => {
        if ((v.password || '').length < PASSWORD_MIN) return local({ password: `at least ${PASSWORD_MIN} characters` });
        if (v.password !== v.confirm) return local({ confirm: 'the passwords do not match' });
        const r = await api('POST', '/password', { password: v.password }, p.token);
        if (!r.ok) {
          if (r.status === 401) {
            ls.set(K.pending, null);
            return void screens.signin({ email: p.email, notice: `${r.error.message} Sign in, and we’ll send you a new code.` });
          }
          return r.error;
        }
        ls.set(K.pending, null);
        const s = await signIn(p.email, v.password);
        if (!s.ok) return void screens.signin({ email: p.email, notice: 'Your password is set.', error: s.error });
      });
    },

    signin(o = {}) {
      const form = card({
        title: 'Sign in',
        sub: 'Use the email and password of your Developer Edition account.',
        notice: o.notice,
        body: field('email', 'Email', { type: 'email', auto: 'username', value: o.email })
          + field('password', 'Password', { type: 'password', auto: 'current-password' }),
        submit: 'Sign in',
        links: `${link('forgot', 'Forgot password?')} · ${link('signup', 'Create an account')}`,
      });
      if (o.error) showErr(form, o.error);
      on(form, 'forgot', () => screens.forgot({ email: form.elements.email.value }));
      on(form, 'signup', () => screens.signup({ email: form.elements.email.value }));
      onSubmit(form, async (v) => {
        const miss = {};
        if (!v.email?.trim()) miss.email = 'required';
        if (!v.password) miss.password = 'required';
        if (Object.keys(miss).length) return local(miss);
        const r = await signIn(v.email.trim(), v.password);
        if (r.ok) return;
        if (r.error?.code === 'email_not_verified') {
          if (r.error.verification_id) {
            ls.set(K.pending, { step: 'verify', email: v.email.trim(), verification_id: r.error.verification_id, code_expires_at: r.error.code_expires_at });
            return void screens.verify({ notice: 'Verify your email first: we sent you a new code.' });
          }
          return { ...r.error, message: 'Verify your email first. You’ve asked for several codes already, so wait up to an hour and sign in again to get a new one.' };
        }
        if (PAUSED.includes(r.error?.code)) return void screens.paused(r.error);
        return r.error;
      });
    },

    forgot(o = {}) {
      const form = card({
        title: 'Reset your password',
        sub: 'Enter your account email. If it’s registered, we’ll send you a 6-digit reset code.',
        body: field('email', 'Email', { type: 'email', auto: 'email', value: o.email }),
        submit: 'Send reset code',
        links: link('signin', 'Back to sign in'),
      });
      on(form, 'signin', () => screens.signin({ email: form.elements.email.value }));
      onSubmit(form, async (v) => {
        if (!v.email?.trim()) return local({ email: 'required' });
        const r = await api('POST', '/password/forgot', { email: v.email.trim() });
        if (!r.ok) return r.error;
        ls.set(K.pending, { step: 'reset', email: v.email.trim() });
        screens.reset({ notice: `If an account exists for ${v.email.trim()}, a reset code is on its way.` });
      });
    },

    reset(o = {}) {
      const p = ls.get(K.pending);
      if (!p?.email) return screens.forgot();
      const form = card({
        title: 'Choose a new password',
        sub: `Enter the code we sent to <b>${esc(p.email)}</b> and a new password of at least ${PASSWORD_MIN} characters.`,
        notice: o.notice,
        body: `<input type="hidden" name="username" autocomplete="username" value="${esc(p.email)}">`
          + field('code', 'Reset code', { auto: 'one-time-code', attrs: 'inputmode="numeric" pattern="[0-9]{6}" maxlength="6" class="code"' })
          + field('new_password', 'New password', { type: 'password', auto: 'new-password', attrs: `minlength="${PASSWORD_MIN}" maxlength="128"` })
          + field('confirm', 'Confirm new password', { type: 'password', auto: 'new-password' }),
        submit: 'Change password',
        links: `${link('resend', 'Send a new code')} · ${link('signin', 'Back to sign in')}`,
      });
      on(form, 'signin', () => { ls.set(K.pending, null); screens.signin({ email: p.email }); });
      on(form, 'resend', async () => {
        const r = await api('POST', '/password/forgot', { email: p.email });
        if (!r.ok) return showErr(form, r.error);
        note(form, `If an account exists for ${p.email}, a new reset code is on its way.`);
      });
      onSubmit(form, async (v) => {
        const code = String(v.code || '').replace(/\D/g, '');
        const miss = {};
        if (code.length !== 6) miss.code = 'enter the 6 digits from the email';
        if ((v.new_password || '').length < PASSWORD_MIN) miss.new_password = `at least ${PASSWORD_MIN} characters`;
        else if (v.new_password !== v.confirm) miss.confirm = 'the passwords do not match';
        if (Object.keys(miss).length) return local(miss);
        const r = await api('POST', '/password/reset', { email: p.email, code, new_password: v.new_password });
        if (r.ok) {
          ls.set(K.pending, null);
          const s = await signIn(p.email, v.new_password);
          if (!s.ok) screens.signin({ email: p.email, notice: 'Your password is changed.', error: s.error });
          return;
        }
        if (r.error.code === 'code_invalid') return { ...r.error, fields: { code: errText(r.error).replace(/\.$/, '').replace(/^./, (c) => c.toLowerCase()) } };
        if (r.error.code === 'code_expired') return { ...r.error, message: 'This code has expired or had too many wrong tries. Send a new code.' };
        return r.error;
      });
    },

    paused(e = {}) {
      const why = {
        account_blocked: 'This account has been blocked by the Developer Edition team, so the app is paused. Contact support if you think this is a mistake.',
        license_revoked: 'The license for this installation has been revoked, so the app is paused. Contact support to restore it.',
        license_invalid: 'This installation’s license belongs to another machine. Sign in again on this machine.',
      }[e.code] || errText(e);
      const form = card({
        title: 'Developer Edition is paused',
        sub: esc(why),
        submit: 'Try again',
        links: link('other', 'Sign in with another account'),
      });
      on(form, 'other', () => { ls.set(K.session, null); screens.signin(); });
      onSubmit(form, async () => {
        const s = ls.get(K.session);
        if (!s) return void screens.signin({ email: e.email });
        await validate({ ...s, next_check_in_at: null }, { interactive: true });
      });
    },

    offline() {
      const form = card({
        title: 'Connect to renew your license',
        sub: 'Developer Edition checks in online every 7 days. This machine has been offline past the grace period, so the app is paused until it can reach the registration service.',
        submit: 'Try again',
        links: link('signin', 'Sign in again'),
      });
      on(form, 'signin', () => { ls.set(K.session, null); screens.signin(); });
      onSubmit(form, async () => {
        const s = ls.get(K.session);
        if (!s) return void screens.signin();
        await validate({ ...s, next_check_in_at: null }, { interactive: true });
      });
    },
  };

  function resume() {
    const p = ls.get(K.pending);
    if (p?.step === 'verify') return screens.verify();
    if (p?.step === 'password') return screens.password();
    if (p?.step === 'reset') return screens.reset();
    return ls.get(K.known) ? screens.signin() : screens.signup();
  }

  async function start(h) {
    hooks = h || {};
    overlay = document.getElementById('signed-out');
    root = document.getElementById('auth-root');
    overlay.hidden = false;
    screens.loading();
    try { cfg = await (await fetch('/install.json', { cache: 'no-store' })).json(); } catch { cfg = null; }
    if (!cfg?.install_id || !cfg?.registration_url) {
      cfg = cfg || {};
      return screens.setup('This installation has no install ID or registration service address. Run devedition/install-identity.sh, then docker compose up -d gateway.');
    }
    const s = ls.get(K.session);
    if (s && s.install_id === cfg.install_id && s.session_token) await validate(s, { interactive: false });
    else { ls.set(K.session, null); resume(); }

    // Check in when it is due: hourly, and whenever the tab comes back into view.
    const tick = () => { const cur = ls.get(K.session); if (current && cur && Date.now() >= Date.parse(cur.next_check_in_at)) validate(cur, { interactive: false }); };
    setInterval(tick, 3600_000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
    window.addEventListener('online', () => { if (current) flushQueue(current).catch(() => {}); });
  }

  window.DEAuth = { start, signOut, user: () => current?.user || null };
})();
