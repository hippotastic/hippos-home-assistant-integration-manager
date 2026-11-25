"""Check ownership, version consistency and interrupted file exchanges."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from custom_components.hippos_home_assistant_integration_manager.archive import read_packages
from custom_components.hippos_home_assistant_integration_manager.const import DOMAIN as MANAGER_DOMAIN
from custom_components.hippos_home_assistant_integration_manager.installer import Installer
from custom_components.hippos_home_assistant_integration_manager.models import ManagerError

from fixtures import DOMAIN, FIRST, REPOSITORY_ID, SECOND, make_archive


class InstallerTests(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.config = Path(directory)
        self.target = self.config / "custom_components" / DOMAIN
        self.work = self.target.parent / f".{MANAGER_DOMAIN}" / DOMAIN
        self.installer = Installer(directory, DOMAIN, REPOSITORY_ID)
        self.first = read_packages(make_archive(FIRST.version))[DOMAIN]
        self.second = read_packages(make_archive(SECOND.version))[DOMAIN]

    def test_update_rollback_and_explicit_resume(self):
        first = self.installer.install(self.first, FIRST)
        second = self.installer.install(self.second, SECOND, expected=first)
        self.assertEqual(self.installer.inspect(), (True, second, first))
        rolled_back = self.installer.rollback(second, first)
        self.assertEqual(rolled_back.sha, FIRST.sha)
        self.assertTrue(rolled_back.paused)
        self.assertEqual(self.installer.inspect(), (True, rolled_back, second))
        self.assertEqual((self.target / "blueprints/automation/example.yaml").read_text(), "1.0.0")
        self.installer.resume(rolled_back)
        self.assertFalse(self.installer.inspect()[1].paused)

    def test_adoption_requires_confirmation_and_leaves_config_alone(self):
        configuration = self.config / "configuration.yaml"
        configuration.write_text("example_extension: {}")
        self.target.mkdir(parents=True)
        manifest = self.target / "manifest.json"
        manifest.write_bytes(self.first.files["manifest.json"])
        (self.target / "local.py").write_text("# local")
        with self.assertRaises(ManagerError) as error:
            self.installer.install(self.second, SECOND)
        self.assertEqual(error.exception.key, "installation_owned")
        self.installer.install(self.second, SECOND, adopt=True)
        self.assertEqual((self.work / "previous/local.py").read_text(), "# local")
        self.assertFalse((self.target / "local.py").exists())
        self.assertEqual(configuration.read_text(), "example_extension: {}")

    def test_unrelated_repo_stale_install_and_mismatched_release_are_refused(self):
        first = self.installer.install(self.first, FIRST)
        for installer, package, revision, expected, reason in (
            (Installer(self.config, DOMAIN, 456), self.second, SECOND, None, "installation_owned"),
            (self.installer, self.second, SECOND, None, "installation_changed"),
            (self.installer, self.second, FIRST, first, "version_mismatch"),
        ):
            with self.subTest(reason=reason), self.assertRaises(ManagerError) as error:
                installer.install(package, revision, expected=expected)
            self.assertEqual(error.exception.key, reason)
        self.assertEqual(self.installer.inspect(), (True, first, None))

    def test_failure_between_directory_moves_restores_old_installation(self):
        first = self.installer.install(self.first, FIRST)
        rename = Path.rename

        def interrupted(source, target):
            if source.name == "new":
                raise OSError("Synthetic failure after moving the old tree")
            return rename(source, target)

        with patch.object(Path, "rename", interrupted), self.assertRaises(ManagerError) as error:
            self.installer.install(self.second, SECOND, expected=first)
        self.assertEqual(error.exception.key, "filesystem_error")
        self.assertEqual(self.installer.inspect(), (True, first, None))
        self.assertFalse((self.work / "transaction").exists())

    def test_new_process_finalizes_interrupted_exchange_after_commit(self):
        first = self.installer.install(self.first, FIRST)
        second = self.installer.install(self.second, SECOND, expected=first)
        transaction = self.work / "transaction"
        transaction.mkdir()
        (self.work / "previous").rename(transaction / "old")
        resumed = Installer(self.config, DOMAIN, REPOSITORY_ID)
        self.assertEqual(resumed.inspect(), (True, second, first))
        self.assertFalse(transaction.exists())

    def test_refuses_builtin_and_symlink_destinations(self):
        with self.assertRaises(ManagerError):
            Installer(self.config, "automation", REPOSITORY_ID).inspect()
        with self.assertRaises(ManagerError):
            Installer(self.config, MANAGER_DOMAIN, REPOSITORY_ID)
        unrelated = self.config / "unrelated"
        unrelated.mkdir()
        self.target.parent.mkdir(exist_ok=True)
        self.target.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaises(ManagerError) as error:
            self.installer.install(self.first, FIRST, adopt=True)
        self.assertEqual(error.exception.key, "unsafe_installation")
        self.assertEqual(list(unrelated.iterdir()), [])
