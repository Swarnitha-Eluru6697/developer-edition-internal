"""Mapping a knowledge base's stored host to one this process can reach.

A KB row records a single host/port, but the same Postgres often has two
names depending on who is asking. In this project's own dev setup the
pgvector database is `db:5432` on the compose network and
`127.0.0.1:5435` from the host, so a KB created through the
containerised api stores `host: "db"` — and every later request served
by a backend running directly on the host then fails with

    DNS resolution failed for 'db'

even though the database is running and reachable. Editing each KB row
does not fix it either: whichever name is stored, the other caller
breaks.

KB_HOST_ALIASES resolves that without either side winning:

    KB_HOST_ALIASES="db=127.0.0.1:5435"
    KB_HOST_ALIASES="db=127.0.0.1:5435,warehouse=10.0.0.4"

Deliberately opt-in and explicit. Nothing is inferred, no name is
rewritten unless an operator said to, and a process with the variable
unset (the container, any real deployment) behaves exactly as before.
Aliasing only ever redirects a *hostname*, never credentials or a
database name, and the result still goes through the same
validate_database_host egress check as any other host.
"""
import os
from typing import Dict, Optional, Tuple

_ENV_VAR = "KB_HOST_ALIASES"


def _parse(raw: Optional[str]) -> Dict[str, Tuple[str, Optional[int]]]:
    """"name=host[:port],name2=host2" -> {name: (host, port|None)}.

    A malformed entry is skipped rather than raising: this runs on the
    connection path, and a typo in an operator convenience must not take
    every knowledge base offline.
    """
    aliases: Dict[str, Tuple[str, Optional[int]]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, target = entry.partition("=")
        name, target = name.strip(), target.strip()
        if not name or not target:
            continue
        host, sep, port_text = target.rpartition(":")
        if sep and port_text.isdigit():
            aliases[name] = (host or target, int(port_text))
        else:
            aliases[name] = (target, None)
    return aliases


def resolve_kb_host(host: str, port) -> Tuple[str, int]:
    """The host/port this process should actually dial for a KB.

    Returns the input unchanged when no alias applies, which is the
    normal case everywhere except a developer machine running a second
    backend outside the compose network.
    """
    try:
        port_num = int(port) if port is not None else 5432
    except (TypeError, ValueError):
        port_num = 5432
    alias = _parse(os.getenv(_ENV_VAR)).get(host)
    if not alias:
        return host, port_num
    mapped_host, mapped_port = alias
    return mapped_host, (mapped_port if mapped_port is not None else port_num)


def alias_hint(host: str) -> str:
    """A sentence to append to a connection error, naming the fix.

    Only offered when it is genuinely likely to be the cause: a bare
    single-label name resolves on a container network but not on a
    host, whereas a failure on an IP or a dotted domain means something
    else and this advice would be a red herring.
    """
    looks_like_service_name = host and "." not in host and ":" not in host
    if not looks_like_service_name or os.getenv(_ENV_VAR):
        return ""
    return (
        f" '{host}' is a bare hostname, which resolves on a container network but not from a "
        f"process running outside it. If this database is the compose stack's own, set "
        f"{_ENV_VAR}=\"{host}=127.0.0.1:5435\" for this process (run-dev.sh does this) so the "
        f"same knowledge base works from both."
    )
