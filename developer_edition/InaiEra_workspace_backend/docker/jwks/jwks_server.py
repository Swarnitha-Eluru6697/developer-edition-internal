"""Dev-only control-plane JWKS stub + JWT minter — with a PERSISTENT key.

There is no control plane locally, so every service is configured to validate
JWTs against this stub. It serves JWKS at
http://0.0.0.0:9999/.well-known/jwks.json (reachable from containers as
host.docker.internal:9999) and mints a token whose claims satisfy validate_jwt():

    iss=http://localhost:9999   aud=workspace.inaiera   org_id=local-dev

The RSA key and the minted token live in ~/.inaiera-dev-jwks/ so the dev JWT is
stable across restarts. That path is NOT arbitrary: frontend/vite.config.ts
reads token.txt from it to redirect / -> /#token=<jwt>. Do not relocate it
without changing the Vite plugin too.

Identity on the token can be overridden per developer:
    DEV_JWT_EMAIL, DEV_JWT_NAME, DEV_JWT_SUB, DEV_JWT_ORG

Only dependency is `cryptography` — the RS256 signature is assembled by hand
rather than pulling in python-jose, which lags new Python releases.
"""
import base64
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KID = "dev-key-1"
KEYDIR = os.path.expanduser("~/.inaiera-dev-jwks")
KEY_PEM = os.path.join(KEYDIR, "key.pem")
TOKEN_TXT = os.path.join(KEYDIR, "token.txt")
os.makedirs(KEYDIR, exist_ok=True)



def _prior_claims() -> dict:
    """Identity from the last token we minted, expired or not.

    The subject is the owner key on every workspace, project and rule row. If a
    restart after expiry minted a fresh random sub, the developer would log back
    in to an empty app with all their work still in the database under the old
    id. So carry the previous identity forward; env vars still win.
    """
    try:
        payload = open(TOKEN_TXT).read().strip().split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


_prior = _prior_claims()
# Audience(s) the token is minted for. Default: Workspace only. DEV_JWT_AUD (comma-separated) adds others, e.g. the
# dbt and FlinkSQL runners, so a service that forwards this bearer to them (Data Quality's job lookup) is accepted.
_auds = [a.strip() for a in os.environ.get("DEV_JWT_AUD", "workspace.inaiera").split(",") if a.strip()]
AUD = _auds[0] if len(_auds) == 1 else _auds
# Optional `permissions` claim (comma-separated Workspace permission IDs). The runners authorize on it, not on roles.
PERMISSIONS = [p.strip() for p in os.environ.get("DEV_JWT_PERMISSIONS", "").split(",") if p.strip()]
# Optional space-separated `scope` claim: the runner operations the bearer may be used for.
SCOPE = " ".join(os.environ.get("DEV_JWT_SCOPE", "").split())
ORG_ID = os.environ.get("DEV_JWT_ORG") or _prior.get("org_id") or "local-dev"
EMAIL = os.environ.get("DEV_JWT_EMAIL") or _prior.get("email") or "dev@localhost"
NAME = os.environ.get("DEV_JWT_NAME") or _prior.get("display_name") or "Local Dev"
# Stable subject, derived from the email so it is reproducible on a fresh machine.
SUB = (
    os.environ.get("DEV_JWT_SUB")
    or _prior.get("sub")
    or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{EMAIL}@inaiera-dev"))
)

# Load an existing key so the token stays valid across restarts; else create one.
if os.path.exists(KEY_PEM):
    with open(KEY_PEM, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None)
else:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(KEY_PEM, "wb") as f:
        f.write(
            priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    os.chmod(KEY_PEM, 0o600)

nums = priv.public_key().public_numbers()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_int(n: int) -> str:
    return b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def b64u_json(obj: dict) -> str:
    return b64u(json.dumps(obj, separators=(",", ":")).encode())


JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": KID,
            "n": b64u_int(nums.n),
            "e": b64u_int(nums.e),
        }
    ]
}


def mint_token() -> str:
    now = int(time.time())
    claims = {
        "sub": SUB,
        "org_id": ORG_ID,
        "email": EMAIL,
        "display_name": NAME,
        "roles": ["developer", "admin"],
        "dp_base": "http://localhost:5179",
        "aud": AUD,
        **({"permissions": PERMISSIONS} if PERMISSIONS else {}),
        **({"scope": SCOPE} if SCOPE else {}),
        "iss": "http://localhost:9999",
        "iat": now,
        "exp": now + 60 * 60 * 24 * 90,  # 90 days — dev only
    }
    signing_input = (
        b64u_json({"alg": "RS256", "typ": "JWT", "kid": KID}) + "." + b64u_json(claims)
    ).encode()
    sig = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode() + "." + b64u(sig)


def token_exp(tok: str) -> int:
    payload = tok.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))


# Reuse the stored token if it has more than a day left; else mint fresh.
TOKEN = None
if os.path.exists(TOKEN_TXT):
    try:
        old = open(TOKEN_TXT).read().strip()
        # Reused only while it still names the configured audience(s); otherwise re-minted (same identity).
        if (old and token_exp(old) - time.time() > 86400 and _prior.get("aud") == AUD
                and (_prior.get("permissions") or []) == PERMISSIONS and (_prior.get("scope") or "") == SCOPE):
            TOKEN = old
    except Exception:
        TOKEN = None
if not TOKEN:
    TOKEN = mint_token()
    with open(TOKEN_TXT, "w") as f:
        f.write(TOKEN)

print(f"=== DEV JWT (persistent; {TOKEN_TXT}) ===")
print(TOKEN)
print(f"=== UI: http://localhost:5179/#token={TOKEN} ===")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/.well-known/jwks.json"):
            body = json.dumps(JWKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("JWKS stub on http://0.0.0.0:9999/.well-known/jwks.json")
    HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
