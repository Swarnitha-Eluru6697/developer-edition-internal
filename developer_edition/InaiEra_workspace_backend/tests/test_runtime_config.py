"""W-006: the SPA's control-plane login URL is deployment configuration, served at /config.json (public, secret-free)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_the_config_is_public_and_not_cached(client):
    r = client.get("/config.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"controlPlaneLoginUrl"}


def test_the_login_url_is_derived_from_the_issuer_by_default(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "control_plane_login_url", None)
    monkeypatch.setattr(s, "control_plane_issuer", "https://login.staging.example.org/")
    assert client.get("/config.json").json()["controlPlaneLoginUrl"] == "https://login.staging.example.org/login"


def test_an_explicit_login_url_wins(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "control_plane_login_url", "https://sso.corp.example/enter")
    assert client.get("/config.json").json()["controlPlaneLoginUrl"] == "https://sso.corp.example/enter"


@pytest.mark.parametrize("bad", ["/login", "login", "javascript:alert(1)", "ftp://x/y", "https://"])
def test_a_login_url_that_is_not_absolute_http_is_refused_at_startup(bad):
    with pytest.raises(ValidationError):
        Settings(control_plane_login_url=bad)


def test_nothing_secret_is_in_the_served_config(client):
    body = client.get("/config.json").text.lower()
    assert not any(w in body for w in ("secret", "token", "password", "key"))
