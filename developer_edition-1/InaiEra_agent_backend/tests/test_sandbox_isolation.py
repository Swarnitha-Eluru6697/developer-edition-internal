"""
Real tests for the sandbox.py security fix (section 0.55, GAP_REPORT).

The real bug, exactly as reported: the previous sandbox.py ran user
code via a plain subprocess with resource limits and a different cwd,
while its own docstring claimed "no network access, and no filesystem
access beyond a throwaway temp directory" - neither was actually
enforced. A script could read /etc/passwd via an absolute path (cwd
doesn't restrict that at all), and outbound network was only ever
blocked by this sandbox environment's own incidental egress proxy, not
by anything in sandbox.py itself.

The fix uses real Linux kernel primitives - unshare for network/mount/
PID namespaces, a real read-only remount of the root filesystem with
only the actual temp directory bind-mounted back read-write, and
setpriv to drop to a genuinely unprivileged UID with capabilities
dropped. These tests exercise the real, actual subprocess chain - no
mocking of the isolation mechanism itself, the same real code path a
live Code node run takes - and reproduce the exact two attack vectors
named in the bug report.

These tests require a real Linux environment where unshare/setpriv can
actually create namespaces (in practice: real root, or CAP_SYS_ADMIN) -
the same real constraint sandbox.py's own module docstring states. They
will legitimately fail-closed rather than silently pass if run
somewhere that can't provide that - see
TestRealFailClosedWhenNoIsolation for the test of that behavior itself.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sandbox import run_sandboxed_code, _real_isolation_available


class TestRealNetworkIsolation:
    def test_an_outbound_tcp_connection_genuinely_fails_at_the_kernel_level(self):
        """The exact class of gap the report named - the previous
        implementation's "network blocked" only ever happened because
        of this sandbox environment's own egress proxy, not because of
        anything sandbox.py did. This is a real, separate network
        namespace - it must fail even for an address a proxy would
        never see, like a raw connect() to a hardcoded IP."""
        result = run_sandboxed_code("""
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
s.connect(("8.8.8.8", 53))
print("connected")
""")
        assert "connected" not in result["stdout"]
        assert "Network is unreachable" in result["stderr"] or "unreachable" in result["stderr"].lower()

    def test_a_udp_socket_also_genuinely_fails(self):
        result = run_sandboxed_code("""
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("1.1.1.1", 53))
s.send(b"x")
print("sent")
""")
        assert "sent" not in result["stdout"]


class TestRealFilesystemIsolation:
    def test_the_exact_reported_exploit_reading_etc_shadow_is_genuinely_blocked(self):
        """/etc/shadow was the original report's protected-file case.
        It used to be blocked by the privilege drop ("Permission
        denied"); it is now blocked because /etc is not in the sandbox
        root at all. Either refusal is acceptable to this test — what
        must never happen is the content coming back."""
        result = run_sandboxed_code('print(open("/etc/shadow").read())')
        assert "root:" not in result["stdout"]
        assert result["exit_code"] != 0
        assert ("Permission denied" in result["stderr"]
                or "No such file or directory" in result["stderr"])

    def test_writing_outside_the_temp_directory_is_genuinely_blocked(self):
        """Now refused because /tmp does not exist in the sandbox root,
        where it used to be refused as a read-only filesystem. The
        property that matters is unchanged and is what is asserted:
        nothing appears on the worker."""
        result = run_sandboxed_code('open("/tmp/sandbox_escape_test.txt", "w").write("bad")')
        assert result["exit_code"] != 0
        assert ("Read-only file system" in result["stderr"]
                or "No such file or directory" in result["stderr"])
        assert not os.path.exists("/tmp/sandbox_escape_test.txt")

    def test_writing_to_this_applications_own_source_tree_is_genuinely_blocked(self):
        """The real, concrete scenario the bug report's own example
        (open("/app/...")) was pointing at - can the sandboxed code
        tamper with the real application it's running inside."""
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(app_dir, "app", "main.py").replace("\\", "/")
        result = run_sandboxed_code(f'open({target!r}, "a").write("MALICIOUS")')
        assert result["exit_code"] != 0
        assert ("Read-only file system" in result["stderr"]
                or "Permission denied" in result["stderr"]
                or "No such file or directory" in result["stderr"])
        with open(os.path.join(app_dir, "app", "main.py")) as f:
            assert "MALICIOUS" not in f.read()

    def test_the_scripts_own_temp_directory_genuinely_stays_writable(self):
        """A real, necessary regression guard - the read-only root
        filesystem must not also lock out the one directory the script
        is actually supposed to be able to use."""
        result = run_sandboxed_code("""
open("real_output.txt", "w").write("ok")
print(open("real_output.txt").read())
""")
        assert result["stdout"].strip() == "ok"
        assert result["exit_code"] == 0


class TestRealPrivilegeAndProcessIsolation:
    def test_the_script_genuinely_runs_as_a_real_unprivileged_uid(self):
        result = run_sandboxed_code("import os; print(os.getuid())")
        assert result["stdout"].strip() == "65534"  # the real nobody UID

    def test_the_script_genuinely_cannot_see_real_host_processes(self):
        """A real, separate PID namespace - the script is genuinely
        PID 1 inside it and the host's real process tree is invisible,
        not just conceptually isolated."""
        result = run_sandboxed_code("import os; print(os.getpid())")
        assert result["stdout"].strip() == "1"


class TestRealPreExistingLimitsStillWork:
    """A real regression guard - the new namespace/privilege isolation
    must not have broken the pre-existing resource limits."""

    def test_memory_limit_still_genuinely_enforced(self):
        result = run_sandboxed_code("x = bytearray(500 * 1024 * 1024)")
        assert result["exit_code"] != 0
        assert "MemoryError" in result["stderr"]

    def test_wall_clock_timeout_still_genuinely_enforced(self):
        result = run_sandboxed_code("import time; time.sleep(30)")
        assert result["exit_code"] == -1
        assert "exceeded" in result["stderr"].lower()

    def test_normal_stdlib_code_still_genuinely_works(self):
        result = run_sandboxed_code("import json, math; print(json.dumps({'pi': round(math.pi, 2)}))")
        assert result["exit_code"] == 0
        assert '"pi": 3.14' in result["stdout"]


class TestRealFailClosedWhenNoIsolation:
    def test_the_real_live_check_function_reports_available_in_this_test_environment(self):
        """A real, direct confirmation that this test suite is
        actually exercising the real isolation path, not silently
        skipping past it - if this is False, every test above would
        be running against a fail-closed refusal, not real isolation."""
        available, reason = _real_isolation_available()
        assert available is True, f"real isolation unavailable in this test run: {reason}"

    def test_a_missing_unshare_binary_produces_a_real_honest_refusal_not_a_silent_downgrade(self, monkeypatch):
        import app.sandbox as sandbox_module
        monkeypatch.setattr(sandbox_module, "_UNSHARE_BIN", None)
        result = run_sandboxed_code("print('should never run')")
        assert result["exit_code"] == -1
        assert "should never run" not in result["stdout"]
        assert "refused" in result["stderr"].lower()
        assert result["mechanism"] == "refused — see sandbox.py's own module docstring"


class TestTheSandboxRootIsMinimalNotMerelyReadOnly:
    """The reported BLOCKER/HIGH, as tests.

    "The untrusted process still sees the worker/container root
    filesystem and can read any file permitted to the sandbox UID."
    That was true: the root was remounted read-only, which stops writes
    and does nothing about reads — and since run_sandboxed_code hands
    stdout back to the agent runtime, a readable file is an
    exfiltratable one even with the network namespace empty.

    The release criterion asked for proof that reading representative
    worker files fails. These assert the stronger property the fix
    actually provides: those paths are not present in the sandbox root
    at all.
    """

    # Representative of the four categories the report named: identity
    # files, this application's own source, its configuration, and
    # whatever happens to be mounted into the worker.
    WORKER_FILES = [
        "/etc/passwd",
        "/etc/shadow",
        "/etc/hosts",
        "/etc/hostname",
        "/app/app/sandbox.py",
        "/app/app/main.py",
        "/app/app/secrets_crypto.py",
        "/app/alembic.ini",
        "/app/requirements.txt",
    ]
    # NOT in the list above, deliberately: /proc/1/environ IS readable,
    # and that is correct rather than a leak. In the sandbox's own PID
    # namespace pid 1 is the script itself, so what it exposes is the
    # script's own environment — which the test below pins to the
    # minimal set, since that is the property actually worth asserting.

    def test_none_of_the_representative_worker_files_can_be_read(self):
        probe = "\n".join([
            "import os",
            f"paths = {self.WORKER_FILES!r}",
            "for p in paths:",
            "    try:",
            "        data = open(p, 'rb').read(64)",
            "        print('READ', p, len(data))",
            "    except OSError as e:",
            "        print('BLOCKED', p, type(e).__name__)",
        ])
        result = run_sandboxed_code(probe)
        assert result["exit_code"] == 0, result["stderr"]
        for path in self.WORKER_FILES:
            assert f"BLOCKED {path}" in result["stdout"], (
                f"{path} was readable inside the sandbox:\n{result['stdout']}"
            )
        assert "READ " not in result["stdout"]

    def test_the_process_environment_carries_no_worker_secrets(self):
        """The environment is the other obvious exfiltration route, and
        the one a minimal filesystem does not by itself close: the
        worker process holds DATABASE_URL, provider API keys and the
        Fernet key, and any of them inherited into the sandbox would
        come straight back through stdout. run_sandboxed_code passes an
        explicit env of just PATH; this is the test that it stays that
        way."""
        result = run_sandboxed_code(
            "import os; print(sorted(os.environ))"
        )
        assert result["exit_code"] == 0, result["stderr"]
        names = set(eval(result["stdout"].strip()))
        # Set by the shell and the interpreter itself, not inherited.
        assert names <= {"PATH", "PWD", "OLDPWD", "SHLVL", "LC_CTYPE"}, (
            f"unexpected environment variables reached the sandbox: {names}"
        )
        for leaked in ("DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                       "SECRETS_ENCRYPTION_KEY", "AWS_SECRET_ACCESS_KEY",
                       "UCS_ADMIN_TOKEN", "ENVOY_INTERNAL_SECRET", "UCS_GATEWAY_SECRET"):
            assert leaked not in names

    def test_the_application_source_directory_is_not_even_listable(self):
        """Stronger than "cannot read main.py": the directory holding
        this application does not exist in the sandbox root, so there is
        nothing to enumerate and no filename to guess at."""
        result = run_sandboxed_code(
            "import os\n"
            "try:\n"
            "    print('LISTED', os.listdir('/app'))\n"
            "except OSError as e:\n"
            "    print('BLOCKED', type(e).__name__)\n"
        )
        assert "BLOCKED" in result["stdout"]
        assert "LISTED" not in result["stdout"]

    def test_the_sandbox_root_contains_only_the_expected_entries(self):
        """Pins the actual contents. A future change that binds
        something extra in — a config directory, a mounted secret, /etc
        "just for DNS" — has to update this list deliberately rather
        than widening the sandbox unnoticed."""
        result = run_sandboxed_code("import os; print(sorted(os.listdir('/')))")
        assert result["exit_code"] == 0, result["stderr"]
        entries = set(eval(result["stdout"].strip()))
        # bin/lib/sbin are recreated symlinks into usr on a merged-/usr
        # image; oldroot is the empty detach point.
        allowed = {"bin", "lib", "lib64", "sbin", "usr", "dev", "proc", "work", "oldroot"}
        assert entries <= allowed, f"unexpected entries in the sandbox root: {entries - allowed}"

    def test_the_old_root_is_detached_not_merely_moved_aside(self):
        """pivot_root alone would leave the entire worker filesystem
        sitting at /oldroot, readable — the gap reopened under a new
        name. The lazy umount is what actually removes it, and this is
        the test that would catch its absence."""
        result = run_sandboxed_code(
            "import os\n"
            "print('OLDROOT', sorted(os.listdir('/oldroot')))\n"
            "for p in ('/oldroot/etc/passwd', '/oldroot/app/app/main.py'):\n"
            "    try:\n"
            "        open(p, 'rb').read(8); print('READ', p)\n"
            "    except OSError as e:\n"
            "        print('BLOCKED', p, type(e).__name__)\n"
        )
        assert result["exit_code"] == 0, result["stderr"]
        assert "OLDROOT []" in result["stdout"], result["stdout"]
        assert "READ " not in result["stdout"]

    def test_only_the_sandboxed_process_is_visible_in_proc(self):
        """/proc is mounted fresh for this PID namespace rather than
        inherited, so the worker's process table — and every
        /proc/<pid>/environ in it — is not there."""
        result = run_sandboxed_code(
            "import os; print(sorted(p for p in os.listdir('/proc') if p.isdigit()))"
        )
        assert result["stdout"].strip() == "['1']", result["stdout"]

    def test_the_runtime_itself_is_read_only(self):
        """The interpreter and its standard library are bound in so the
        script can RUN; it must not be able to tamper with them, or one
        run could poison the next."""
        result = run_sandboxed_code(
            "import sysconfig, os\n"
            "target = os.path.join(sysconfig.get_paths()['stdlib'], 'json', '__init__.py')\n"
            "try:\n"
            "    open(target, 'a').write('# tampered')\n"
            "    print('WROTE')\n"
            "except OSError as e:\n"
            "    print('BLOCKED', type(e).__name__)\n"
        )
        assert "BLOCKED" in result["stdout"], result["stdout"]
        assert "WROTE" not in result["stdout"]

    def test_ordinary_stdlib_code_still_works_in_the_minimal_root(self):
        """The guard that makes the rest of this class safe to trust: a
        minimal root that broke normal code would be useless, and the
        temptation would be to bind more in until it worked."""
        result = run_sandboxed_code(
            "import json, math, hashlib, base64, re, datetime, random, itertools, collections\n"
            "print(json.dumps({'pi': round(math.pi, 2)}))\n"
            "print(hashlib.sha256(b'x').hexdigest()[:8])\n"
            "print(base64.b64encode(b'hi').decode())\n"
            "print(bool(re.match(r'\\\\d+', '42')), len(list(itertools.repeat(1, 3))))\n"
        )
        assert result["exit_code"] == 0, result["stderr"]
        assert '"pi": 3.14' in result["stdout"]
        assert "2d711642" in result["stdout"]
        assert "aGk=" in result["stdout"]

    def test_the_work_directory_is_the_only_writable_path(self):
        result = run_sandboxed_code(
            "import os\n"
            "open('mine.txt', 'w').write('ok')\n"
            "print('WORKDIR', open('mine.txt').read())\n"
            "for p in ('/x.txt', '/usr/x.txt', '/dev/x.txt', '/proc/x.txt', '/oldroot/x.txt'):\n"
            "    try:\n"
            "        open(p, 'w').write('bad'); print('WROTE', p)\n"
            "    except OSError as e:\n"
            "        print('BLOCKED', p, type(e).__name__)\n"
        )
        assert result["exit_code"] == 0, result["stderr"]
        assert "WORKDIR ok" in result["stdout"]
        assert "WROTE " not in result["stdout"]


class TestTheMinimalRootPlanItself:
    """Unit-level checks that need no privileges, so they run even where
    namespace creation is unavailable."""

    def test_the_plan_never_includes_the_application_source_or_etc(self):
        from app.sandbox import _minimal_root_plan

        dirs, files, _links = _minimal_root_plan()
        for path in list(dirs) + list(files):
            assert not path.startswith("/etc"), f"plan would bind {path} into the sandbox"
            assert not path.startswith("/app/"), f"plan would bind {path} into the sandbox"
            assert path != "/", "plan would bind the whole root — the reported bug"

    def test_the_plan_includes_the_interpreter_and_its_stdlib(self):
        import sys as _sys
        import sysconfig

        from app.sandbox import _minimal_root_plan

        dirs, files, _links = _minimal_root_plan()
        assert os.path.realpath(_sys.executable) in files
        stdlib = sysconfig.get_paths()["stdlib"]
        assert any(stdlib == d or stdlib.startswith(d.rstrip("/") + "/") for d in dirs), (
            f"the standard library at {stdlib} is not covered by {dirs}"
        )

    def test_isolation_is_refused_when_pivot_root_is_unavailable(self, monkeypatch):
        """Fail-closed, consistent with the rest of this module: without
        pivot_root the old read-only-root behaviour would still "work"
        while leaving the worker filesystem readable, and silently
        downgrading to that is exactly what this must not do."""
        import app.sandbox as sandbox_module

        monkeypatch.setattr(sandbox_module, "_PIVOT_ROOT_BIN", None)
        available, reason = sandbox_module._real_isolation_available()
        assert available is False
        assert "pivot_root" in reason

        result = run_sandboxed_code("print('should not run')")
        assert result["exit_code"] == -1
        assert "should not run" not in result["stdout"]
        assert "refused" in result["stderr"].lower()
