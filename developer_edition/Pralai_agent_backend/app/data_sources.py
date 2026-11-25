"""Browsing and fetching documents from external data sources.

Every source exposes the same two operations so the Documents tab needs
one UI rather than six:

    list_children(source, config, path) -> [Entry]   folders and files
    fetch_file(source, config, file_id) -> (filename, bytes)

`path` is opaque to the caller — each adapter decides what it means (a
repo tree path for GitHub, a drive item id for Graph, an object prefix
for GCS). The UI only ever echoes back the `path` an entry carried, so
arbitrarily deep nesting works without the frontend knowing anything
about a given source's addressing.

Auth uses what the Integrations page already collects, which is why no
redirect flow is needed:

  * SharePoint / OneDrive / Google Drive-as-service-account — the
    client_credentials grant with client_id + client_secret (+ tenant_id
    for Microsoft). This yields an app-only token; the app must hold
    application permissions, since there is no signed-in user.
  * GitHub / Notion — the token pasted into `credentials`.
  * Google Cloud Storage / Google Drive — a service-account JSON in
    `credentials`, signed into a bearer token via JWT.

Nothing here is verified against a live tenant: no credentials for any
of these sources exist in this environment. The request shapes follow
each vendor's documented API, and every failure is surfaced with the
provider's own status and body rather than being swallowed, so a wrong
credential says so plainly on the first browse.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import base64
import json
import time

import httpx

from . import transcription

# A DENYLIST of extensions with no text layer, not an allowlist of
# permitted ones — because ingestion.parse_file already falls through to
# a best-effort decode for any unrecognised extension, and rejects
# genuinely binary content there with a message naming the file.
#
# The allowlist this replaces was narrower than the pipeline itself: a
# repository of .sql files showed every one greyed out as "no text
# extractor for this type", and a selected folder expanded to zero
# files, when SQL is plain text the pipeline reads without complaint.
# Any allowlist of text formats is a list someone must remember to
# extend; the set of things that genuinely have no text is small,
# stable, and safe to enumerate instead.
#
# Office legacy binaries (.doc/.xls/.ppt) are deliberately absent from
# this list: the container really does carry antiword/catdoc/xls2csv/
# catppt, so those extract.
NO_TEXT_LAYER = {
    # images
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "ico", "heic", "heif", "raw",
    # Audio and video. Both genuinely have no text layer, which is what
    # this set means — but audio in a format transcription.py can send
    # to a speech provider is still offered by _is_ingestible below,
    # because it can be turned into text even though none is in the
    # bytes. Video stays refused: there is no video path here.
    "mp3", "m4a", "aac", "flac", "ogg", "oga", "opus", "wma", "wav", "aiff",
    "mp4", "m4v", "mov", "avi", "mkv", "webm", "wmv", "flv", "mpg", "mpeg",
    # archives and disk images
    "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "iso", "dmg", "pkg", "jar", "war",
    # compiled or opaque binaries
    "exe", "dll", "so", "dylib", "bin", "o", "a", "class", "pyc", "pyo", "wasm",
    # fonts
    "ttf", "otf", "woff", "woff2", "eot",
    # binary design and data files
    "psd", "ai", "sketch", "fig", "xd", "blend", "db", "sqlite", "sqlite3", "mdb", "accdb",
    "parquet", "avro", "orc", "pdb", "pkl", "npy", "npz", "h5", "hdf5", "safetensors", "onnx",
}


@dataclass
class Entry:
    """One row in the browser. `is_dir` decides whether clicking it
    descends (via `path`) or selects it for ingest (via `id`)."""
    id: str
    name: str
    is_dir: bool
    path: str = ""
    size: Optional[int] = None
    mime: Optional[str] = None
    ingestible: bool = True
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "is_dir": self.is_dir, "path": self.path,
                "size": self.size, "mime": self.mime, "ingestible": self.ingestible,
                "note": self.note}


# Set by an adapter when the listing itself needs explaining. The
# OneDrive root is the case that prompted it: app-only auth has no
# signed-in user, so it lists EVERY drive the app was consented to —
# which surprises whoever registered the app from their own account and
# expected to see only their own files.
LAST_NOTICE: Dict[str, str] = {}


class DataSourceError(RuntimeError):
    pass


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


# Excluded by NAME rather than extension, for two different reasons.
#
# Noise: OS metadata files appear in nearly every folder someone picks
# and are binary anyway, so offering them only produces failures.
#
# Secrets: a .env or credentials file is plain text, so the denylist
# above would happily offer it — and ingesting it would copy live
# credentials into a vector store, where they would then come back in
# retrieval results. Refusing by default is the right trade; anyone who
# genuinely wants such a file can rename it.
_EXCLUDED_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".localized",
    ".env", ".env.local", ".env.production", ".env.development", ".env.test",
    "credentials", "credentials.json", "id_rsa", "id_ed25519", ".netrc", ".pgpass",
    ".htpasswd", "secrets.yaml", "secrets.yml",
}


def _is_ingestible(name: str) -> bool:
    """Whether this file is worth offering. Errs towards offering it:
    the pipeline's own binary check is accurate and names the file when
    it refuses, whereas a browser that hides a file explains nothing.

    Audio is the one exception to the denylist: it has no text layer, but
    ingestion.parse_file transcribes it, so it is offered whenever a
    speech provider is actually configured — and only then. Offering a
    .wav with no key behind it would fail at the end of the upload
    instead of being explained here.
    """
    lowered = (name or "").lower()
    if lowered in _EXCLUDED_NAMES or lowered.startswith(".env."):
        return False
    ext = _ext(name)
    if ext not in NO_TEXT_LAYER:
        return True
    if ext in transcription.AUDIO_EXTENSIONS:
        return transcription.availability().available
    return False


def file_note(name: str) -> str:
    """The one-line explanation shown beside a file in the browser.
    Empty when there is nothing to explain."""
    if _ext(name) in transcription.AUDIO_EXTENSIONS:
        if transcription.availability().available:
            return "audio — transcribed on ingest"
        return "audio — add an OpenAI or Gemini key to transcribe it"
    return "" if _is_ingestible(name) else "no text to extract (binary format)"


def _check(r: httpx.Response, what: str) -> Any:
    if r.status_code >= 400:
        raise DataSourceError(f"{what} failed ({r.status_code}): {r.text[:220]}")
    return r.json() if r.content else {}


# ------------------------------------------------------------------ GitHub
def _github_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    token = cfg.get("credentials")
    if not token:
        raise DataSourceError("No GitHub token stored — connect GitHub on the Integrations page.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _github_list(cfg: Dict[str, Any], path: str) -> List[Entry]:
    """path is "" (repos), "owner/repo" (repo root) or
    "owner/repo/some/dir" (a directory inside it)."""
    h = _github_headers(cfg)
    with httpx.Client(timeout=45) as c:
        if not path:
            repos = _check(c.get("https://api.github.com/user/repos",
                                 headers=h, params={"per_page": 100, "sort": "updated"}),
                           "GitHub list repositories")
            return [Entry(id=r["full_name"], name=r["full_name"], is_dir=True, path=r["full_name"])
                    for r in repos]
        parts = path.split("/")
        if len(parts) < 2:
            raise DataSourceError(f"'{path}' is not a repository path (expected owner/repo).")
        owner, repo = parts[0], parts[1]
        inner = "/".join(parts[2:])
        items = _check(c.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{inner}",
                             headers=h), f"GitHub list {path}")
    if isinstance(items, dict):
        items = [items]
    out: List[Entry] = []
    for it in items:
        is_dir = it.get("type") == "dir"
        full = f"{owner}/{repo}/{it['path']}"
        out.append(Entry(
            id=full, name=it["name"], is_dir=is_dir,
            path=full if is_dir else "",
            size=it.get("size"),
            ingestible=True if is_dir else _is_ingestible(it["name"]),
            note="" if is_dir else file_note(it["name"]),
        ))
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def _github_fetch(cfg: Dict[str, Any], file_id: str) -> Tuple[str, bytes]:
    parts = file_id.split("/")
    owner, repo, inner = parts[0], parts[1], "/".join(parts[2:])
    with httpx.Client(timeout=120) as c:
        meta = _check(c.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{inner}",
                            headers=_github_headers(cfg)), f"GitHub fetch {file_id}")
    if meta.get("encoding") == "base64" and meta.get("content"):
        return meta["name"], base64.b64decode(meta["content"])
    # Files over 1 MB come back without inline content.
    dl = meta.get("download_url")
    if not dl:
        raise DataSourceError(f"GitHub returned no content for '{file_id}'.")
    with httpx.Client(timeout=180, follow_redirects=True) as c:
        r = c.get(dl, headers=_github_headers(cfg))
        if r.status_code >= 400:
            raise DataSourceError(f"GitHub download failed ({r.status_code}).")
        return meta["name"], r.content


# ------------------------------------------------- Microsoft Graph (app-only)
def _graph_token(cfg: Dict[str, Any]) -> str:
    """client_credentials against the tenant. This is why no redirect
    flow is needed — but it is an APP token, so the registered app needs
    application permissions (e.g. Files.Read.All / Sites.Read.All) and
    admin consent. A delegated-only app returns a token that then 403s
    on every item call, which surfaces as such on the first browse."""
    cid, secret = cfg.get("client_id"), cfg.get("client_secret")
    tenant = cfg.get("tenant_id") or "organizations"
    if not cid or not secret:
        raise DataSourceError("Client ID and client secret are required for this source.")
    with httpx.Client(timeout=45) as c:
        tok = _check(c.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"client_id": cid, "client_secret": secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"},
        ), "Microsoft token request")
    if "access_token" not in tok:
        raise DataSourceError(f"Microsoft token response had no access_token: {str(tok)[:180]}")
    return tok["access_token"]


def _jwt_claims(token: str) -> Dict[str, Any]:
    """The claims inside an access token we just obtained ourselves.

    Read for DIAGNOSTICS only, never for a security decision: the
    signature is not verified, because the only consumer is the error
    message below and the issuer is the endpoint we called a moment ago.

    What makes this worth doing: an app-only token carries a `roles`
    claim listing the APPLICATION permissions actually consented. When
    Graph rejects a call, that list is the difference between "your
    credentials are wrong" (they are not) and "this app has no
    SharePoint permission granted" (it has not) — a distinction the
    provider's own 401 body does not make.
    """
    import base64
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# The application permissions Graph needs for each source, app-only.
_GRAPH_REQUIRED_ROLES = {
    "SharePoint": ("Sites.Read.All", "Files.Read.All"),
    # Personal OneDrives are enumerated as SharePoint sites on the "-my"
    # host, so Sites.Read.All suffices; User.Read.All is only needed for
    # the /users fallback, and is not demanded up front.
    "OneDrive": ("Sites.Read.All", "Files.Read.All"),
}


def _graph_authz_error(source: str, token: str, status: int, body: str) -> DataSourceError:
    """An authorization failure explained in terms of what is
    missing, rather than repeating Microsoft's own opaque body.

    A 401 carrying an spException on /sites/{hostname} is the standard
    signature of an app registration that has no consented SharePoint
    application permission — the token itself is valid, which is why the
    message says so explicitly instead of sending someone back to
    re-check a client secret that is fine.
    """
    claims = _jwt_claims(token)
    granted = claims.get("roles") or []
    required = _GRAPH_REQUIRED_ROLES.get(source, ())
    missing = [r for r in required if r not in granted]
    app_name = claims.get("app_displayname") or claims.get("appid") or "this app"
    tenant = claims.get("tid") or "your tenant"

    lines = [f"Microsoft rejected the request ({status})."]
    if not granted:
        lines.append(
            f"The token for {app_name} carries NO application permissions at all "
            f"(its 'roles' claim is empty). Client ID, secret and tenant are therefore fine — "
            f"the app simply has nothing granted."
        )
    else:
        lines.append(f"Granted application permissions: {', '.join(granted)}.")
    if missing:
        lines.append(
            f"{source} needs {' and '.join(missing)} as APPLICATION permissions "
            f"(not delegated), with admin consent granted."
        )
    if source == "OneDrive" and "User.Read.All" in missing:
        lines.append(
            "Alternatively, set this source's 'OneDrive user' field to one email address: that "
            "addresses their drive directly and needs no directory access at all."
        )
    lines.append(
        f"In the Azure portal: Entra ID > App registrations > {app_name} > API permissions > "
        f"Add a permission > Microsoft Graph > Application permissions, then 'Grant admin "
        f"consent for {tenant}'. Delegated permissions do not apply here — there is no signed-in "
        f"user in a client-credentials flow."
    )
    lines.append(f"Microsoft's own response: {body[:200]}")
    return DataSourceError(" ".join(lines))


def _graph_get(c: httpx.Client, url: str, headers: Dict[str, str], what: str,
               source: str, token: str) -> Any:
    """A Graph GET whose 401/403 is explained, not just relayed."""
    r = c.get(url, headers=headers)
    if r.status_code in (401, 403):
        raise _graph_authz_error(source, token, r.status_code, r.text)
    return _check(r, what)


def _graph_paths(source: str, cfg: Dict[str, Any], token: str) -> str:
    """The SharePoint site to browse, resolved from the subdomain the
    Integrations form collects. SharePoint only."""
    sub = cfg.get("sharepoint_subdomain")
    if not sub:
        raise DataSourceError(
            "A SharePoint/tenant subdomain is required (e.g. 'contoso' for "
            "contoso.sharepoint.com)."
        )
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=45) as c:
        site = _graph_get(c, f"https://graph.microsoft.com/v1.0/sites/{sub}.sharepoint.com",
                          h, "Graph resolve site", source, token)
    return site["id"]


# Path encoding for the Graph sources. Everything resolves to a driveId
# and an optional itemId, because /drives/{id}/items/{id} addresses a
# personal OneDrive and a SharePoint document library identically:
#
#   ""                       root listing (users for OneDrive, libraries
#                            for SharePoint)
#   "user:<userId>"          that person's OneDrive, top level
#   "drive:<driveId>"        that library/drive, top level
#   "drive:<driveId>|<item>" a folder inside it
#
# This replaced addressing a SITE drive for OneDrive, which was wrong in
# a way that looked like success: the call returned 200 and zero items,
# because a team site's "Documents" library is not where anyone's
# personal OneDrive files live. Empty and broken were indistinguishable.


def _graph_split(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    """(kind, id, item_id) for a browse path — see the encoding above."""
    if not path:
        return "root", None, None
    if path.startswith("user:"):
        return "user", path[len("user:"):], None
    if path.startswith("site:"):
        return "site", path[len("site:"):], None
    if path.startswith("drive:"):
        rest = path[len("drive:"):]
        drive_id, sep, item_id = rest.partition("|")
        return "drive", drive_id, (item_id or None) if sep else None
    # A bare id is a drive, for tolerance of older saved paths.
    return "drive", path, None


def _graph_entries(items: List[Dict[str, Any]], drive_id: str) -> List[Entry]:
    out: List[Entry] = []
    for it in items:
        is_dir = "folder" in it
        name = it.get("name", "")
        out.append(Entry(
            id=f"drive:{drive_id}|{it['id']}",
            name=name, is_dir=is_dir,
            path=f"drive:{drive_id}|{it['id']}" if is_dir else "",
            size=it.get("size"),
            mime=(it.get("file") or {}).get("mimeType"),
            ingestible=True if is_dir else _is_ingestible(name),
            note="" if is_dir else file_note(name),
        ))
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def _graph_list(cfg: Dict[str, Any], source: str, path: str) -> List[Entry]:
    token = _graph_token(cfg)
    h = {"Authorization": f"Bearer {token}"}
    kind, ident, item_id = _graph_split(path)

    with httpx.Client(timeout=60) as c:
        if kind == "root":
            if source == "OneDrive":
                # Personal OneDrive is per USER (/users/{id}/drive), never
                # per site. Naming one user addresses their drive
                # directly, which avoids needing User.Read.All at all —
                # enumerating people is only necessary when browsing
                # everyone's. Worth offering: User.Read.All reads the
                # whole directory, which is a lot of access to grant for
                # the sake of reaching one person's files.
                only = (cfg.get("onedrive_user") or "").strip()
                if only:
                    drive = _graph_get(
                        c, f"https://graph.microsoft.com/v1.0/users/{only}/drive",
                        h, f"Graph resolve {only}'s drive", source, token)
                    data = _graph_get(
                        c, f"https://graph.microsoft.com/v1.0/drives/{drive['id']}/root/children",
                        h, "Graph list drive root", source, token)
                    return _graph_entries(data.get("value", []), drive["id"])

                # Every personal OneDrive is ALSO a SharePoint site, on
                # the tenant's "-my" host under /personal/. Enumerating
                # those needs only Sites.Read.All, which any app browsing
                # SharePoint already has — whereas /users needs
                # User.Read.All, i.e. read access to the whole directory.
                # Tried first for that reason: the same result for
                # considerably less privilege.
                r = c.get("https://graph.microsoft.com/v1.0/sites/getAllSites", headers=h)
                if r.status_code == 200:
                    personal = [
                        site for site in r.json().get("value", [])
                        if "-my.sharepoint.com/personal/" in (site.get("webUrl") or "")
                    ]
                    if personal:
                        LAST_NOTICE["OneDrive"] = (
                            f"{len(personal)} OneDrive account(s). This is app-only access: the "
                            f"token carries no signed-in user (idtyp=app), so it shows every "
                            f"drive the app was granted — not just the account that registered "
                            f"it. To browse one person's files only, set this source's "
                            f"'OneDrive user' field. To narrow what the app can reach at all, "
                            f"replace the tenant-wide Files/Sites roles with Sites.Selected and "
                            f"grant only the sites you intend."
                        )
                        return [
                            Entry(id=f"site:{site['id']}",
                                  name=site.get("displayName") or site.get("name") or "OneDrive",
                                  is_dir=True, path=f"site:{site['id']}",
                                  note=(site.get("webUrl") or "").split("/personal/")[-1])
                            for site in sorted(
                                personal,
                                key=lambda x: (x.get("displayName") or x.get("name") or "").lower())
                        ]

                # Falls back to the directory only when the cheaper route
                # found nothing, so the stronger permission is needed only
                # where it is genuinely the sole option.
                data = _graph_get(
                    c, "https://graph.microsoft.com/v1.0/users"
                       "?$select=id,displayName,userPrincipalName&$top=100",
                    h, "Graph list users", source, token)
                people = data.get("value", [])
                if not people:
                    raise DataSourceError(
                        "No users are visible to this app. OneDrive is addressed per user, so "
                        "either grant User.Read.All (application) to browse everyone's drive, or "
                        "set this source's 'OneDrive user' to one email address to go straight "
                        "to that person's files without directory access."
                    )
                return [Entry(id=f"user:{u['id']}", name=u.get("displayName")
                              or u.get("userPrincipalName") or u["id"],
                              is_dir=True, path=f"user:{u['id']}",
                              note=u.get("userPrincipalName") or "")
                        for u in people]
            # SharePoint: list the site's document libraries rather than
            # assuming one default drive — the -my host has none at all,
            # which is why /sites/{id}/drive returned 404 there.
            site_id = _graph_paths(source, cfg, token)
            data = _graph_get(c, f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
                              h, "Graph list document libraries", source, token)
            drives = data.get("value", [])
            if not drives:
                raise DataSourceError(
                    f"This site has no document libraries. If the subdomain ends in '-my' it is "
                    f"the personal-OneDrive host, which has none — use the OneDrive source for "
                    f"those, and the plain tenant subdomain here."
                )
            return [Entry(id=f"drive:{d['id']}", name=d.get("name") or "Documents",
                          is_dir=True, path=f"drive:{d['id']}",
                          note=d.get("driveType") or "")
                    for d in drives]

        if kind in ("user", "site"):
            owner = ("users" if kind == "user" else "sites")
            drive = _graph_get(c, f"https://graph.microsoft.com/v1.0/{owner}/{ident}/drive",
                               h, f"Graph resolve {kind} drive", source, token)
            data = _graph_get(c, f"https://graph.microsoft.com/v1.0/drives/{drive['id']}/root/children",
                              h, "Graph list drive root", source, token)
            return _graph_entries(data.get("value", []), drive["id"])

        url = (f"https://graph.microsoft.com/v1.0/drives/{ident}/items/{item_id}/children"
               if item_id else
               f"https://graph.microsoft.com/v1.0/drives/{ident}/root/children")
        data = _graph_get(c, url, h, "Graph list children", source, token)
        return _graph_entries(data.get("value", []), ident)


def _graph_fetch(cfg: Dict[str, Any], source: str, file_id: str) -> Tuple[str, bytes]:
    token = _graph_token(cfg)
    h = {"Authorization": f"Bearer {token}"}
    kind, drive_id, item_id = _graph_split(file_id)
    if kind != "drive" or not item_id:
        raise DataSourceError(f"'{file_id}' is not a file id from this source's browser.")
    base = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
    with httpx.Client(timeout=60) as c:
        meta = _graph_get(c, base, h, "Graph item metadata", source, token)
    with httpx.Client(timeout=300, follow_redirects=True) as c:
        r = c.get(f"{base}/content", headers=h)
        if r.status_code >= 400:
            raise DataSourceError(f"Graph download failed ({r.status_code}): {r.text[:180]}")
        return meta.get("name", item_id), r.content


# ----------------------------------------------------- Google (service account)
def _google_token(cfg: Dict[str, Any], scope: str) -> str:
    """Signs a JWT with the service-account key and exchanges it. Uses
    `cryptography`, already a dependency, rather than pulling in
    google-auth for one signature."""
    raw = cfg.get("credentials")
    if not raw:
        raise DataSourceError("A service-account JSON key is required for this source.")
    try:
        sa = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        raise DataSourceError("Credentials must be the service-account JSON key.")
    for k in ("client_email", "private_key"):
        if k not in sa:
            raise DataSourceError(f"Service-account key is missing '{k}'.")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    claim = {"iss": sa["client_email"], "scope": scope,
             "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}
    if cfg.get("impersonate_subject"):
        # Domain-wide delegation, needed to see a real user's Drive.
        claim["sub"] = cfg["impersonate_subject"]
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=")
    signing_input = b64({"alg": "RS256", "typ": "JWT"}) + b"." + b64(claim)
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")
    with httpx.Client(timeout=45) as c:
        r = c.post("https://oauth2.googleapis.com/token",
                   data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                         "assertion": assertion.decode()})
    if r.status_code >= 400:
        body = r.text[:400]
        subject = claim.get("sub")
        # Google's own message for "delegation was never authorised" is
        # `invalid_grant: Invalid email or User ID`, which sends people
        # to check the address they typed — the address is almost always
        # fine, and the real cause is that this service account's client
        # ID has no domain-wide delegation for this scope. Say that, and
        # say what to paste where, rather than passing the misdirection
        # straight through.
        if subject and ("invalid_grant" in body or "unauthorized_client" in body):
            raise DataSourceError(
                f"Google refused to let this service account act as {subject}. Domain-wide "
                f"delegation is not authorised for it yet: in the Google Workspace Admin console, "
                f"Security → Access and data control → API controls → Domain-wide delegation, add "
                f"client ID {sa.get('client_id', '(the client_id in the key)')} with the scope "
                f"{scope}. Then check that {subject} is a real user in that Workspace. "
                f"(Google's own words: {body})"
            )
        raise DataSourceError(f"Google token request failed ({r.status_code}): {body}")
    tok = r.json() if r.content else {}
    if "access_token" not in tok:
        raise DataSourceError(f"Google token response had no access_token: {str(tok)[:180]}")
    return tok["access_token"]


def _gdrive_service_account_email(cfg: Dict[str, Any]) -> str:
    """The identity this source actually authenticates as — which is not
    the person who created the key, and is the single most useful thing
    to tell someone whose Drive looks empty."""
    raw = cfg.get("credentials")
    try:
        sa = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return sa.get("client_email", "")
    except Exception:
        return ""


def _gdrive_entry(f: Dict[str, Any], *, shared_drive: bool = False) -> Entry:
    is_dir = shared_drive or f.get("mimeType") == "application/vnd.google-apps.folder"
    name = f.get("name", "")
    # A Google Doc has no file bytes; it must be exported, so it is
    # listed as ingestible with the export handled at fetch time.
    native = str(f.get("mimeType", "")).startswith("application/vnd.google-apps.") and not is_dir
    owner = ""
    owners = f.get("owners") or []
    if owners and isinstance(owners, list):
        owner = (owners[0] or {}).get("emailAddress", "")
    return Entry(
        id=f["id"], name=name, is_dir=is_dir,
        path=f["id"] if is_dir else "",
        size=int(f["size"]) if f.get("size") else None,
        mime="application/vnd.google-apps.folder" if shared_drive else f.get("mimeType"),
        ingestible=True if (is_dir or native) else _is_ingestible(name),
        note=("shared drive" if shared_drive
              else "exported as text" if native
              else ("" if is_dir else file_note(name))),
    )


def _gdrive_query(c: httpx.Client, token: str, params: Dict[str, Any], what: str) -> List[Dict[str, Any]]:
    data = _check(c.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"fields": "files(id,name,mimeType,size,owners(emailAddress))",
                                "pageSize": 200, "supportsAllDrives": "true",
                                "includeItemsFromAllDrives": "true", **params}),
                 what)
    return data.get("files", [])


def _gdrive_list(cfg: Dict[str, Any], path: str) -> List[Entry]:
    """What this source can actually see, which for a service account is
    not "your Google Drive".

    A service-account key authenticates as its OWN identity
    (…@….iam.gserviceaccount.com) with its OWN, empty Drive. Listing
    `'root' in parents` therefore returned zero items for a correctly
    configured source whose owner could see plenty of files in their
    browser — the files are in a DIFFERENT account's Drive, and nothing
    in the listing said so.

    The root listing now covers every way a service account can legally
    reach a file:

      * shared drives it is a member of (drives.list),
      * files and folders shared WITH it (`sharedWithMe`), which is what
        happens when someone shares a folder with its email — the normal
        route for a personal Google account, and previously invisible
        here because shared items are not children of `root`,
      * its own root, which is also the real user's root when
        `impersonate_subject` is set (Workspace domain-wide delegation).

    When all three are empty the source is not broken and neither are
    the credentials: nothing has been shared with this identity yet, so
    the listing says so and names the address to share with.
    """
    token = _google_token(cfg, "https://www.googleapis.com/auth/drive.readonly")
    out: List[Entry] = []
    seen: set = set()

    def add(entry: Entry) -> None:
        if entry.id not in seen:
            seen.add(entry.id)
            out.append(entry)

    with httpx.Client(timeout=60) as c:
        if path:
            # Inside a folder — or inside a shared drive, whose id IS its
            # root folder id, so one query covers both.
            for f in _gdrive_query(c, token, {"q": f"'{path}' in parents and trashed=false"},
                                   f"Drive list {path}"):
                add(_gdrive_entry(f))
            out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return out

        drives = _check(c.get("https://www.googleapis.com/drive/v3/drives",
                              headers={"Authorization": f"Bearer {token}"},
                              params={"pageSize": 100}),
                        "Drive shared-drive list").get("drives", [])
        for d in drives:
            add(_gdrive_entry({"id": d["id"], "name": d.get("name", d["id"]),
                               "mimeType": "application/vnd.google-apps.folder"},
                              shared_drive=True))

        for f in _gdrive_query(c, token, {"q": "sharedWithMe and trashed=false"},
                               "Drive shared-with-me list"):
            entry = _gdrive_entry(f)
            # Google keeps "Shared with me" as its own section rather
            # than inside My Drive, and with impersonation both appear
            # here — so a shared folder says where it came from instead
            # of looking like one of the user's own. Files keep their
            # ingestibility note, which is the more useful thing to say
            # about them.
            if entry.is_dir and not entry.note:
                entry.note = "shared with this account"
            add(entry)

        for f in _gdrive_query(c, token, {"q": "'root' in parents and trashed=false"},
                               "Drive list root"):
            add(_gdrive_entry(f))

    if not out:
        email = _gdrive_service_account_email(cfg)
        subject = cfg.get("impersonate_subject")
        if subject:
            LAST_NOTICE["Google Drive"] = (
                f"Nothing visible while impersonating {subject}. Domain-wide delegation is "
                f"configured for this key, so check that {subject} is a real user in your "
                f"Workspace domain and that the delegation grants "
                f"https://www.googleapis.com/auth/drive.readonly."
            )
        else:
            LAST_NOTICE["Google Drive"] = (
                f"This source signs in as the service account "
                f"{email or 'in the uploaded key'} — a separate identity with its own, empty "
                f"Drive. It cannot see your personal Drive until you give it access: open the "
                f"folder in Google Drive, Share, and add {email or 'that address'} as a Viewer. "
                f"Shared folders appear here immediately. (For a Google Workspace domain, the "
                f"alternative is domain-wide delegation plus this source's "
                f"'impersonate_subject' field, which makes it act as one of your users.)"
            )
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def _gdrive_fetch(cfg: Dict[str, Any], file_id: str) -> Tuple[str, bytes]:
    token = _google_token(cfg, "https://www.googleapis.com/auth/drive.readonly")
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60) as c:
        meta = _check(c.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                            headers=h, params={"fields": "id,name,mimeType",
                                               "supportsAllDrives": "true"}),
                      "Drive metadata")
    name, mime = meta.get("name", file_id), meta.get("mimeType", "")
    with httpx.Client(timeout=300, follow_redirects=True) as c:
        if mime.startswith("application/vnd.google-apps."):
            # Native Docs/Sheets/Slides have to be exported.
            export = {"application/vnd.google-apps.document": ("text/plain", ".txt"),
                      "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
                      "application/vnd.google-apps.presentation": ("text/plain", ".txt")}
            fmt, suffix = export.get(mime, ("text/plain", ".txt"))
            r = c.get(f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                      headers=h, params={"mimeType": fmt})
            if r.status_code >= 400:
                raise DataSourceError(f"Drive export failed ({r.status_code}): {r.text[:160]}")
            return (name if name.endswith(suffix) else name + suffix), r.content
        r = c.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                  headers=h, params={"alt": "media", "supportsAllDrives": "true"})
        if r.status_code >= 400:
            raise DataSourceError(f"Drive download failed ({r.status_code}): {r.text[:160]}")
        return name, r.content


def _gcs_list(cfg: Dict[str, Any], path: str) -> List[Entry]:
    """path is "" (buckets), "bucket" or "bucket/prefix/". GCS has no
    real directories — `delimiter=/` makes shared prefixes behave as
    folders, which is what the browser shows."""
    token = _google_token(cfg, "https://www.googleapis.com/auth/devstorage.read_only")
    h = {"Authorization": f"Bearer {token}"}
    if not path:
        named = [b.strip() for b in str(cfg.get("bucket_names") or "").split(",") if b.strip()]
        if named:
            return [Entry(id=b, name=b, is_dir=True, path=b) for b in named]
        project = None
        raw = cfg.get("credentials")
        try:
            project = (json.loads(raw) if isinstance(raw, str) else raw).get("project_id")
        except Exception:
            pass
        if not project:
            raise DataSourceError(
                "List which buckets to browse on this source, or use a service-account key "
                "that includes project_id."
            )
        with httpx.Client(timeout=60) as c:
            data = _check(c.get("https://storage.googleapis.com/storage/v1/b",
                                headers=h, params={"project": project}), "GCS list buckets")
        return [Entry(id=b["name"], name=b["name"], is_dir=True, path=b["name"])
                for b in data.get("items", [])]
    bucket, _, prefix = path.partition("/")
    with httpx.Client(timeout=60) as c:
        data = _check(c.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o",
                            headers=h, params={"prefix": prefix, "delimiter": "/",
                                               "maxResults": 500}),
                      f"GCS list {path}")
    out: List[Entry] = []
    for p in data.get("prefixes", []):
        out.append(Entry(id=f"{bucket}/{p}", name=p[len(prefix):].rstrip("/"),
                         is_dir=True, path=f"{bucket}/{p}"))
    for o in data.get("items", []):
        nm = o["name"][len(prefix):]
        if not nm:
            continue  # the prefix placeholder object itself
        out.append(Entry(id=f"{bucket}/{o['name']}", name=nm, is_dir=False,
                         size=int(o.get("size", 0)) or None, mime=o.get("contentType"),
                         ingestible=_is_ingestible(nm),
                         note=file_note(nm)))
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def _gcs_fetch(cfg: Dict[str, Any], file_id: str) -> Tuple[str, bytes]:
    token = _google_token(cfg, "https://www.googleapis.com/auth/devstorage.read_only")
    bucket, _, obj = file_id.partition("/")
    from urllib.parse import quote
    with httpx.Client(timeout=300, follow_redirects=True) as c:
        r = c.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(obj, safe='')}",
                  headers={"Authorization": f"Bearer {token}"}, params={"alt": "media"})
        if r.status_code >= 400:
            raise DataSourceError(f"GCS download failed ({r.status_code}): {r.text[:160]}")
    return obj.rsplit("/", 1)[-1], r.content


# ------------------------------------------------------------------ Notion
def _notion_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    token = cfg.get("credentials")
    if not token:
        raise DataSourceError("No Notion integration token stored.")
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


def _notion_title(obj: Dict[str, Any]) -> str:
    if obj.get("object") == "database":
        return "".join(t.get("plain_text", "") for t in obj.get("title", [])) or "Untitled database"
    props = obj.get("properties", {})
    for v in props.values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title", [])) or "Untitled"
    return "Untitled"


# Notion returns at most 100 results per call and pages the rest behind
# a cursor. Five pages is a real workspace's worth of connected content
# without letting one browse run forever; hitting the cap is reported
# rather than silently showing a subset.
_NOTION_MAX_PAGES = 5


def _notion_paged(c: httpx.Client, h: Dict[str, str], *, url: str, what: str,
                  json_body: Optional[Dict[str, Any]] = None,
                  params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], bool]:
    """Every result across up to _NOTION_MAX_PAGES, and whether there
    were more. The previous single call showed only the first 100 items
    with nothing to say that was all it asked for."""
    results: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(_NOTION_MAX_PAGES):
        if json_body is not None:
            body = {**json_body, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = _check(c.post(url, headers=h, json=body), what)
        else:
            query = {**(params or {}), "page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            data = _check(c.get(url, headers=h, params=query), what)
        results.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return results, False
    return results, True


def _notion_bot_identity(c: httpx.Client, h: Dict[str, str]) -> Tuple[str, str]:
    """The integration's own name and workspace, for the empty-listing
    message. Only fetched when there is nothing to show, since that is
    the only time it helps."""
    try:
        r = c.get("https://api.notion.com/v1/users/me", headers=h)
        if r.status_code != 200:
            return "", ""
        me = r.json()
        return me.get("name") or "", ((me.get("bot") or {}).get("workspace_name") or "")
    except Exception:
        return "", ""


def _notion_list(cfg: Dict[str, Any], path: str) -> List[Entry]:
    """Notion has pages, not folders. A page that contains child pages
    behaves as a folder and is also ingestible in its own right, so it
    is listed as a directory whose id can still be selected.

    An integration token sees NOTHING in a workspace until each page or
    database is connected to it — Notion's deliberate design, not a
    permission that can be granted once centrally. So an empty listing
    here is the normal state of a freshly created integration, and
    saying "no files" without saying that reads as a broken connection.
    The notice names the integration and its workspace, which is what
    someone needs in order to find it in the page's own Connections
    menu.
    """
    h = _notion_headers(cfg)
    with httpx.Client(timeout=60) as c:
        if not path:
            items, truncated = _notion_paged(
                c, h, url="https://api.notion.com/v1/search", what="Notion search",
                json_body={})
            if not items:
                bot, workspace = _notion_bot_identity(c, h)
                LAST_NOTICE["Notion"] = (
                    f"The token works — this is the integration "
                    f"{('\'' + bot + '\'') if bot else 'configured here'}"
                    f"{(' in the ' + workspace + ' workspace') if workspace else ''} — but no page "
                    f"or database has been connected to it yet. Notion integrations see nothing by "
                    f"default: open a page in Notion, use the ••• menu → Connections (older "
                    f"versions: 'Add connections'), and pick "
                    f"{('\'' + bot + '\'') if bot else 'this integration'}. Child pages are "
                    f"included automatically, so connecting one parent page is usually enough."
                )
            elif truncated:
                LAST_NOTICE["Notion"] = (
                    f"Showing the first {len(items)} connected pages and databases; this "
                    f"workspace has more. Open a page to browse inside it rather than scrolling "
                    f"this list."
                )
            return [Entry(id=it["id"], name=_notion_title(it), is_dir=True, path=it["id"],
                          note="page" if it.get("object") == "page" else "database")
                    for it in items]
        blocks, truncated = _notion_paged(
            c, h, url=f"https://api.notion.com/v1/blocks/{path}/children",
            what="Notion children")
    out: List[Entry] = []
    for b in blocks:
        if b.get("type") in ("child_page", "child_database"):
            title = (b.get("child_page") or b.get("child_database") or {}).get("title", "Untitled")
            out.append(Entry(id=b["id"], name=title, is_dir=True, path=b["id"], note="page"))
    if truncated:
        LAST_NOTICE["Notion"] = "This page has more child pages than one listing can show."
    return out


def _notion_fetch(cfg: Dict[str, Any], file_id: str) -> Tuple[str, bytes]:
    """Notion stores content as blocks, not a file, so the page is
    flattened to Markdown-ish text before it goes through the normal
    ingestion path."""
    h = _notion_headers(cfg)
    lines: List[str] = []
    with httpx.Client(timeout=120) as c:
        page = _check(c.get(f"https://api.notion.com/v1/pages/{file_id}", headers=h), "Notion page")
        title = _notion_title(page)
        lines.append(f"# {title}")
        cursor = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = _check(c.get(f"https://api.notion.com/v1/blocks/{file_id}/children",
                                headers=h, params=params), "Notion blocks")
            for b in data.get("results", []):
                body = b.get(b.get("type"), {})
                text = "".join(t.get("plain_text", "") for t in (body.get("rich_text") or []))
                if not text:
                    continue
                t = b.get("type")
                if t == "heading_1":
                    lines.append(f"# {text}")
                elif t == "heading_2":
                    lines.append(f"## {text}")
                elif t == "heading_3":
                    lines.append(f"### {text}")
                elif t in ("bulleted_list_item", "numbered_list_item"):
                    lines.append(f"- {text}")
                else:
                    lines.append(text)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in title).strip() or "notion-page"
    return f"{safe}.md", ("\n\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------- dispatch
_LIST = {
    "GitHub": lambda cfg, path: _github_list(cfg, path),
    "Notion": lambda cfg, path: _notion_list(cfg, path),
    "Google Cloud Storage": lambda cfg, path: _gcs_list(cfg, path),
    "Google Drive": lambda cfg, path: _gdrive_list(cfg, path),
    "SharePoint": lambda cfg, path: _graph_list(cfg, "SharePoint", path),
    "OneDrive": lambda cfg, path: _graph_list(cfg, "OneDrive", path),
}
_FETCH = {
    "GitHub": lambda cfg, fid: _github_fetch(cfg, fid),
    "Notion": lambda cfg, fid: _notion_fetch(cfg, fid),
    "Google Cloud Storage": lambda cfg, fid: _gcs_fetch(cfg, fid),
    "Google Drive": lambda cfg, fid: _gdrive_fetch(cfg, fid),
    "SharePoint": lambda cfg, fid: _graph_fetch(cfg, "SharePoint", fid),
    "OneDrive": lambda cfg, fid: _graph_fetch(cfg, "OneDrive", fid),
}

SUPPORTED = sorted(_LIST)


def list_children(source: str, config: Dict[str, Any], path: str = "") -> List[Dict[str, Any]]:
    if source not in _LIST:
        raise DataSourceError(f"Browsing is not implemented for '{source}'. Supported: {SUPPORTED}.")
    LAST_NOTICE.pop(source, None)
    return [e.as_dict() for e in _LIST[source](config, path or "")]


def take_notice(source: str) -> Optional[str]:
    """Any scope explanation the last listing produced, consumed once."""
    return LAST_NOTICE.pop(source, None)


# A folder can hold anything, and a recursive walk of a whole GitHub
# organisation or OneDrive account is not something to launch by
# accident, so expansion is bounded on both axes.
MAX_FOLDER_FILES = 500
MAX_FOLDER_DEPTH = 12


def expand_folder(source: str, config: Dict[str, Any], path: str,
                  max_files: int = MAX_FOLDER_FILES) -> Tuple[List[Dict[str, str]], bool]:
    """Every ingestible file beneath a folder, walked breadth-first.

    Returns (files, truncated) where each file is {"id", "name", "path"}
    and `truncated` says the cap was hit — reported rather than silently
    ingesting an arbitrary subset.

    Selecting a folder is the natural way to say "index this", but only
    files can be fetched, so the expansion has to happen somewhere. Doing
    it server-side means one request instead of the browser walking the
    tree with a round trip per directory.

    Breadth-first deliberately: if the cap is hit, what was collected is
    the shallow, most-likely-wanted part of the tree rather than one deep
    branch. Directories already seen are skipped, so a symlink loop or a
    provider that lists a parent inside its own child cannot spin.
    """
    collected: List[Dict[str, str]] = []
    seen: set = set()
    queue: List[Tuple[str, str, int]] = [(path, "", 0)]
    truncated = False

    while queue:
        current, prefix, depth = queue.pop(0)
        if current in seen or depth > MAX_FOLDER_DEPTH:
            continue
        seen.add(current)
        for entry in list_children(source, config, current):
            name = entry["name"]
            rel = f"{prefix}{name}"
            if entry["is_dir"]:
                if entry["path"]:
                    queue.append((entry["path"], f"{rel}/", depth + 1))
                continue
            if not entry["ingestible"]:
                continue  # no text extractor; listing it would only fail later
            if len(collected) >= max_files:
                truncated = True
                return collected, truncated
            collected.append({"id": entry["id"], "name": name, "path": rel})
    return collected, truncated


def fetch_file(source: str, config: Dict[str, Any], file_id: str) -> Tuple[str, bytes]:
    if source not in _FETCH:
        raise DataSourceError(f"Fetching is not implemented for '{source}'. Supported: {SUPPORTED}.")
    return _FETCH[source](config, file_id)
