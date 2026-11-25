"""Validate GitHub ZIP archives without extracting untrusted paths."""

from io import BytesIO
import json
from pathlib import PurePosixPath
import stat
import struct
import unicodedata
from zipfile import BadZipFile, ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED
import zlib

from homeassistant.const import __version__ as HA_VERSION
from packaging.version import InvalidVersion, Version

from .const import DOMAIN_PATTERN, MARKER
from .models import ManagerError, Package

_MAX_ENTRIES = 5000
_MAX_DIRECTORY = 512 * 1024
_MAX_EXPANDED = 128 * 1024 * 1024
_MAX_FILE = 24 * 1024 * 1024
_MAX_MANIFEST = 64 * 1024


def read_packages(contents: bytes, domain: str | None = None) -> dict[str, Package]:
    """Read ordinary files under an integration's directory or the repository root."""
    # zipfile allocates the index eagerly, before we can count its members.
    end = contents.rfind(b"PK\x05\x06", max(0, len(contents) - 65557))
    if end < 0 or end + 22 > len(contents):
        raise ManagerError("invalid_archive")
    disk, index_disk, local_count, count, size, offset, comment = struct.unpack_from(
        "<4H2IH", contents, end + 4
    )
    if (
        disk or index_disk or local_count != count or not 0 < count <= _MAX_ENTRIES
        or size > _MAX_DIRECTORY or offset + size != end or end + 22 + comment != len(contents)
    ):
        raise ManagerError("unsafe_archive")
    try:
        with ZipFile(BytesIO(contents)) as archive:
            paths = _index(archive.infolist())
            if (metadata := paths.get(("hacs.json",))) is not None:
                _check_repository_metadata(archive, metadata)
            roots = {
                parts[:2]
                for parts in paths
                if len(parts) == 3
                and parts[0] == "custom_components"
                and parts[-1] == "manifest.json"
                and (domain is None or parts[1] == domain)
            }
            if not roots and ("manifest.json",) in paths and ("__init__.py",) in paths:
                roots.add(())

            packages = {}
            for root in sorted(roots):
                info = paths[(*root, "manifest.json")]
                if info.file_size > _MAX_MANIFEST:
                    raise ManagerError("invalid_manifest")
                manifest = json.loads(archive.read(info))
                integration, name, version = _manifest(manifest)
                if root and integration != root[1]:
                    raise ManagerError("domain_mismatch")
                if domain is not None and integration != domain:
                    continue
                if integration in packages or (*root, "__init__.py") not in paths:
                    raise ManagerError("invalid_manifest")
                files = {}
                for parts, member in paths.items():
                    if parts[: len(root)] != root:
                        continue
                    relative = parts[len(root) :]
                    if not root and relative[0] in {"custom_components", ".git", ".github"}:
                        # A root layout cannot carry other component or workflow trees.
                        continue
                    if any(part.casefold() in {MARKER, "__pycache__", ".git"} for part in relative):
                        raise ManagerError("unsafe_archive")
                    files[PurePosixPath(*relative).as_posix()] = archive.read(member)
                packages[integration] = Package(integration, name, version, files)
            if not packages:
                raise ManagerError("integration_missing")
        return packages
    except (BadZipFile, EOFError, RuntimeError, ValueError, RecursionError, zlib.error):
        raise ManagerError("invalid_archive") from None


def _index(members: list[ZipInfo]) -> dict[tuple[str, ...], ZipInfo]:
    if not members or len(members) > _MAX_ENTRIES:
        raise ManagerError("unsafe_archive")

    roots = set()
    paths = {}
    spellings = {}
    files = set()
    directories = set()
    seen = set()
    size = 0
    for member in members:
        name = member.orig_filename
        directory = member.is_dir()
        parts = tuple(name.removesuffix("/").split("/"))
        kind = stat.S_IFMT(member.external_attr >> 16)
        if (
            len(name) > 768
            or len(parts) > 24
            or any(
                part in {"", ".", ".."}
                or len(part.encode("utf-8")) > 255
                or part.endswith((" ", "."))
                for part in parts
            )
            or any(char in name for char in "\\:")
            or any(unicodedata.category(char).startswith("C") for char in name)
            or kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
            or member.flag_bits & 1
            or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
            or member.file_size > _MAX_FILE
            or (directory and member.file_size)
        ):
            raise ManagerError("unsafe_archive")

        # Detect duplicate names, case collisions and directory/file aliases.
        normalized = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
        if normalized in seen:
            raise ManagerError("unsafe_archive")
        seen.add(normalized)
        for index in range(1, len(parts) + 1):
            key = normalized[:index]
            if spellings.setdefault(key, parts[:index]) != parts[:index]:
                raise ManagerError("unsafe_archive")
        for index in range(1, len(parts)):
            parent = normalized[:index]
            if parent in files:
                raise ManagerError("unsafe_archive")
            directories.add(parent)
        if directory:
            directories.add(normalized)
        else:
            if len(parts) < 2 or normalized in directories:
                raise ManagerError("unsafe_archive")
            files.add(normalized)
            paths[parts[1:]] = member
        roots.add(parts[0])
        size += member.file_size
        if size > _MAX_EXPANDED or len(roots) > 1:
            raise ManagerError("unsafe_archive")

    return paths


def _check_repository_metadata(archive: ZipFile, info: ZipInfo) -> None:
    if info.file_size > _MAX_MANIFEST:
        raise ManagerError("invalid_repository_metadata")
    try:
        metadata = json.loads(archive.read(info))
        minimum = metadata.get("homeassistant") if isinstance(metadata, dict) else None
        if not isinstance(metadata, dict) or (
            minimum is not None and (not isinstance(minimum, str) or len(minimum) > 32)
        ):
            raise ValueError
        if minimum and Version(minimum) > Version(HA_VERSION):
            raise ManagerError("home_assistant_too_old")
    except (ValueError, RecursionError):
        raise ManagerError("invalid_repository_metadata") from None
    if metadata.get("persistent_directory"):
        raise ManagerError("persistent_directory_unsupported")


def _manifest(manifest: object) -> tuple[str, str, str]:
    if not isinstance(manifest, dict):
        raise ManagerError("invalid_manifest")
    domain, name, version = (manifest.get(key) for key in ("domain", "name", "version"))
    if (
        not isinstance(domain, str)
        or DOMAIN_PATTERN.fullmatch(domain) is None
        or not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 128
        or not name.isprintable()
        or not isinstance(version, str)
        or len(version) > 64
    ):
        raise ManagerError("invalid_manifest")
    try:
        Version(version)
    except InvalidVersion:
        raise ManagerError("invalid_manifest") from None
    return domain, name, version
