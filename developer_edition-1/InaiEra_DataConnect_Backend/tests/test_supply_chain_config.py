"""
Hardening: the supply-chain controls stay in place. These are structural checks (the scanners themselves run in CI, against live advisory
databases): they fail if someone deletes the audit job, drops the SBOM, loosens a security floor that a real finding forced, or lets the policy
page and the pinned images drift apart.
"""

import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
WORKFLOW = (BACKEND / ".github" / "workflows" / "security.yml").read_text()
PYPROJECT = tomllib.loads((BACKEND / "pyproject.toml").read_text())
DOC = (BACKEND / "docs" / "SUPPLY_CHAIN.md").read_text()


def _floor(requirement_list: list[str], name: str) -> tuple[int, ...]:
    for requirement in requirement_list:
        match = re.match(rf"^{re.escape(name)}\s*>=\s*([\d.]+)", requirement, re.IGNORECASE)
        if match:
            return tuple(int(part) for part in match.group(1).split("."))
    raise AssertionError(f"{name} has no >= floor in pyproject.toml")


def test_the_security_workflow_audits_dependencies_scans_the_image_and_publishes_sboms() -> None:
    for required in ("pip_audit", "cyclonedx-bom", "aquasecurity/trivy-action", "anchore/sbom-action", "upload-artifact"):
        assert required in WORKFLOW, f"security.yml no longer contains {required}"


def test_the_python_sbom_is_taken_before_the_scanners_are_installed() -> None:
    """Otherwise pip-audit's own dependencies land in the bill of materials of a product that does not ship them."""
    job = WORKFLOW[WORKFLOW.index("python-dependencies:") : WORKFLOW.index("  image:")]
    assert job.index("cyclonedx-bom") < job.index("pip install pip-audit")


def test_it_runs_on_a_schedule_not_only_on_changes() -> None:
    assert re.search(r"^\s*schedule:", WORKFLOW, re.MULTILINE), "a new CVE needs no code change, so the scan must run on a timer"


def test_the_image_scan_is_blocking_but_only_for_fixable_high_and_critical() -> None:
    block = WORKFLOW[WORKFLOW.index("Trivy scan (image)") : WORKFLOW.index("SBOM (image")]
    assert "HIGH,CRITICAL" in block
    assert "ignore-unfixed: true" in block
    assert 'exit-code: "1"' in block


def test_the_security_tooling_is_an_installable_extra() -> None:
    extras = PYPROJECT["project"]["optional-dependencies"]["security"]
    assert {re.split(r"[<>=]", e)[0] for e in extras} == {"pip-audit", "cyclonedx-bom"}


def test_pymongo_keeps_the_floor_that_fixed_three_cves() -> None:
    """pip-audit found CVE-2026-96747/96748/96749 in pymongo 4.18.1; the fixed release is 4.18.2."""
    assert _floor(PYPROJECT["project"]["dependencies"], "pymongo") >= (4, 18, 2)


def test_the_image_upgrades_its_build_tooling_to_the_fixed_versions() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text()
    match = re.search(r'pip install[^\n]*"pip>=([\d.]+)"[^\n]*"setuptools>=([\d.]+)"', dockerfile)
    assert match, "the Dockerfile must upgrade pip and setuptools before installing the project"
    assert tuple(map(int, match.group(1).split("."))) >= (26, 2)
    assert tuple(map(int, match.group(2).split("."))) >= (83, 0, 0)
    assert dockerfile.index("pip>=") < dockerfile.index("pip install --no-cache-dir -e ."), "tooling must be upgraded BEFORE the project is installed"


def test_make_exposes_audit_and_sbom() -> None:
    makefile = (BACKEND / "Makefile").read_text()
    assert re.search(r"^audit:", makefile, re.MULTILINE)
    assert re.search(r"^sbom:", makefile, re.MULTILINE)
    assert "sbom/" in (BACKEND / ".gitignore").read_text()


def test_the_trivyignore_never_holds_an_exception_without_a_reason_and_expiry() -> None:
    for line in (BACKEND / ".trivyignore").read_text().splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        assert "#" in entry, f"exception needs a reason: {line!r}"
        assert re.search(r"expires \d{4}-\d{2}-\d{2}", entry), f"exception needs `# reason — owner — expires YYYY-MM-DD`: {line!r}"


def test_the_policy_page_lists_exactly_the_images_the_compose_pins() -> None:
    compose = (BACKEND / "infra" / "docker-compose.yml").read_text()
    for image in ("postgres", "redis", "cp-zookeeper", "cp-kafka", "cp-schema-registry", "prometheus", "kafka-ui"):
        assert image in compose, f"{image} not in compose any more"
        assert image.replace("cp-", "") in DOC, f"{image} is pinned in compose but missing from docs/SUPPLY_CHAIN.md"
