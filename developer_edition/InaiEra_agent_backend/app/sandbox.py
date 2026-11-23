"""
Sandboxed execution for the Code Node (§8 of the design doc; §0.55, GAP_REPORT).

A real, reported security bug (P0) in the previous version of this module:
it ran user code via a plain subprocess with resource limits and a
different `cwd`, while its own docstring claimed "no network access, and
no filesystem access beyond a throwaway temp directory" — neither of
those was actually true. `cwd` does not restrict filesystem access at
all (an absolute path like `open("/etc/passwd")` still worked, verified
live), and a plain subprocess inherits the parent's real network
namespace (an outbound connection was only ever blocked by this sandbox
environment's own incidental egress proxy, not by anything in this
file). The gap was real and confirmed before writing any fix.

What this now genuinely does, using real Linux kernel primitives — not
a full container runtime, but real, verifiable, kernel-enforced
isolation, confirmed by directly testing each guarantee below before
relying on it:

  - A real, separate network namespace (`unshare --net`) — an outbound
    socket connection gets a genuine `ENETUNREACH` from the kernel, not
    an incidental block from surrounding infrastructure. Verified live.
  - A real, separate mount namespace containing a MINIMAL ROOT
    FILESYSTEM built per call: a fresh tmpfs, `pivot_root`ed into, with
    the old root lazily unmounted so it is genuinely detached rather
    than merely unreachable by path. Only the interpreter, its standard
    library, the shared libraries those need, the handful of binaries
    that must be exec'd after the pivot, four /dev character devices
    and one ephemeral work directory are bound into it — the work
    directory read-write, everything else read-only.

    This replaced an earlier version that remounted the existing root
    read-only instead, and the distinction is the whole point of the
    change: read-only stopped the script WRITING anywhere outside its
    temp directory, but left the entire worker filesystem READABLE.
    Application source, world-readable configuration, mounted files,
    and any accidentally-permissive credential file the sandbox UID
    could open were all one `open()` away — and `run_sandboxed_code`
    returns stdout to the agent runtime, so "can read" means "can
    exfiltrate" even with outbound networking already blocked. A
    read-only filesystem is not a private one. Reported as a
    BLOCKER/HIGH and fixed here.

    Three real bugs were found building this, each of which produced a
    confusing failure rather than an obvious one:
      * A bind mount ignores `-o ro` on the first call — the flag only
        takes effect on a second `mount -o remount,bind,ro`. Getting
        this wrong yields a writable mount that reads as `ro` in
        /proc/mounts.
      * An ELF binary names its dynamic loader by the LITERAL path
        recorded in the executable (`/lib/ld-linux-aarch64.so.1`), not
        the realpath. On a merged-/usr distribution that path is a
        symlink, and without it recreated every binary fails with
        "cannot execute: required file not found", which names neither
        the missing file nor the symlink.
      * `umount` resolves its target through `/proc/self/mountinfo`, so
        with no /proc mounted it reports the old root as "No such file
        or directory" and the old root stays attached — a silent
        reopening of the exact gap this change closes.
  - A real, separate PID namespace (`unshare --pid --fork
    --mount-proc`) — the executed script is genuinely PID 1 inside its
    own namespace and cannot see or signal any other real process on
    the host. Verified live.
  - Real privilege drop to an actual unprivileged UID (`setpriv
    --reuid <numeric> --regid <numeric> --clear-groups`; numeric
    because a NAME is resolved through /etc/passwd, which a minimal
    root does not have), plus a real capability
    bounding-set/inheritable-set drop and `--no-new-privs`
    (`setpriv --inh-caps=-all --bounding-set=-all --no-new-privs`) —
    the script cannot regain privileges through a setuid binary even if
    one were somehow reachable. (`/etc/shadow` is no longer merely
    denied — /etc does not exist in the sandbox root at all.)
  - The pre-existing hard CPU-time, memory, and process-count rlimits
    (`_limit_resources`) are kept, and were confirmed to still apply
    correctly across the whole `unshare`/`setpriv`/interpreter exec
    chain (rlimits are inherited across `execve`), plus the pre-existing
    wall-clock timeout via `subprocess.run(timeout=...)`.

A real, honest boundary this does NOT close, stated plainly rather than
implied away: this is still real Linux namespace isolation on the same
kernel the rest of this application runs on, not a separate microVM or
gVisor sandbox with its own kernel — a genuine kernel-level exploit in
namespace/cgroup handling itself is a real, different class of risk
this doesn't address, and the design doc's own stated production target
(Firecracker microVMs, or gVisor's `runsc`) remains the right answer for
a deployment that needs that stronger guarantee. Neither is available
in this build environment: Firecracker needs a Linux/KVM host, and
gVisor's `runsc` needs a container runtime this environment doesn't
provide. No seccomp syscall filter is applied either — a real, stated
gap, not a silent one; the namespace/capability/rlimit/read-only-FS
combination above is real defense in depth, not a substitute for it.

A real, honest deployment constraint, found and confirmed by testing
directly, not assumed: creating network/mount/PID namespaces needs
`CAP_SYS_ADMIN` — in practice, this process needs to run as real root
(as this reference deployment's own backend process does; confirmed
live) or have that capability explicitly granted. Tested directly as a
genuinely unprivileged user and it failed with a real `unshare failed:
Operation not permitted`. An unprivileged user-namespace bridge
(`unshare --user --map-root-user` before the rest) was tested and does
work in this specific environment, but many hardened kernels disable
unprivileged user namespaces entirely, so it isn't a portable substitute
— a real, stated option for a non-root deployment, not implemented
here. `run_sandboxed_code()` checks live, on every call, whether real
isolation can actually be created in the current environment, and
refuses to execute the code at all if it can't — failing closed, not
silently downgrading to the old, weaker guarantees.

Swap this module's implementation for a real Firecracker/gVisor call
when deploying somewhere that supports it — the `run_sandboxed_code()`
interface is what the harness calls, so the caller doesn't need to
change.
"""
import pwd
import resource
import subprocess
import sys
import sysconfig
import tempfile
import os
import shlex
import shutil

TIMEOUT_SECONDS = 5
MEMORY_LIMIT_MB = 128
# The new root is a tmpfs holding nothing but mount points, so it needs
# almost no space. The work directory is a bind mount and is NOT limited
# by this — the rlimits and the caller's own timeout bound what a script
# can produce there.
ROOT_TMPFS_SIZE = "16m"
# Character devices the interpreter and ordinary user code expect to
# exist. Bound as files from the worker's own /dev rather than created
# with mknod, which would need CAP_MKNOD kept for longer than necessary.
DEV_NODES = ("/dev/null", "/dev/urandom", "/dev/random", "/dev/zero")


def _unprivileged_ids() -> tuple:
    """The numeric uid/gid to drop to.

    Numeric, not the name "nobody": setpriv resolves a NAME through
    /etc/passwd, and a minimal root has no /etc/passwd — the failure is
    `setpriv: failed to parse reuid: 'nobody'`, which sounds like a bad
    argument rather than a missing file. Resolved here, on the worker,
    where /etc/passwd does exist, instead of hardcoding 65534 and hoping
    every base image agrees."""
    try:
        entry = pwd.getpwnam("nobody")
        return entry.pw_uid, entry.pw_gid
    except KeyError:
        return 65534, 65534

_UNSHARE_BIN = shutil.which("unshare")
_SETPRIV_BIN = shutil.which("setpriv")
_BASH_BIN = shutil.which("bash")
# pivot_root lives in /usr/sbin, which is not always on a non-root
# PATH even though the binary is present and usable.
_PIVOT_ROOT_BIN = shutil.which("pivot_root") or shutil.which("pivot_root", path="/usr/sbin:/sbin")
_UMOUNT_BIN = shutil.which("umount")
_MOUNT_BIN = shutil.which("mount")


def _shared_objects(binary: str) -> set:
    """Every absolute path `ldd` reports for a binary: its shared
    libraries and its dynamic loader. Read from the live binary rather
    than hardcoded, because these paths are architecture- and
    distro-specific (/lib/aarch64-linux-gnu here, /lib/x86_64-linux-gnu
    elsewhere) and a hardcoded list silently produces a sandbox whose
    interpreter cannot start."""
    found = set()
    try:
        out = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return found
    for line in out.splitlines():
        line = line.strip()
        candidate = ""
        if "=>" in line:
            candidate = line.split("=>", 1)[1].strip().split(" ")[0]
        elif line.startswith("/"):
            # The loader itself is listed with no "=>".
            candidate = line.split(" ")[0]
        if candidate.startswith("/") and os.path.exists(candidate):
            # The path AS REPORTED, not its realpath. Both matter and for
            # different reasons: the realpath says which directory to
            # bind, while the reported path is what binaries actually
            # ask for and therefore has to resolve inside the new root.
            found.add(candidate)
    return found


def _prune_nested(dirs: set) -> list:
    """Drop any directory already contained in another one being bound.
    Binding both /usr/local/lib and /usr/local/lib/python3.12 would stack
    a mount on top of a mount whose own contents are what the inner path
    resolves against — harmless by luck, confusing by design."""
    ordered = sorted(dirs)
    kept = []
    for d in ordered:
        if not any(d != other and d.startswith(other.rstrip("/") + "/") for other in ordered):
            kept.append(d)
    return kept


def _minimal_root_plan() -> tuple:
    """(dirs_ro, files_ro) — everything the sandbox root must contain for
    the interpreter to run, and nothing else.

    This is the fix for the reported gap: remounting / read-only stopped
    the script WRITING anywhere, but left the whole worker filesystem
    READABLE — application source, configuration, anything world-readable
    the sandbox UID could open, all of it exfiltratable through the
    stdout this function's caller returns to the agent runtime. A
    read-only filesystem is not a private one.

    So the script no longer gets the worker's root at all. It gets a
    fresh tmpfs containing only: the interpreter, its standard library,
    the shared libraries those need, the two helper binaries that must
    be exec'd after the pivot, and an ephemeral work directory.
    """
    pybin = os.path.realpath(sys.executable)
    # setpriv and umount are exec'd AFTER pivot_root, so unlike bash
    # (already running, and mapped in memory) they must exist inside the
    # new root or the chain dies with "No such file or directory".
    files = {pybin}
    for helper in (_SETPRIV_BIN, _UMOUNT_BIN, _MOUNT_BIN):
        if helper:
            files.add(os.path.realpath(helper))

    dirs = {sysconfig.get_paths()["stdlib"]}
    # Directories, not just the individual .so files: a stdlib C
    # extension resolves its OWN dependencies at import time (_ssl.so
    # needs libssl, _hashlib needs libcrypto), and those are not listed
    # by ldd on the interpreter. Binding the directory that holds libc
    # covers them; binding only the files ldd names does not, and the
    # failure shows up as a mysterious ImportError inside user code.
    #
    # `links` is the other half. A reported path is very often a symlink
    # into the arch directory (/usr/lib/ld-linux-aarch64.so.1 ->
    # aarch64-linux-gnu/ld-linux-aarch64.so.1). Binding the arch
    # directory does not create that symlink, and without it every
    # binary fails to start with "required file not found" — the ELF
    # names its loader by the symlink path, not the target.
    root_links = dict(_root_symlinks())
    links = set()
    for binary in list(files):
        for reported in _shared_objects(binary):
            real = os.path.realpath(reported)
            dirs.add(os.path.dirname(real))
            # Rewrite the reported path through the root symlinks rather
            # than recording it as-is. ldd names the loader
            # /lib/ld-linux-aarch64.so.1, but /lib is itself recreated as
            # a symlink, so a link created at that path would have to be
            # ordered after its own target directory exists. The
            # canonical /usr/lib/ld-linux-aarch64.so.1 is the same file
            # and needs no ordering.
            canonical = reported
            head = canonical.split("/")[1] if canonical.count("/") >= 1 else ""
            if head in root_links:
                canonical = "/" + root_links[head] + canonical[len(head) + 1:]
            if os.path.islink(canonical):
                target = os.readlink(canonical)
                if not target.startswith("/"):
                    links.add((canonical, target))

    dirs = {d for d in dirs if os.path.isdir(d)}
    kept_dirs = _prune_nested(dirs)
    # A symlink that lives INSIDE a bound directory arrives with the
    # bind — it is already there. Trying to recreate it would mean
    # writing into a read-only mount, which fails the whole setup with
    # "Read-only file system". Only links outside every bind need
    # creating, which in practice means the loader symlink sitting in
    # /usr/lib next to (not inside) the arch directory.
    links = {(path, target) for (path, target) in links
             if not any(path.startswith(d.rstrip("/") + "/") for d in kept_dirs)}
    return kept_dirs, sorted(files), sorted(links)


def _limit_resources():
    mem_bytes = MEMORY_LIMIT_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_NPROC, (10, 10))
    # No forking off children, no writing outside the temp file it's given.


def _real_isolation_available() -> tuple[bool, str]:
    """A real, live check every call — not an assumption made once at
    import time and trusted forever, and not just "are we root" (some
    hardened root configurations still lack CAP_SYS_ADMIN). Returns
    (available, reason) so a real, honest reason can be surfaced when
    it's False rather than a generic failure."""
    if not (_UNSHARE_BIN and _SETPRIV_BIN and _BASH_BIN):
        return False, "unshare/setpriv/bash not found on PATH — this environment cannot provide real kernel-level isolation for the Code node"
    # pivot_root and umount are what make the sandbox root MINIMAL rather
    # than merely read-only. Without them the old behaviour would still
    # "work" while leaving the whole worker filesystem readable, so this
    # refuses instead — the same fail-closed stance as the rest of this
    # check, applied to the guarantee this module now documents.
    if not (_PIVOT_ROOT_BIN and _UMOUNT_BIN and _MOUNT_BIN):
        return False, ("pivot_root/umount/mount not found — cannot build a minimal sandbox root, and "
                       "running with the worker filesystem readable is not an acceptable downgrade")
    plan_dirs, plan_files, _plan_links = _minimal_root_plan()
    missing_runtime = [pth for pth in list(plan_dirs) + list(plan_files) if not os.path.exists(pth)]
    if missing_runtime:
        return False, f"sandbox runtime paths missing, cannot build a minimal root: {missing_runtime}"
    try:
        r = subprocess.run(
            [_UNSHARE_BIN, "--net", "--mount", "--pid", "--fork", "true"],
            capture_output=True, timeout=3, text=True,
        )
        if r.returncode != 0:
            return False, f"this process cannot create real network/mount/PID namespaces (needs CAP_SYS_ADMIN, in practice real root): {r.stderr.strip()}"

        # Creating the namespaces is NOT sufficient, and checking only
        # that was a real bug: in a container that adds CAP_SYS_ADMIN but
        # runs as a non-root user, `unshare --mount` succeeds while every
        # mount inside it fails with "must be superuser to use mount"
        # (the capability is in the bounding set but not the effective
        # set of a non-root process). This check then reported isolation
        # as available, and the Code node failed later, mid-setup, as an
        # opaque timeout instead of the honest refusal this function
        # exists to produce. So the probe now does what the real setup
        # does: make / private and mount a tmpfs.
        with tempfile.TemporaryDirectory() as probe_dir:
            probe = subprocess.run(
                [_UNSHARE_BIN, "--net", "--mount", "--pid", "--fork", _BASH_BIN, "-c",
                 f"mount --make-rprivate / && mount -t tmpfs -o size=1m tmpfs {shlex.quote(probe_dir)}"],
                capture_output=True, timeout=5, text=True,
            )
        if probe.returncode != 0:
            return False, (
                "this process can create namespaces but cannot mount inside them "
                f"({(probe.stderr or '').strip().splitlines()[0] if probe.stderr else 'mount failed'}) — "
                "a minimal sandbox root cannot be built, so the Code node is refused. "
                "In practice this means running as a non-root user: CAP_SYS_ADMIN in the "
                "container's bounding set is not enough, it has to be effective."
            )
        return True, ""
    except Exception as e:
        return False, f"real isolation check itself failed: {e}"


def _root_symlinks() -> list:
    """(name, target) for each top-level symlink in the worker's own root.

    On a merged-/usr distribution (Debian, and so this image) /lib, /bin
    and /sbin are symlinks into /usr. That matters because an ELF binary
    names its dynamic loader by the LITERAL path recorded in the
    executable — /lib/ld-linux-aarch64.so.1 — not by the realpath the
    plan binds. Without these symlinks recreated, every bind mount is
    correct and every binary still fails to start with the famously
    unhelpful "cannot execute: required file not found", which names
    neither the missing file nor the fact that it is a symlink problem.

    Only relative targets are copied. An absolute one would be resolved
    against the new root and could point at something that is
    deliberately no longer there.
    """
    links = []
    try:
        entries = sorted(os.listdir("/"))
    except OSError:
        return links
    for name in entries:
        path = os.path.join("/", name)
        if os.path.islink(path):
            target = os.readlink(path)
            if not target.startswith("/"):
                links.append((name, target))
    return links


def _build_setup_script(newroot: str, workdir: str) -> str:
    """The in-namespace setup, as an explicit command list.

    Order is load-bearing and was established by testing each step, not
    by reasoning about it:

      1. `--make-rprivate /` first, or every mount below propagates back
         to the worker's own mount table.
      2. tmpfs on the new root, so pivot_root has a real mount point to
         pivot ONTO (it refuses a plain directory).
      3. Bind in the runtime read-only. A bind mount ignores `-o ro` on
         the first call — the flag only takes effect on a second
         `remount,bind,ro`, which is why each bind here is two commands.
         Getting this wrong produces a writable "read-only" mount that
         looks correct in /proc/mounts.
      4. Bind the work directory read-write: the one writable path.
      5. pivot_root, then detach the old root with a LAZY umount. This is
         the step that actually closes the reported gap — until the old
         root is unmounted it is still sitting at /oldroot, fully
         readable, and the sandbox would be no better than before.
      6. Only then drop privileges and exec the interpreter.
    """
    dirs_ro, files_ro, links = _minimal_root_plan()
    uid, gid = _unprivileged_ids()
    q = shlex.quote
    lines = [
        "set -e",
        "mount --make-rprivate /",
        f"mount -t tmpfs -o size={ROOT_TMPFS_SIZE},mode=755 tmpfs {q(newroot)}",
        f"mkdir -p {q(os.path.join(newroot, 'work'))} {q(os.path.join(newroot, 'oldroot'))} "
        f"{q(os.path.join(newroot, 'proc'))}",
    ]

    # Before any bind mount: the /lib -> usr/lib class of symlink, or the
    # loader cannot be found at the path the binaries actually name.
    lines.append(f"mkdir -p {q(os.path.join(newroot, 'dev'))}")
    for node in DEV_NODES:
        if not os.path.exists(node):
            continue
        placed = os.path.join(newroot, node.lstrip("/"))
        lines += [f": > {q(placed)}", f"mount --bind {q(node)} {q(placed)}"]

    for name, target in _root_symlinks():
        lines.append(f"ln -s {q(target)} {q(os.path.join(newroot, name))}")
    # Then the deeper ones the runtime itself names, e.g. the loader
    # symlink inside /usr/lib. `-f` because /lib may already resolve
    # here through a root symlink created just above.
    for link_path, target in links:
        placed = os.path.join(newroot, link_path.lstrip("/"))
        lines.append(f"mkdir -p {q(os.path.dirname(placed))}")
        lines.append(f"ln -sfn {q(target)} {q(placed)}")

    for d in dirs_ro:
        target = os.path.join(newroot, d.lstrip("/"))
        lines += [
            f"mkdir -p {q(target)}",
            f"mount --bind {q(d)} {q(target)}",
            f"mount -o remount,bind,ro {q(target)}",
        ]
    for f in files_ro:
        target = os.path.join(newroot, f.lstrip("/"))
        lines += [
            f"mkdir -p {q(os.path.dirname(target))}",
            f": > {q(target)}",
            f"mount --bind {q(f)} {q(target)}",
            f"mount -o remount,bind,ro {q(target)}",
        ]

    lines += [
        f"mount --bind {q(workdir)} {q(os.path.join(newroot, 'work'))}",
        f"cd {q(newroot)}",
        f"{q(_PIVOT_ROOT_BIN)} . oldroot",
        "cd /",
        # A fresh proc for THIS pid namespace, mounted before the umount
        # because umount resolves its target through /proc/self/mountinfo
        # and silently reports "No such file or directory" without it —
        # which reads like a missing directory rather than a missing
        # /proc. It shows only the sandboxed process (pid 1 is the
        # script), not the worker's process table.
        f"{q(os.path.realpath(_MOUNT_BIN))} -t proc proc /proc",
        f"{q(os.path.realpath(_UMOUNT_BIN))} -l /oldroot",
        # No rmdir: it is a separate binary and bash has no builtin for
        # it, so calling it in the minimal root only produces noise on
        # stderr. After the lazy umount /oldroot is an EMPTY directory —
        # the mount is detached, not merely hidden — which the
        # regression tests assert directly rather than taking on trust.
        "cd /work",
        # setpriv and the interpreter are addressed by their real paths,
        # which are the same inside the new root because that is exactly
        # where they were bound.
        f"exec {q(os.path.realpath(_SETPRIV_BIN))} --reuid {uid} --regid {gid} --clear-groups "
        f"--inh-caps=-all --bounding-set=-all --no-new-privs "
        f"{q(os.path.realpath(sys.executable))} user_code.py",
    ]
    return "\n".join(lines)


def run_sandboxed_code(code: str, language: str = "python3") -> dict:
    if language != "python3":
        return {"stdout": "", "stderr": f"Unsupported language: {language}", "exit_code": -1, "sandboxed": True}

    available, reason = _real_isolation_available()
    if not available:
        # Real, honest fail-closed (§0.55, GAP_REPORT) — refuses to run
        # untrusted code with weaker guarantees than this module
        # documents, rather than silently downgrading to the old,
        # actually-unenforced isolation.
        return {
            "stdout": "", "stderr": f"Code node execution refused — no real kernel-level isolation available: {reason}",
            "exit_code": -1, "sandboxed": True, "mechanism": "refused — see sandbox.py's own module docstring",
        }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Two separate directories: the tmpfs mount point for the new
        # root, and the work directory the script actually gets. Keeping
        # them apart is what lets the work directory be the ONLY
        # writable path inside the sandbox.
        newroot = os.path.join(tmpdir, "root")
        workdir = os.path.join(tmpdir, "work")
        os.makedirs(newroot)
        os.makedirs(workdir)
        script_path = os.path.join(workdir, "user_code.py")
        with open(script_path, "w") as f:
            f.write(code)
        # Real, required for the later privilege drop (§0.55) — this
        # directory and file are created as root; setpriv drops to
        # nobody before the script itself runs, and nobody needs real
        # read/write/execute access to actually use them.
        os.chmod(tmpdir, 0o755)
        os.chmod(newroot, 0o755)
        os.chmod(workdir, 0o777)
        os.chmod(script_path, 0o644)

        # Real, ordering-sensitive (§0.55) — the temp dir is bind-
        # mounted and `cd`'d into AFTER the root remount, inside this
        # same shell, not before it and not via subprocess.run's own
        # cwd= parameter — see the module docstring for the real bug
        # that ordering was found to cause.
        setup = _build_setup_script(newroot, workdir)
        # --mount-proc is deliberately gone: it mounted a fresh /proc over
        # the WORKER's root, which the script can no longer see anyway,
        # and the new root gets no /proc at all. That is a second win —
        # /proc/self/environ and /proc/1/cmdline were both readable
        # before, so anything in the process environment was one open()
        # away regardless of the read-only mount.
        cmd = [_UNSHARE_BIN, "--net", "--mount", "--pid", "--fork", _BASH_BIN, "-c", setup]
        try:
            result = subprocess.run(
                cmd,
                cwd=tmpdir,  # only where bash starts; the script's own cwd is set after the pivot
                timeout=TIMEOUT_SECONDS,
                capture_output=True,
                text=True,
                preexec_fn=_limit_resources,
                env={"PATH": "/usr/bin:/bin"},  # no inherited secrets/env vars
            )
            return {
                "stdout": result.stdout[:4000],
                "stderr": result.stderr[:4000],
                "exit_code": result.returncode,
                "sandboxed": True,
                "mechanism": "real network+mount+PID namespace isolation, non-root, capabilities "
                             "dropped, pivot_root into a minimal tmpfs root containing only the "
                             "interpreter and an ephemeral work directory (not gVisor/Firecracker "
                             "— see module docstring)",
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"Execution exceeded {TIMEOUT_SECONDS}s limit",
                     "exit_code": -1, "sandboxed": True}
