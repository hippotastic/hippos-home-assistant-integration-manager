"""Install a validated package and keep the last installation recoverable."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile

from homeassistant import components
from packaging.version import Version

from .const import DOMAIN, DOMAIN_PATTERN, MARKER, SHA_PATTERN
from .models import Installation, ManagerError, Package, Revision


class Installer:
    """Own a single custom component directory and its recovery data."""

    def __init__(self, config_dir: str | Path, domain: str, repository_id: int) -> None:
        """Derive all writable paths from a validated Home Assistant domain."""
        if DOMAIN_PATTERN.fullmatch(domain) is None or domain == DOMAIN:
            raise ManagerError("protected_domain")
        self.domain = domain
        self.repository_id = repository_id
        self._components = Path(config_dir).resolve() / "custom_components"
        self._control = self._components / f".{DOMAIN}"
        self._work = self._control / domain
        self._transaction = self._work / "transaction"
        self._previous = self._work / "previous"
        self._target = self._components / domain

    def inspect(self) -> tuple[bool, Installation | None, Installation | None]:
        """Recover an interrupted exchange and report current and prior ownership."""
        with self._locked():
            current = self._read_marker(self._target)
            previous = self._read_marker(self._previous)
            if previous is not None and previous.repository_id != self.repository_id:
                previous = None
            return self._target.exists(), current, previous

    def takeover_version(self) -> str:
        """Read the version currently installed, independently of GitHub."""
        with self._locked():
            return self._read_local_version()

    def claim_marketplace(self, expected_version: str, *, detached: bool) -> None:
        """Claim an existing directory without replacing its integration files."""
        with self._locked():
            if self._read_local_version() != expected_version:
                raise ManagerError("installation_changed")

            # The Marketplace may have installed a release asset or an older ref.
            # The files have a known version but no independently verified SHA.
            installation = Installation(self.repository_id, None, expected_version, expected_version)
            current = self._read_marker(self._target)
            if current == installation:
                return
            if current is not None or detached:
                raise ManagerError("installation_changed")
            self._write_marker(self._target, installation)

    def install(
        self,
        package: Package,
        revision: Revision,
        *,
        adopt: bool = False,
        expected: Installation | None = None,
    ) -> Installation:
        """Stage a package, then move it into place once the target is validated."""
        if package.domain != self.domain:
            raise ManagerError("domain_mismatch")
        if revision.stable and package.version.removeprefix("v") != revision.version:
            raise ManagerError("version_mismatch")
        installation = Installation(
            self.repository_id, revision.sha, package.version, revision.version
        )

        with self._locked():
            current = self._read_marker(self._target)
            if current is not None and current.repository_id != self.repository_id:
                raise ManagerError("installation_owned")
            if current != expected:
                raise ManagerError("installation_changed")
            if self._target.exists() and current is None:
                if not adopt:
                    raise ManagerError("installation_owned")
                manifest = self._read_object(self._target / "manifest.json", required=True)
                if manifest is None or manifest.get("domain") != self.domain:
                    raise ManagerError("installation_owned")

            staged = self._transaction / "new"
            staged.mkdir(parents=True)
            try:
                for relative, contents in package.files.items():
                    destination = staged / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as file:
                        file.write(contents)
                self._write_marker(staged, installation)
                self._exchange()
            except OSError:
                self._recover()
                raise
        return installation

    def rollback(self, expected: Installation, backup: Installation | None) -> Installation:
        """Restore the retained version and pause the update channel."""
        with self._locked():
            current = self._read_marker(self._target)
            previous = self._read_marker(self._previous)
            if (
                current != expected
                or current.repository_id != self.repository_id
                or previous is None
                or previous != backup
                or previous.repository_id != self.repository_id
                or previous.sha == current.sha
            ):
                raise ManagerError("rollback_unavailable")
            if any(
                path.is_symlink() or not (path.is_file() or path.is_dir())
                for path in self._previous.rglob("*")
            ):
                raise ManagerError("unsafe_installation")

            paused = replace(previous, paused=True)
            self._transaction.mkdir()
            staged = self._transaction / "new"
            try:
                shutil.copytree(self._previous, staged)
                self._write_marker(staged, paused)
                self._exchange()
            except OSError:
                self._recover()
                raise
        return paused

    def resume(self, expected: Installation) -> None:
        """Resume update checks for an existing managed installation."""
        with self._locked():
            current = self._read_marker(self._target)
            if current != expected or current.repository_id != self.repository_id:
                raise ManagerError("installation_changed")
            self._write_marker(self._target, replace(current, paused=False))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # The file lock still protects the exchange if the awaiting HA task is cancelled.
        try:
            core_manifest = Path(components.__file__).parent / self.domain / "manifest.json"
            if core_manifest.is_file():
                raise ManagerError("protected_domain")
            for directory in (self._components, self._control, self._work):
                self._check_directory(directory)
                if not directory.exists():
                    directory.mkdir(mode=0o700, exist_ok=True)
                    self._sync_path(directory.parent, os.O_DIRECTORY)
            descriptor = os.open(
                self._work / "lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600
            )
            with os.fdopen(descriptor, "ab") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for directory in (self._target, self._previous, self._transaction):
                    self._check_directory(directory)
                self._recover()
                yield
        except OSError:
            raise ManagerError("filesystem_error") from None

    def _exchange(self) -> None:
        staged = self._transaction / "new"
        for directory, _, files in os.walk(staged, topdown=False):
            for name in files:
                self._sync_path(Path(directory) / name)
            self._sync_path(Path(directory), os.O_DIRECTORY)
        self._sync_path(self._transaction, os.O_DIRECTORY)
        self._sync_path(self._work, os.O_DIRECTORY)

        if self._target.exists():
            self._target.rename(self._transaction / "old")
            self._sync_path(self._components, os.O_DIRECTORY)
            self._sync_path(self._transaction, os.O_DIRECTORY)

        # Moving in the staged tree is the commit point. Recovery uses the old
        # tree if interrupted before this move, or retains it as the backup after.
        staged.rename(self._target)
        self._sync_path(self._components, os.O_DIRECTORY)
        self._sync_path(self._transaction, os.O_DIRECTORY)
        self._recover()

    def _recover(self) -> None:
        if not self._transaction.exists():
            return
        old = self._transaction / "old"
        self._check_directory(old)
        if old.exists():
            if self._target.exists():
                if self._previous.exists():
                    shutil.rmtree(self._previous)
                old.rename(self._previous)
                self._sync_path(self._work, os.O_DIRECTORY)
            else:
                old.rename(self._target)
                self._sync_path(self._components, os.O_DIRECTORY)
            self._sync_path(self._transaction, os.O_DIRECTORY)
        shutil.rmtree(self._transaction)
        self._sync_path(self._work, os.O_DIRECTORY)

    @staticmethod
    def _check_directory(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ManagerError("unsafe_installation")

    @staticmethod
    def _read_object(path: Path, *, required: bool) -> dict | None:
        if path.is_symlink():
            raise ManagerError("unsafe_installation")
        if not path.exists() and not required:
            return None
        if not path.is_file() or path.stat().st_size > 65536:
            raise ManagerError("unsafe_installation")
        try:
            with path.open("rb") as file:
                value = json.loads(file.read(65537))
        except (ValueError, RecursionError):
            raise ManagerError("unsafe_installation") from None
        if not isinstance(value, dict):
            raise ManagerError("unsafe_installation")
        return value

    def _read_marker(self, directory: Path) -> Installation | None:
        value = self._read_object(directory / MARKER, required=False)
        if value is None:
            return None
        try:
            result = Installation(**value)
            if (
                type(result.repository_id) is not int
                or result.repository_id <= 0
                or (result.sha is not None and SHA_PATTERN.fullmatch(result.sha) is None)
                or not isinstance(result.version, str)
                or len(result.version) > 64
                or not isinstance(result.label, str)
                or not 1 <= len(result.label) <= 142
                or not result.label.isprintable()
                or type(result.paused) is not bool
            ):
                raise ValueError
            Version(result.version)
        except (TypeError, ValueError):
            raise ManagerError("unsafe_installation") from None
        return result

    def _read_local_version(self) -> str:
        manifest = self._read_object(self._target / "manifest.json", required=True)
        version = manifest.get("version") if manifest and manifest.get("domain") == self.domain else None
        if not isinstance(version, str) or len(version) > 64 or not version.isprintable():
            raise ManagerError("unsafe_installation")
        try:
            Version(version)
        except ValueError:
            raise ManagerError("unsafe_installation") from None
        return version

    def _write_marker(self, directory: Path, installation: Installation) -> None:
        # The existing marker doubles as the durable rollback-pause state.
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f"{MARKER}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(asdict(installation), file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, directory / MARKER)
            self._sync_path(directory, os.O_DIRECTORY)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _sync_path(path: Path, flags: int = 0) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
