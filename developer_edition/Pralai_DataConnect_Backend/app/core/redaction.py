"""
Redaction of free text that may carry secrets or sensitive detail (BUG-11).

A database, HTTP or Kafka driver's exception text can embed a username, a password in a connection string, a bearer
token, the SQL it ran (with literal values), a file path, or the request payload. Two levels, for two audiences:

  redact_secrets     — credentials only. Safe to show a user who is troubleshooting their own connection: the host,
                       database and statement are left readable.
  redact_diagnostic  — everything redact_secrets does, plus user names, SQL literal values, e-mail addresses and the
                       directory part of file paths. This is what a diagnostic LOG entry gets (app/core/logging.py
                       applies it to the usual error-text keys, so no call site has to remember). Host names stay: an
                       operator needs them to debug, and the log is a controlled audience, not a browser tab.

Neither is a substitute for not persisting raw exception text in the first place — what a caller reads back from a
stored field is a SafeError's fixed message (app/services/safe_error.py). This is the net under that, and what makes the
detailed log safe to keep.
"""

import re

MASK = "********"

# scheme://user:password@host  ->  scheme://********:********@host
_CREDENTIAL_URI_RE = re.compile(r"(://)([^/:@\s]+):([^/:@\s]*)(@)")

# Any non-space run, not just the base64url alphabet: an opaque token is whatever the issuer made it, and a charset allow-list leaves the
# tokens it does not list (a "*", a "!") readable. Stops at quote/comma/bracket so surrounding text survives.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s\"',;)}\]]{6,}")

# password=..., "password": "...", api_key: ..., Authorization: ..., also inside a URL query (?user=a&password=b).
_SECRET_KEY = (
    r"password|passwd|pwd|passphrase|(?:client[_-]?)?secret|(?:[a-z]+[_-])?token|api[_-]?key|apikey|"
    r"(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?|private[_-]?key|authorization|sas[_-]?token"
)
_SECRET_KV_RE = re.compile(rf"(?i)(\b(?:{_SECRET_KEY})\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&;,)}}\]]+)")

# Identity keys: not secret on their own, but a user name has no business in a stored or logged error.
_USER_KV_RE = re.compile(r"(?i)(\b(?:user(?:name)?|uid|login)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&;,)}\]]+)")
_USER_QUOTED_RE = re.compile(r"(?i)(\b(?:for\s+)?user\s+[\"'])([^\"']+)([\"'])")

_SQL_KEYWORD_RE = re.compile(r"(?i)\b(select|insert|update|delete|merge|where|values|from|into)\b")
_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_NUMERIC_COMPARE_RE = re.compile(r"(?i)(=|<>|!=|<|>|\bin\s*\(|\blike\s)\s*\d[\d.]*")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_UNIX_PATH_RE = re.compile(r"(?<![\w:/.@~-])(?:/[\w.@+~-]+){2,}")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"']+\\)+[^\\\s\"']*")

_DIAGNOSTIC_MAX_LENGTH = 2000


# A Slack / Microsoft Teams webhook URL is itself the credential (anyone holding it can post into the channel): keep the host, mask the path.
# The hosts are the ones app/services/alerts/webhooks.py accepts. httpx logs every request URL, so this also covers its log lines.
_WEBHOOK_URL_RE = re.compile(
    r"(?i)(https?://(?:hooks\.slack(?:-gov)?\.com|[\w.-]*webhook\.office\.com|outlook\.office(?:365)?\.com|[\w.-]+\.logic\.azure\.com"
    r"|[\w.-]+\.powerplatform\.com))(?::\d+)?/[^\s\"'<>]*"
)


def _mask_value(match: re.Match[str]) -> str:
    return f"{match.group(1)}{MASK}"


def redact_secrets(text: str | None) -> str:
    """Credentials out, everything else left readable."""
    if not text:
        return ""
    text = _CREDENTIAL_URI_RE.sub(rf"\1{MASK}:{MASK}\4", text)
    text = _BEARER_RE.sub(rf"\1\2{MASK}", text)
    text = _WEBHOOK_URL_RE.sub(rf"\1/{MASK}", text)
    return _SECRET_KV_RE.sub(_mask_value, text)


def _basename(match: re.Match[str]) -> str:
    path = match.group(0)
    separator = "\\" if "\\" in path else "/"
    return f"…{separator}{path.rstrip(separator).rsplit(separator, 1)[-1]}"


def redact_diagnostic(text: str | None) -> str:
    """The version that is safe to keep in a diagnostic log: see this module's docstring."""
    if not text:
        return ""
    text = redact_secrets(text)
    text = _USER_KV_RE.sub(_mask_value, text)
    text = _USER_QUOTED_RE.sub(rf"\1{MASK}\3", text)
    if _SQL_KEYWORD_RE.search(text):
        text = _QUOTED_LITERAL_RE.sub("'?'", text)
        text = _NUMERIC_COMPARE_RE.sub(lambda m: f"{m.group(1)} ?", text)
    text = _EMAIL_RE.sub(f"{MASK}@{MASK}", text)
    text = _WINDOWS_PATH_RE.sub(_basename, text)
    text = _UNIX_PATH_RE.sub(_basename, text)
    if len(text) > _DIAGNOSTIC_MAX_LENGTH:
        text = text[:_DIAGNOSTIC_MAX_LENGTH] + "…"
    return text
