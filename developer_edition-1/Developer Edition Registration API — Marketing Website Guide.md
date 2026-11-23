# Developer Edition Registration API — Marketing Website Guide

Nov 11, 2026 · @Arindam-Aadhya

## Overview

The marketing site needs four calls to turn a visitor into a verified, downloadable lead: **sign up, verify the emailed code, set a password, record the download**. All four are public JSON endpoints on the Registration service; none needs an API key, so nothing secret ever sits in the website's code.

| Environment | Base URL | Notes |
| --- | --- | --- |
| Production | `https://register.<domain>/v1` | Domain not final (open question 5 in the Plan of Execution) |
| Local (Docker, `just admin up`) | `http://localhost:4102/v1` | The admin backend container publishes port 4102 |
| Local (`npm run dev` in the backend) | `http://localhost:4002/v1` | Backend run directly on the host |

- **Format:** JSON request and response bodies, `Content-Type: application/json`, UTF-8. All times are ISO 8601 in UTC.
- **Auth:** none for the marketing calls. The set-password call carries a short-lived token returned by the verify call.
- **Browser access:** the service only answers browsers from origins listed in `DEV_PUBLIC_CORS_ORIGINS`. The marketing site's origin must be added there before any call works from the page (see Setup checklist).
- **Privacy:** raw IP addresses are never stored; only a country code from the edge header. Passwords and codes are stored as hashes only.

The same account is used later in the Developer Control Plane: a visitor who finishes this flow signs in to the installed app with the same email and password, with no second sign-up.

## Sign-up and download flow

Run the whole flow on the marketing page, ending with a password, so the visitor can sign in to the installed app straight away. A visitor who verifies the email but skips the password step has an account with no password: the app will refuse their sign-in until they use "Forgot password".

&#91;embedded content: marketing sign-up flow · 4 calls, 2 branches\]

A wrong code stays on the code screen; only an expired or exhausted code needs a resend, which returns a new `verification_id`.

1. **Form.** Collect name, email, company, role, the license checkbox and the optional product-emails checkbox. Send `POST /registrations` with `source: "marketing_form"`. Keep the returned `verification_id`.
2. **Code screen.** The visitor gets a 6-digit code by email (valid 10 minutes). Send `POST /registrations/verify` with the `verification_id` and the code. Keep the returned `set_password_token` (valid 15 minutes).
   - Wrong code → show the `attempts_left` from the error and let them retry.
   - Code expired or 5 wrong tries → offer "Send a new code": `POST /registrations/resend-code`, then use the new `verification_id`.
3. **Password screen.** At least 12 characters. Send `POST /password` with the token in the `Authorization` header. A welcome email follows.
4. **Download.** Send `POST /downloads` with the visitor's email, the chosen OS and architecture, and the page's referrer. Redirect the browser to the returned `download_url`.

Two branches to handle on the form:

- **Email already verified** (`409 email_taken`): the visitor already has an account. Skip to step 4 with a "Welcome back" message, or send them to the download directly.
- **Email registered but never verified**: `POST /registrations` returns `201` again with a fresh code, so the normal flow simply continues.

## Endpoint reference

Paths are relative to the base URL. Every error uses the shape in the next section.

### POST /registrations — sign up

Creates the account (unverified) and emails a 6-digit code.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `name` | string | yes | Up to 200 characters |
| `email` | string | yes | Case-insensitive; stored lower-case |
| `company` | string | no | Up to 200 characters |
| `role` | string | no | Job title, up to 200 characters |
| `license_version` | string | yes | The version of the license text shown next to the checkbox, e.g. `"1.0"` |
| `license_accepted` | boolean | yes | Must be `true`, otherwise `422 license_not_accepted` |
| `marketing_consent` | boolean | no | Product-emails opt-in; default `false` |
| `source` | string | yes for the website | Send `"marketing_form"` (the app sends `"first_run"`) |

```json
POST /v1/registrations
{
  "name": "Asha Rao",
  "email": "asha@company.com",
  "company": "Company Ltd",
  "role": "Data engineer",
  "license_version": "1.0",
  "license_accepted": true,
  "marketing_consent": false,
  "source": "marketing_form"
}

201 Created
{
  "user_id": "8f0c2b1e-…",
  "verification_id": "c41a9d3f-…",
  "code_expires_at": "2026-11-11T10:40:00.000Z"
}
```

Errors: `400 validation_failed`, `409 email_taken`, `422 license_not_accepted`, `403 account_blocked`, `429 rate_limited`.

### POST /registrations/verify — check the code

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `verification_id` | string (UUID) | yes | From the sign-up or resend response |
| `code` | string | yes | Exactly 6 digits; send as a string to keep leading zeros |

```json
POST /v1/registrations/verify
{ "verification_id": "c41a9d3f-…", "code": "482913" }

200 OK
{
  "user_id": "8f0c2b1e-…",
  "set_password_token": "eyJhbGciOi…",
  "set_password_token_expires_at": "2026-11-11T10:47:00.000Z"
}
```

Errors: `400 code_invalid` (with `attempts_left`), `410 code_expired` (10 minutes passed, 5 wrong tries, or a newer code was sent), `400 validation_failed` (unknown `verification_id`), `403 account_blocked`, `429 rate_limited`.

### POST /registrations/resend-code — send a new code

The previous code stops working. Up to 3 resends per email per hour.

```json
POST /v1/registrations/resend-code
{ "verification_id": "c41a9d3f-…" }

202 Accepted
{ "verification_id": "5e77a0c2-…", "code_expires_at": "2026-11-11T10:52:00.000Z" }
```

Always use the **new** `verification_id` for the next verify call. An empty `202 {}` means the email is already verified (or the account is not active): move the visitor on to sign-in or download. Errors: `400 validation_failed`, `429 rate_limited`.

### POST /password — set the password

Header `Authorization: Bearer <set_password_token>`. The token works once and expires 15 minutes after verify.

```json
POST /v1/password
Authorization: Bearer eyJhbGciOi…
{ "password": "a-long-passphrase" }

200 OK
{ "ok": true }
```

Password rules: 12 to 128 characters. Errors: `400 validation_failed` (too short or long), `401 unauthenticated` (token expired or already used; send the visitor back to request a new code), `403 account_blocked`.

### POST /downloads — record the download

Records the download against the visitor and returns the link to send them to.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `email` | string | recommended | Ties the download to the account; without it the download counts as anonymous |
| `os` | string | recommended | `windows`, `macos` or `linux` (the admin portal filters on these) |
| `arch` | string | recommended | `amd64` or `arm64` |
| `release_version` | string | no | e.g. `"v1.0.0"`; defaults to the latest release |
| `referrer` | string | no | `document.referrer` or a campaign tag, up to 500 characters |
| `source` | string | no | Defaults to `"marketing_page"` |

```json
POST /v1/downloads
{ "email": "asha@company.com", "os": "macos", "arch": "arm64", "referrer": "https://www.google.com/" }

201 Created
{
  "download_id": "0d3e…",
  "release_version": "v1.0.0",
  "download_url": "https://github.com/aadhyaanalytics/developer-edition-public/releases/download/v1.0.0/developer-edition-v1.0.0.zip"
}
```

Errors: `400 validation_failed`, `429 rate_limited`.

### Optional: forgot-password page

If the website offers its own "Forgot password" page (the app has one too):

| Call | Body | Response |
| --- | --- | --- |
| `POST /password/forgot` | `{ "email" }` | Always `202 {}`, whether or not the email exists |
| `POST /password/reset` | `{ "email", "code", "new_password" }` | `200 { "ok": true }`; `400 code_invalid` / `410 code_expired` as for verify |

A reset signs the user out of the app on every machine.

## Errors and rate limits

Every error has one shape, so the form can show `error.message` directly and branch on `error.code`:

```json
{ "error": { "code": "code_invalid", "message": "That code is not correct.", "attempts_left": 3 } }
```

Validation errors add a `fields` object naming each bad field, e.g. `{"email": "not a valid email address"}`, so the form can mark the right input.

| Status | `error.code` | When | What the form should do |
| --- | --- | --- | --- |
| 400 | `validation_failed` | Missing or malformed field | Highlight the fields in `error.fields` |
| 400 | `code_invalid` | Wrong 6-digit code | Show "Wrong code, N tries left" from `attempts_left` |
| 401 | `unauthenticated` | Set-password token expired or used | Restart from the code screen with a new code |
| 403 | `account_blocked` | An admin blocked this account | Show the message and a support link; stop the flow |
| 409 | `email_taken` | Email already verified | Show "You already have an account" and go to download |
| 410 | `code_expired` | 10 minutes passed, 5 wrong tries, or a newer code exists | Offer "Send a new code" |
| 422 | `license_not_accepted` | Checkbox not ticked | Ask the visitor to accept the license |
| 429 | `rate_limited` | Too many attempts | Disable the button for `Retry-After` seconds (header) or `error.retry_after` |
| 500 | `internal_error` | Server fault | Generic "Try again in a minute" |

Rate limits are counted per client IP over a 15-minute window, plus a per-email cap on codes.

| Call | Limit |
| --- | --- |
| `POST /registrations` | 5 per IP per 15 minutes |
| `POST /registrations/verify` | 30 per IP per 15 minutes |
| `POST /registrations/resend-code` | 20 per IP per 15 minutes |
| `POST /downloads` | 30 per IP per 15 minutes |
| `POST /password/forgot` | 10 per IP per 15 minutes |
| `POST /password/reset` | 20 per IP per 15 minutes |
| Codes emailed to one address | 4 per hour (the first plus 3 resends), sign-up and reset counted separately |

Visitors behind one office network share an IP, so the sign-up limit of 5 can bite during an event or workshop. The value is set by `DEV_RL_SIGNUPS_PER_IP` on the service.

## JavaScript example

Plain `fetch`, no library, so it drops into any site builder or framework. Wire each function to its screen; the comments mark where the UI reacts.

```javascript
const API = 'https://register.<domain>/v1'; // local: http://localhost:4102/v1
const LICENSE_VERSION = '1.0';              // must match the license text shown on the form

// One helper: returns the JSON body, or throws an Error carrying the API's error fields.
async function call(path, body, token) {
  const res = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error?.message || 'Something went wrong. Try again.');
    Object.assign(err, { status: res.status, code: data.error?.code, ...data.error,
      retryAfter: Number(res.headers.get('Retry-After')) || data.error?.retry_after });
    throw err;
  }
  return data;
}

const state = {}; // email, verificationId, setPasswordToken

// Step 1: the sign-up form
export async function signUp({ name, email, company, role, acceptedLicense, productEmails }) {
  state.email = email;
  try {
    const r = await call('/registrations', {
      name, email, company, role,
      license_version: LICENSE_VERSION,
      license_accepted: acceptedLicense === true,
      marketing_consent: !!productEmails,
      source: 'marketing_form',
    });
    state.verificationId = r.verification_id;
    return 'show-code-screen';
  } catch (e) {
    if (e.code === 'email_taken') return 'already-registered'; // go straight to download
    throw e; // show e.message; e.fields marks bad inputs
  }
}

// Step 2: the code screen
export async function verifyCode(code) {
  try {
    const r = await call('/registrations/verify', { verification_id: state.verificationId, code: String(code).trim() });
    state.setPasswordToken = r.set_password_token;
    return 'show-password-screen';
  } catch (e) {
    if (e.code === 'code_invalid') e.message = `Wrong code. ${e.attempts_left} tries left.`;
    if (e.code === 'code_expired') e.message = 'That code has expired. Send a new one.';
    throw e;
  }
}

export async function resendCode() {
  const r = await call('/registrations/resend-code', { verification_id: state.verificationId });
  if (r.verification_id) state.verificationId = r.verification_id; // always use the newest id
  return r.verification_id ? 'code-sent' : 'already-verified';
}

// Step 3: the password screen
export async function setPassword(password) {
  if (password.length < 12) throw new Error('Use at least 12 characters.');
  await call('/password', { password }, state.setPasswordToken);
  return 'show-download';
}

// Step 4: download
export async function download({ os, arch }) {
  const r = await call('/downloads', { email: state.email, os, arch, referrer: document.referrer || undefined });
  window.location.href = r.download_url;
}
```

For the OS picker, a reasonable default comes from the browser: `navigator.userAgentData?.platform` (or `navigator.platform` as a fallback), mapped to `windows`, `macos` or `linux`. Let the visitor change it, since Apple Silicon and Intel Macs need different builds (`arm64` vs `amd64`).

## Setup checklist

The website cannot call the API until the first item is done on the Registration service.

- [ ] **Allow the website's origin.** Add it to `DEV_PUBLIC_CORS_ORIGINS` on the service, comma-separated with `http://localhost:8080`, e.g. `https://www.<domain>,http://localhost:8080`. Include staging and preview origins the site is tested on.
- [ ] **Show the license text and its version.** Link the Developer Edition License next to the checkbox and send its exact version as `license_version`. Bump both together when Legal changes the text.
- [ ] **Link the privacy notice** on the form: it must say the app checks in online and what installation data is collected (Plan of Execution, Legal §3).
- [ ] **Never pre-tick** the license or product-emails checkboxes.
- [ ] **Behind a CDN or proxy**, set `DEV_TRUST_PROXY=true` on the service so rate limits count the visitor's IP, not the proxy's, and pass a country header (`CF-IPCountry` or `X-Geo-Country`) so downloads show a country.
- [ ] **Release links:** confirm `DEV_DOWNLOAD_URL_TEMPLATE` and `DEV_LATEST_VERSION` on the service point at the public release before launch.
- [ ] **Test end to end** against local Docker (`http://localhost:4102/v1`) with a real inbox, then check the visitor appears in the Admin Portal under Developer Edition → Registered Users with a first download.
- [ ] **Bounce reporting:** once SendGrid's Event Webhook points at `/v1/webhooks/sendgrid`, undelivered codes show in the Admin Portal's Email Delivery screen.

Open questions: the production domain for the service, and whether the GitHub repo is linked openly or only behind this form (Plan of Execution, open questions 2 and 5).
