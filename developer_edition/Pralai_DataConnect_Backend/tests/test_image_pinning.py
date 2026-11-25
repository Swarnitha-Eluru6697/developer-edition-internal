"""
Hardening: every OPERATIONAL container image is pinned to an exact version AND an immutable digest (`name:1.2.3@sha256:<64 hex>`).

A floating tag (`latest`, `16`, `3.11-slim`) means the same file builds a different image next month — a silent upgrade of the database, the
broker or the base OS, and a supply-chain risk (a re-pushed tag is not noticed). The digest is what makes it reproducible; the tag is for the
human reading it. Images that are built locally by compose (`inai-connect-*:local`, the app's own build) are not pulled, so they are exempt.

Scope is the operational stack: the compose files used to run DataConnect and the Dockerfiles that build/ship it. `docker-compose.test-backends.yml`
is a catalogue of throwaway third-party systems used to exercise connectors (many only publish `latest`); it is deliberately out of scope, and
a test below makes sure the operational compose never pulls from it.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "Pralai_DataConnect_Frontend"

OPERATIONAL_FILES = [
    BACKEND / "infra" / "docker-compose.yml",
    BACKEND / "infra" / "docker-compose.fresh.yml",
    BACKEND / "Dockerfile",
    *sorted((BACKEND / "infra").glob("*.Dockerfile")),
    FRONTEND / "Dockerfile",
]

PINNED = re.compile(r"^(?P<name>[\w./-]+):(?P<tag>[\w.-]+)@sha256:[0-9a-f]{64}$")
FLOATING_TAGS = {"latest", "stable", "main", "master", "edge", "nightly"}
IMAGE_REF = re.compile(r"^\s*image:\s*(?P<compose>\S+)|^FROM\s+(?:--platform=\S+\s+)?(?P<docker>\S+)", re.MULTILINE)


def _external_images(path: Path) -> list[str]:
    refs = [m.group("compose") or m.group("docker") for m in IMAGE_REF.finditer(path.read_text())]
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", path.read_text(), re.MULTILINE | re.IGNORECASE))
    # a reference to an earlier build stage, an `${ARG}` or a locally built image is not pulled from a registry
    return [r for r in refs if r not in stages and not r.startswith("$") and not r.endswith(":local")]


def _existing() -> list[Path]:
    return [p for p in OPERATIONAL_FILES if p.exists()]


def test_the_operational_files_are_actually_found() -> None:
    names = {p.name for p in _existing()}
    assert {"docker-compose.yml", "docker-compose.fresh.yml", "Dockerfile", "kafka-connect.Dockerfile"} <= names


@pytest.mark.parametrize("path", _existing(), ids=lambda p: str(p.relative_to(BACKEND.parent)))
def test_every_pulled_image_is_pinned_to_a_version_and_a_digest(path: Path) -> None:
    images = _external_images(path)
    assert images, f"found no images in {path.name} — the scan is broken"
    unpinned = [image for image in images if not PINNED.match(image)]
    assert unpinned == [], f"{path.name}: pin these as name:version@sha256:<digest> — {unpinned}"


@pytest.mark.parametrize("path", _existing(), ids=lambda p: str(p.relative_to(BACKEND.parent)))
def test_no_image_uses_a_floating_tag_even_with_a_digest(path: Path) -> None:
    """`name:latest@sha256:…` still reads as "whatever is newest" to the next person who bumps the digest."""
    floating = [i for i in _external_images(path) if (m := PINNED.match(i)) and m.group("tag") in FLOATING_TAGS]
    assert floating == []


def test_the_pin_check_actually_rejects_what_it_should() -> None:
    for bad in ["postgres:16-alpine", "redis", "kafka-ui:latest", "x/y:1.0@sha256:abc", "x/y@sha256:" + "a" * 64]:
        assert not PINNED.match(bad), bad
    assert PINNED.match("postgres:16-alpine@sha256:" + "a" * 64)


def test_the_operational_compose_never_pulls_from_the_unpinned_test_backend_catalogue() -> None:
    for name in ("docker-compose.yml", "docker-compose.fresh.yml"):
        text = (BACKEND / "infra" / name).read_text()
        assert "docker-compose.test-backends.yml" not in re.sub(r"#.*", "", text), f"{name} must not include the test-backend catalogue"


def test_the_local_images_are_only_ever_the_ones_compose_builds() -> None:
    """`:local` is exempt from pinning because nothing pulls it — so it must really be built, not pulled."""
    text = (BACKEND / "infra" / "docker-compose.yml").read_text()
    for image in set(re.findall(r"image:\s*(\S+:local)", text)):
        block = text[text.index(f"image: {image}") - 600 : text.index(f"image: {image}") + 200]
        assert "build:" in block, f"{image} is :local but has no build: — it would be pulled unpinned"
