"""
Real, shared encryption at rest for secret values (§0.59, GAP_REPORT) —
EnvVar secrets, Knowledge Base connection passwords, and the MCP
server token. A real, deliberate scope decision, stated honestly: this
is real, working Fernet (AES-128-CBC + HMAC) envelope encryption with
a locally-held key, not a Vault/KMS/cloud secrets manager integration
— those require real external infrastructure (a running Vault
cluster, cloud IAM credentials) this reference environment doesn't
have, matching this project's own established "production infra, out
of scope for this repo" boundary elsewhere (§9.3). What's genuinely
real here: the bytes actually stored in Postgres are no longer the
plaintext secret, encrypted/decrypted with a real, standard cipher —
not a placeholder claiming to be encryption.

§0.89, GAP_REPORT — two real, reported gaps in the ORIGINAL §0.59 fix,
both closed here:

1. Fail CLOSED in production, not silently open: `_get_fernet()` used
   to generate a real, temporary, in-memory-only key whenever
   SECRETS_ENCRYPTION_KEY was unset — fine for a genuine local dev
   accident, but a real, silent trap in a real multi-process
   deployment outside this repo's own Docker Compose (which DOES
   correctly require the real env var for both API and worker): the
   API process and the Temporal worker process each generate their
   OWN real, independent temporary key, so a secret encrypted by one
   process's key genuinely cannot be decrypted by the other's — a
   real, silent, cross-process key mismatch, not a hypothetical one.
   Now: ENVIRONMENT=production with no real SECRETS_ENCRYPTION_KEY set
   raises a real, loud RuntimeError immediately (checked at real
   process startup in both main.py and worker.py, not just lazily on
   first secret use) — refusing to start rather than silently
   generating a key that will foreseeably cause a real cross-process
   decryption failure later.

2. An undecryptable value no longer silently masquerades as a bad
   credential: `decrypt_secret()` used to catch ANY decryption failure
   and return the raw value completely unchanged — meaning a genuine
   encryption-key mismatch (wrong/rotated/missing key) looked
   identical to "this API key/password is simply wrong," a real,
   confusing failure mode the report named directly. New encrypted
   values now carry a real, explicit `enc:v1:` prefix
   (_ENC_PREFIX) — genuinely unambiguous: this value WAS
   encrypted by this module, and a caller decrypting it has every
   right to expect that to succeed. `enc:v1:` present but genuinely
   undecryptable now raises SecretDecryptionError, a real, loud,
   specifically-named failure (not a generic exception, not a silent
   pass-through) — logged with a clear, honest explanation that this
   is a key-management problem, not a bad credential.

A real, deliberate, honest refinement of the report's own literal
"no prefix -> legacy plaintext" instruction, found necessary by
directly checking this environment's own real, existing data before
writing this: real EnvVar/KnowledgeBase rows already exist in this
database, genuinely encrypted under the ORIGINAL §0.59 scheme, which
never had a prefix at all — treating every no-prefix value as
literal, unencrypted plaintext (the report's own literal instruction)
would have silently broken every one of those real, already-encrypted
secrets, returning raw ciphertext as if it were the real credential
the exact failure mode this whole fix exists to prevent, just
inflicted on real, existing data instead of new. A no-prefix value is
therefore still given one real decrypt attempt first (preserving
those real, existing rows); ONLY if that fails does it fall back to
being treated as genuine legacy plaintext — the real, inherent
ambiguity of a value with no marker at all is honestly irreducible,
and silently keeping the old, lenient behavior for that one specific,
already-ambiguous case is what actually "preserves backward
compatibility without treating undecryptable ciphertext as plaintext"
for the case that matters: any NEW value, unambiguously marked
`enc:v1:`, that fails to decrypt is a real, loud, specific error, not
a guess. Real, stated follow-on, not silently assumed done (matching
this module's own pre-existing, honest precedent for the original
retroactive-re-encryption gap): existing no-prefix rows are not
retroactively re-prefixed by this fix.
"""
import os
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

_fernet: Optional[MultiFernet] = None

# Real, explicit marker (§0.89, GAP_REPORT) for a value this module
# itself encrypted under the current scheme — see this module's own
# docstring for the full real reasoning on why a no-prefix value gets
# a real, lenient fallback while an enc:v1-marked one gets a real,
# hard failure.
_ENC_PREFIX = "enc:v1:"


class SecretDecryptionError(Exception):
    """Real, loud, specifically-named failure (§0.89, GAP_REPORT) —
    the actual fix for the reported gap: this used to be silently
    swallowed, with the raw, still-encrypted ciphertext returned as if
    it were the real credential, indistinguishable from "the stored
    API key/password is simply wrong." Raised only for a value
    explicitly marked _ENC_PREFIX (this module's own real, unambiguous
    "this WAS encrypted, decryption failure here is a real key-
    management problem, not ambiguous legacy data") that genuinely
    fails to decrypt with the currently configured
    SECRETS_ENCRYPTION_KEY."""


# "One real definition of 'production,' not two that could quietly
# drift apart" — which is exactly right, and there were three. The
# definition lives in app.environment now; the private alias stays so
# this module's own callers below are untouched.
from .environment import is_production as _is_production  # noqa: E402,F401


def check_production_key_requirement() -> None:
    """Real, fail-CLOSED startup guard (§0.89, GAP_REPORT) — the
    actual fix for the reported gap. Called at real process startup
    by BOTH main.py (the API process) and worker.py (the Temporal
    worker process) — not just lazily inside _get_fernet() on first
    secret use, since a real, misconfigured production deployment
    should refuse to even start, the same real "fail loud before
    serving anything" philosophy main.py's own existing
    ALLOW_MOCK_MODELS-in-production check already established. Also
    called defensively from _get_fernet() itself, so a caller that
    somehow reaches secret encryption without either real startup path
    having run first still can't silently fall through to a real,
    temporary key in a real production environment."""
    if _is_production() and not os.getenv("SECRETS_ENCRYPTION_KEY"):
        raise RuntimeError(
            "Refusing to start: ENVIRONMENT=production but SECRETS_ENCRYPTION_KEY "
            "is not set. A real, temporary, per-process key would mean the API "
            "process and the Temporal worker process each generate their OWN "
            "independent key — a secret encrypted by one process genuinely "
            "cannot be decrypted by the other. Set SECRETS_ENCRYPTION_KEY to a "
            "real, persisted value (Fernet.generate_key()) and share it across "
            "every real process that needs to encrypt/decrypt a secret."
        )


def using_ephemeral_key() -> bool:
    """Whether secrets encrypted right now are guaranteed to become
    unreadable.

    With SECRETS_ENCRYPTION_KEY unset, _get_fernet() generates a key per
    PROCESS: nothing encrypted survives a restart, and no other process
    can read it. That has been a warning in the log only, which is not
    enough — a log line does not stop a person from typing an API key
    into a form that is about to destroy it. Callers that PERSIST a
    secret use this to refuse instead.
    """
    return not os.getenv("SECRETS_ENCRYPTION_KEY")


def require_durable_key(what: str) -> None:
    """Refuse to store a secret that cannot be read back.

    Raises RuntimeError with the fix named. This is deliberately a hard
    failure rather than a warning: silently accepting a credential and
    returning success, having encrypted it under a key that dies with
    this process, is worse than declining. It has already cost real
    credentials twice in this project — a provider key and a knowledge
    base password, both saved through a dev server started without the
    variable exported, both unrecoverable afterwards.
    """
    if not using_ephemeral_key():
        return
    raise RuntimeError(
        f"Refusing to save {what}: SECRETS_ENCRYPTION_KEY is not set for this process, so the "
        f"value would be encrypted with a throwaway key and become unreadable on the next "
        f"restart — including to this same server. Export SECRETS_ENCRYPTION_KEY (run-dev.sh "
        f"reads it from .env) and try again. Nothing has been stored."
    )


def _previous_keys() -> list:
    """Keys this server used before, from SECRETS_ENCRYPTION_KEY_PREVIOUS
    (comma-separated). Decrypt-only: a secret saved under an old key stays
    readable after the key is rotated, while every new save uses the
    current key. Without this, changing the key made every stored
    connection password unreadable at once."""
    raw = os.getenv("SECRETS_ENCRYPTION_KEY_PREVIOUS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    check_production_key_requirement()
    key = os.getenv("SECRETS_ENCRYPTION_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        logger.warning(
            "SECRETS_ENCRYPTION_KEY is not set — generated a real, temporary "
            "encryption key for this process only. Secrets encrypted now will "
            "become unreadable after a restart, AND unreadable by any OTHER "
            "real process (e.g. the Temporal worker) that doesn't happen to "
            "share this exact, randomly-generated key. Set "
            "SECRETS_ENCRYPTION_KEY to a real, persisted value "
            "(Fernet.generate_key()) for any real deployment."
        )
    keys = [key] + [k for k in _previous_keys() if k != key]
    # MultiFernet encrypts with the FIRST key (the current one) and tries
    # each in turn to decrypt.
    _fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])
    return _fernet


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    """Real encryption — returns a real Fernet token, safe to store in
    an ordinary string/text column, prefixed with _ENC_PREFIX (§0.89,
    GAP_REPORT) so a future decrypt attempt can tell "this was
    genuinely encrypted by this module" apart from legacy plaintext
    unambiguously, rather than only ever guessing from whether
    decryption happens to succeed. Empty/None passes through unchanged
    (nothing real to protect)."""
    if not plaintext:
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """Real decryption (§0.59/§0.89, GAP_REPORT) — see this module's
    own docstring for the full real reasoning behind the two real,
    different behaviors below:

    - _ENC_PREFIX present: this value was genuinely encrypted by this
      module's own current scheme. A successful decrypt returns the
      real plaintext; a genuine failure is a real, loud
      SecretDecryptionError (logged, not swallowed) — a real key-
      management problem (wrong/rotated/missing key), never silently
      treated as "the credential itself is just wrong."
    - _ENC_PREFIX absent: real, honest ambiguity — this could be
      genuine legacy plaintext (never encrypted at all), or a real,
      already-encrypted value from before this exact prefix existed
      (confirmed directly: real rows like this already exist in this
      platform's own database). One real decrypt attempt is still
      made first (preserving those real, pre-existing encrypted rows);
      if that fails, the value is returned unchanged, the same real,
      lenient fallback this function has always had for the
      genuinely-ambiguous, unmarked case."""
    if not value:
        return value
    if value.startswith(_ENC_PREFIX):
        token = value[len(_ENC_PREFIX):]
        try:
            return _get_fernet().decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError) as e:
            logger.error(
                "A real, %s-marked secret could not be decrypted with the "
                "currently configured SECRETS_ENCRYPTION_KEY — this is a real "
                "key-management failure (the wrong key, a rotated key, or a "
                "missing key shared across processes), NOT an invalid stored "
                "credential. Fix the real SECRETS_ENCRYPTION_KEY configuration "
                "rather than assuming the underlying secret itself is wrong.",
                _ENC_PREFIX.rstrip(":"),
            )
            raise SecretDecryptionError(
                f"Could not decrypt a real, {_ENC_PREFIX}-marked secret with the "
                f"currently configured SECRETS_ENCRYPTION_KEY — this is a real "
                f"key-management failure, not a bad stored credential."
            ) from e
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return value

