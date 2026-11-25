"""Synthetic repository fixtures shared by the tests."""

from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from custom_components.hippos_home_assistant_integration_manager.models import Revision

DOMAIN = "example_extension"
REPOSITORY = "example-owner/example-repository"
REPOSITORY_ID = 12345
TOKEN = "github_pat_" + "a" * 40
FIRST = Revision("a" * 40, "1.0.0", stable=True)
SECOND = Revision("b" * 40, "1.1.0", stable=True)


def make_archive(version: str, extras: tuple = (), *, root_layout: bool = False) -> bytes:
    """Build a snapshot with a loadable synthetic integration and a bundled asset."""
    root = "example-owner-example-repository-abcdef0"
    folder = root if root_layout else f"{root}/custom_components/{DOMAIN}"
    manifest = {
        "domain": DOMAIN,
        "name": "Example Extension",
        "documentation": "https://example.com/extension",
        "version": version,
    }
    files = (
        (f"{folder}/manifest.json", json.dumps(manifest)),
        (f"{folder}/__init__.py", f'"""Example Extension {version}."""\n'),
        (f"{folder}/blueprints/automation/example.yaml", version),
        *extras,
    )
    contents = BytesIO()
    with ZipFile(contents, "w", ZIP_DEFLATED) as zip_file:
        for name, text in files:
            zip_file.writestr(name, text)
    return contents.getvalue()


def unusual_file(path: str, mode: int) -> ZipInfo:
    """Create an archive member carrying a Unix file type."""
    result = ZipInfo(f"example-owner-example-repository-abcdef0/{path}")
    result.create_system = 3
    result.external_attr = mode << 16
    return result
