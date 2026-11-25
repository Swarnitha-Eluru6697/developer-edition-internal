"""Local disk is not a safe production default for uploads.

The reported risk, and it is not hypothetical — reproduced against real
containers before this was written:

  * LOCAL_STORAGE_DIR defaults to /var/inaiera-uploads INSIDE the
    container, and this repo's compose file mounts no volume there. An
    upload therefore lives in the container's writable layer.
  * Replace the container (a rolling deploy does this routinely) and the
    download returns 502 "no real object at key …".
  * Start a second replica against the SAME database and the download
    returns the same 502 there: the per-row storage_backend fix records
    WHICH backend holds an object, but no column can record which pod's
    private filesystem.

Both were demonstrated live, and object storage passed both in the same
harness. So production must either use object storage or explicitly
declare that its local path is a durable shared volume — refused at
startup, because discovering this once a file is already gone is too
late.

The container-level restart and multi-replica proof needs Docker and is
run out of band (see the commit message / task notes); these tests pin
the guard and the same property at the layer a test suite can reach: two
independently constructed backends, which is what two replicas are.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import storage as storage_module
from app.storage import (LOCAL_DURABLE_ENV, LocalDiskStorageBackend, StorageError,
                         check_production_storage_requirement, local_storage_declared_durable)


class TestProductionRefusesEphemeralLocalStorage:
    def test_local_storage_is_refused_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        monkeypatch.delenv(LOCAL_DURABLE_ENV, raising=False)

        with pytest.raises(RuntimeError) as exc:
            check_production_storage_requirement()
        message = str(exc.value)
        # The refusal has to say what to do, or it is just an obstacle.
        assert "STORAGE_BACKEND=s3" in message
        assert LOCAL_DURABLE_ENV in message

    def test_object_storage_is_accepted_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        check_production_storage_requirement()  # must not raise

    def test_an_explicit_durable_declaration_is_honoured(self, monkeypatch):
        """A deployment that really does mount a shared volume must not
        be blocked — it says so, and takes responsibility."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        monkeypatch.setenv(LOCAL_DURABLE_ENV, "true")
        check_production_storage_requirement()  # must not raise

    def test_development_is_left_alone(self, monkeypatch):
        """Local disk is a perfectly reasonable default for a dev
        machine; the whole point is that production is different."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        monkeypatch.delenv(LOCAL_DURABLE_ENV, raising=False)
        check_production_storage_requirement()  # must not raise

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("", False), ("maybe", False),
    ])
    def test_the_durability_declaration_is_read_strictly(self, monkeypatch, value, expected):
        monkeypatch.setenv(LOCAL_DURABLE_ENV, value)
        assert local_storage_declared_durable() is expected

    def test_startup_performs_the_check(self):
        """It must fail at boot, not at the first upload — by which time
        a deploy has already succeeded and files are already landing
        somewhere that is about to be thrown away."""
        import inspect

        from app import main

        src = inspect.getsource(main)
        assert "check_production_storage_requirement()" in src


class TestTheRiskIsReal:
    """Guards the diagnosis, not just the fix. If local storage ever
    became shared between processes, these would fail and the reasoning
    above would need re-reading."""

    def test_a_second_backend_instance_sees_the_same_local_directory(self, tmp_path):
        """Two backends on the SAME path do share — which is exactly why
        the fix is about the path being a shared volume, not about the
        backend being local."""
        key = f"probe/{uuid.uuid4().hex}.txt"
        first = LocalDiskStorageBackend(root=str(tmp_path))
        first.put(key, b"payload", "text/plain")
        second = LocalDiskStorageBackend(root=str(tmp_path))
        assert second.get(key) == b"payload"

    def test_a_backend_on_a_different_path_cannot_see_it(self, tmp_path):
        """And this is the replica case: a second pod's /var/inaiera-uploads
        is a different filesystem with the same path, so the object is
        simply absent — the 502 reproduced live against two containers."""
        key = f"probe/{uuid.uuid4().hex}.txt"
        LocalDiskStorageBackend(root=str(tmp_path / "pod-a")).put(key, b"payload", "text/plain")
        other_pod = LocalDiskStorageBackend(root=str(tmp_path / "pod-b"))
        assert other_pod.exists(key) is False
        with pytest.raises(StorageError):
            other_pod.get(key)


class TestTheDefaultIsStillLocalForDevelopment:
    def test_the_code_default_is_documented_as_a_development_default(self):
        """Not changed to s3: a developer with no bucket must still be
        able to run this. The guard is what makes that safe, by making
        production the case that has to be deliberate."""
        import inspect

        src = inspect.getsource(storage_module)
        assert 'os.getenv("STORAGE_BACKEND", "local")' in src
        # And the reason it is unsafe in production is written down next
        # to the guard rather than left for the next reader to rediscover.
        assert "writable layer" in src
