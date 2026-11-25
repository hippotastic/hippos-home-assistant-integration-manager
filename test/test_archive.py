"""Checks on the untrusted repository archive boundary."""

import stat
from unittest import TestCase
from unittest.mock import patch

from custom_components.hippos_home_assistant_integration_manager.archive import read_packages
from custom_components.hippos_home_assistant_integration_manager.models import ManagerError

from fixtures import DOMAIN, make_archive, unusual_file


class ArchiveTests(TestCase):
    def test_installs_only_the_selected_component_tree(self):
        archive = make_archive(
            "1.0.0",
            (("example-owner-example-repository-abcdef0/docs/readme.md", "other"),),
        )
        package = read_packages(archive, DOMAIN)[DOMAIN]
        self.assertEqual(package.files["blueprints/automation/example.yaml"], b"1.0.0")
        self.assertEqual(len(package.files), 3)

    def test_supports_component_at_repository_root(self):
        package = read_packages(make_archive("1.0.0", root_layout=True))[DOMAIN]
        self.assertIn("__init__.py", package.files)

    def test_rejects_unsafe_archive_members(self):
        for name in (
            "../escape",
            "/absolute",
            "example-owner-example-repository-abcdef0/dir/../../escape",
            "example-owner-example-repository-abcdef0/dir\\file",
            f"example-owner-example-repository-abcdef0/custom_components/{DOMAIN}/MANIFEST.JSON",
            f"example-owner-example-repository-abcdef0/custom_components/{DOMAIN}/.hippos-integration-manager.json",
            unusual_file("link", stat.S_IFLNK | 0o777),
            unusual_file("pipe", stat.S_IFIFO | 0o600),
        ):
            with self.subTest(name=name), self.assertRaises(ManagerError):
                read_packages(make_archive("1.0.0", ((name, "value"),)))

    def test_rejects_declared_expanded_size_limit(self):
        with patch(
            "custom_components.hippos_home_assistant_integration_manager.archive._MAX_EXPANDED", 10
        ), self.assertRaises(ManagerError) as error:
            read_packages(make_archive("1.0.0"))
        self.assertEqual(error.exception.key, "unsafe_archive")

    def test_rejects_integration_removed_from_selected_commit(self):
        with self.assertRaises(ManagerError) as error:
            read_packages(make_archive("1.0.0"), "other_extension")
        self.assertEqual(error.exception.key, "integration_missing")

    def test_enforces_home_assistant_compatibility_and_persistent_files(self):
        for metadata, reason in (
            ('{"homeassistant":"9999.1.0"}', "home_assistant_too_old"),
            ('{"persistent_directory":"local"}', "persistent_directory_unsupported"),
        ):
            with self.subTest(reason=reason), self.assertRaises(ManagerError) as error:
                read_packages(make_archive(
                    "1.0.0", (("example-owner-example-repository-abcdef0/hacs.json", metadata),)
                ))
            self.assertEqual(error.exception.key, reason)
