#!/usr/bin/env python3
"""Run the connector test backends (infra/docker-compose.test-backends.yml) without melting your laptop.

    ./test-backends.py list                 tiers, sizes, what is running
    ./test-backends.py up mssql             a HEAVY unit (any other heavy unit is stopped first)
    ./test-backends.py up light-1           a group of three LIGHT units
    ./test-backends.py up qdrant influxdb   individual light units
    ./test-backends.py down mssql           stop + remove a unit's containers (data volumes are kept)
    ./test-backends.py down all [--volumes] stop everything (--volumes also deletes the data)
    ./test-backends.py pull mssql           download a unit's images without starting it
    ./test-backends.py init                 (re)generate the certificates / fixtures some units need

Rules, enforced here because the compose file alone cannot:
  * one HEAVY unit at a time (they are big images and want gigabytes of RAM each);
  * at most TB_MAX_LIGHT (default 3) LIGHT units at the same time;
  * an image that is not on this machine yet is never downloaded without asking (use --yes to skip the question).

Only the Python standard library is used. Needs docker compose v2 and, for `init`, openssl.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "docker-compose.test-backends.yml"
DATA = HERE / "test-backends-data"
PROJECT = "test-backends"
MAX_LIGHT = int(os.environ.get("TB_MAX_LIGHT", "3"))
LABEL = "inaiera.test-backend."


def run(cmd: list[str], *, capture: bool = False, check: bool = True, input_: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=capture, check=check, input=input_)


def compose(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], capture=capture, check=check)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ── model: units and groups, read from the compose file's profiles and labels ──────────────────────────────────────

def load_model() -> dict:
    cfg = json.loads(compose("--profile", "*", "config", "--format", "json", capture=True).stdout)
    services = cfg["services"]
    units: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    for name, svc in services.items():
        labels = svc.get("labels", {})
        size = int(labels.get(LABEL + "size-mb", "0"))
        for entry in labels[LABEL + "units"].split(","):   # "unit:tier:group"; a shared service lists several units
            unit, tier, group = entry.split(":")
            u = units.setdefault(unit, {"tier": tier, "services": [], "size_mb": 0, "images": {}, "group": group or None})
            u["services"].append(name)
            u["size_mb"] += size
            u["images"][svc["image"]] = size
            if group:
                groups.setdefault(group, [])
                if unit not in groups[group]:
                    groups[group].append(unit)
    return {"services": services, "units": units, "groups": groups}


def running_units(model: dict) -> set[str]:
    out = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
               "--format", "{{.Label \"com.docker.compose.service\"}}"], capture=True).stdout.split()
    live = set(out)
    return {u for u, d in model["units"].items() if live & set(d["services"])}


def image_present(image: str) -> bool:
    return run(["docker", "image", "inspect", image], capture=True, check=False).returncode == 0


def resolve(model: dict, targets: list[str]) -> list[str]:
    units: list[str] = []
    for t in targets:
        if t in model["groups"]:
            units += model["groups"][t]
        elif t in model["units"]:
            units.append(t)
        else:
            die(f"unknown unit or group '{t}'. Run `{Path(sys.argv[0]).name} list`.")
    return list(dict.fromkeys(units))


# ── commands ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def cmd_list(_args, model) -> None:
    live = running_units(model)
    print(f"HEAVY — one at a time            LIGHT — up to {MAX_LIGHT} at a time (groups: {', '.join(sorted(model['groups']))})\n")
    print(f"{'unit':<15}{'tier':<7}{'group':<9}{'~MB':>6}  {'state':<9}{'images':<8}containers")
    for tier in ("heavy", "light"):
        for unit, d in sorted(model["units"].items(), key=lambda kv: (-kv[1]["size_mb"], kv[0])):
            if d["tier"] != tier:
                continue
            have = sum(image_present(i) for i in d["images"])
            print(f"{unit:<15}{tier:<7}{d['group'] or '-':<9}{d['size_mb']:>6}  {'RUNNING' if unit in live else 'stopped':<9}"
                  f"{have}/{len(d['images']):<6}{', '.join(d['services'])}")
    print("\n~MB is the images' size on disk; images column = how many are already downloaded.")


def cmd_status(_args, model) -> None:
    live = running_units(model)
    if not live:
        print("nothing running")
    for unit in sorted(live):
        print(f"{unit:<15}{model['units'][unit]['tier']}")


def ensure_pulled(model: dict, units: list[str], assume_yes: bool) -> None:
    missing: dict[str, int] = {}
    for u in units:
        for image, size in model["units"][u]["images"].items():
            if not image_present(image):
                missing[image] = size
    if not missing:
        return
    print("These images are not on this machine yet:")
    for image, size in missing.items():
        print(f"  {image}  (~{size} MB)")
    if not assume_yes and (not sys.stdin.isatty() or input(f"Download ~{sum(missing.values())} MB? [y/N] ").strip().lower() != "y"):
        die("not downloading. Re-run with --yes to allow it.")
    for image in missing:
        run(["docker", "pull", image])


def cmd_pull(args, model) -> None:
    ensure_pulled(model, resolve(model, args.targets), args.yes)


def cmd_up(args, model) -> None:
    wanted = resolve(model, args.targets)
    heavy = [u for u in wanted if model["units"][u]["tier"] == "heavy"]
    light = [u for u in wanted if model["units"][u]["tier"] == "light"]
    if len(heavy) > 1:
        die(f"heavy units run one at a time, you asked for {len(heavy)}: {', '.join(heavy)}")
    live = running_units(model)
    if heavy:
        others = sorted(u for u in live if model["units"][u]["tier"] == "heavy" and u != heavy[0])
        if others:
            print(f"stopping the running heavy unit first: {', '.join(others)}")
            for u in others:
                compose("--profile", u, "down")
    running_light = sorted(u for u in live if model["units"][u]["tier"] == "light" and u not in light)
    if len(running_light) + len(light) > MAX_LIGHT:
        die(f"at most {MAX_LIGHT} light units at a time. Running: {', '.join(running_light) or 'none'}; requested: {', '.join(light)}.\n"
            f"Stop some first, e.g. `{Path(sys.argv[0]).name} down {(running_light or light)[0]}` (or raise TB_MAX_LIGHT).")
    ensure_pulled(model, wanted, args.yes)
    if {"ftps", "cosmos", "firebase"} & set(wanted):
        cmd_init(args, model, only=set(wanted))
    profiles = [x for u in wanted for x in ("--profile", u)]
    compose(*profiles, "up", "-d")
    if "ftps" in wanted:
        post_up_ftps(model["services"]["ftps"]["container_name"])
    if "mongo" in wanted:
        post_up_mongo(model["services"]["mongo"]["container_name"])
    show_endpoints(model, wanted)


def cmd_down(args, model) -> None:
    targets = list(model["units"]) if args.targets == ["all"] else resolve(model, args.targets)
    profiles = [x for u in targets for x in ("--profile", u)]
    compose(*profiles, "down", *(["--volumes"] if args.volumes else []))


def show_endpoints(model: dict, units: list[str]) -> None:
    print()
    for u in units:
        for s in model["units"][u]["services"]:
            ports = model["services"][s].get("ports", [])
            shown = ", ".join(f"localhost:{p['published']}" for p in ports) or "(internal only — reachable on the dataconnect network)"
            print(f"  {s:<18}{shown}")


# ── fixtures: certificates and files that cannot live in git ─────────────────────────────────────────────────────────

def openssl_cert(directory: Path, stem: str, cn: str, sans: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    san = ",".join(f"DNS:{s}" for s in sans)
    conf = (f"[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN={cn}\n[ext]\nsubjectAltName={san}\n")
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as f:
        f.write(conf)
    try:
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-config", f.name,
             "-keyout", str(directory / f"{stem}.key"), "-out", str(directory / f"{stem}.pem")], capture=True)
    finally:
        os.unlink(f.name)


def cmd_init(_args, model, only: set[str] | None = None) -> None:
    only = only or {"ftps", "cosmos", "firebase"}
    if "ftps" in only and not (DATA / "ftps-certs" / "cert.pem").exists():
        openssl_cert(DATA / "ftps-certs", "tmp", "ftps", ["ftps", "localhost"])
        (DATA / "ftps-certs" / "tmp.pem").rename(DATA / "ftps-certs" / "cert.pem")
        (DATA / "ftps-certs" / "tmp.key").rename(DATA / "ftps-certs" / "key.pem")
        print("generated ftps certificate")
    if "cosmos" in only and not (DATA / "cosmos" / "cosmos.pfx").exists():
        d = DATA / "cosmos"
        openssl_cert(d, "cosmos", "t1-cosmos", ["t1-cosmos", "localhost"])
        secret = model["services"]["t1-cosmos"]["environment"]["CERT_SECRET"]
        run(["openssl", "pkcs12", "-export", "-inkey", str(d / "cosmos.key"), "-in", str(d / "cosmos.pem"),
             "-out", str(d / "cosmos.pfx"), "-passout", f"pass:{secret}"], capture=True)
        print("generated cosmos certificate (t1-cosmos, localhost)")
    if "firebase" in only and not (DATA / "firebase" / "fb" / "firebase.json").exists():
        fb = DATA / "firebase" / "fb"
        fb.mkdir(parents=True, exist_ok=True)
        (fb / "firebase.json").write_text(json.dumps({
            "database": {"rules": "database.rules.json"},
            "emulators": {"database": {"host": "0.0.0.0", "port": 9000}, "ui": {"enabled": False}}}, indent=2) + "\n")
        (fb / "database.rules.json").write_text(json.dumps({"rules": {".read": True, ".write": True}}, indent=2) + "\n")
        tls = DATA / "firebase" / "tls"
        openssl_cert(tls, "fb", "t1-firebase-tls", ["t1-firebase-tls", "t1-firebase", "localhost"])
        (tls / "Caddyfile").write_text("{\n\tauto_https off\n}\n:443 {\n\ttls /certs/fb.pem /certs/fb.key\n\treverse_proxy t1-firebase:9000\n}\n")
        print("generated firebase emulator config + TLS front (UNVERIFIED, see TEST_BACKENDS.md)")


def post_up_ftps(container: str) -> None:
    """vsftpd insists on TLS session reuse on the data channel, which Java FTPS clients do not do (test log, FTPS #5)."""
    seed = "id,name,dept\n1,Ann,Sales\n2,Bob,Engineering\n3,Cara,Finance\n"
    run(["docker", "exec", container, "sh", "-c",
         "mkdir -p /ftp/dataconnect && printf '%s' \"$0\" > /ftp/dataconnect/employees.csv && chown -R 1000 /ftp/dataconnect; "
         "grep -q '^require_ssl_reuse=NO' /etc/vsftpd/vsftpd.conf || echo 'require_ssl_reuse=NO' >> /etc/vsftpd/vsftpd.conf", seed],
        check=False)
    run(["docker", "restart", container], capture=True, check=False)


def post_up_mongo(container: str) -> None:
    """The node runs as a single-member replica set (rs0) — change streams and the Debezium MongoDB connector need one.
    rs.initiate() is only needed the first time; a restored data volume already carries the set."""
    script = ("try { rs.status().ok } catch (e) { "
              "rs.initiate({_id: 'rs0', members: [{_id: 0, host: 'mongo:27017'}]}) }")
    for _ in range(30):
        ping = run(["docker", "exec", container, "mongosh", "--quiet", "-u", "dataconnect", "-p", "dataconnect",
                    "--authenticationDatabase", "admin", "--eval", "db.adminCommand({ping: 1}).ok"], capture=True, check=False)
        if ping.stdout.strip().endswith("1"):
            break
        time.sleep(2)
    else:
        print("warning: mongo did not answer; run rs.initiate() yourself (see TEST_BACKENDS.md)")
        return
    out = run(["docker", "exec", container, "mongosh", "--quiet", "-u", "dataconnect", "-p", "dataconnect",
               "--authenticationDatabase", "admin", "--eval", script], capture=True, check=False)
    print("mongo replica set rs0:", "ready" if out.returncode == 0 else out.stderr.strip() or out.stdout.strip())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    for name, fn in (("up", cmd_up), ("pull", cmd_pull)):
        s = sub.add_parser(name)
        s.add_argument("targets", nargs="+")
        s.add_argument("--yes", "-y", action="store_true", help="download missing images without asking")
        s.set_defaults(fn=fn)
    s = sub.add_parser("down")
    s.add_argument("targets", nargs="+")
    s.add_argument("--volumes", "-v", action="store_true", help="also delete the units' data volumes")
    s.set_defaults(fn=cmd_down)
    args = p.parse_args()
    if not COMPOSE_FILE.exists():
        die(f"{COMPOSE_FILE} not found")
    if run(["docker", "network", "inspect", "dataconnect"], capture=True, check=False).returncode != 0 and args.cmd == "up":
        die("the `dataconnect` docker network does not exist. Start the main stack first:  docker compose up -d  (in this folder)")
    args.fn(args, load_model())


if __name__ == "__main__":
    main()
