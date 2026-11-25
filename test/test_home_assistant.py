"""Exercise production config flows and update actions in Home Assistant Core."""

from dataclasses import replace
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from aiohttp import ClientSession

import custom_components

from homeassistant import loader
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.components import marketplace
from homeassistant.components.marketplace import base as marketplace_base
from homeassistant.components.marketplace.const import SIGNAL_REPOSITORY_INSTALLED
from homeassistant.components.marketplace.repositories.integration import IntegrationRepository
from homeassistant.components.marketplace.utils.storage import (
    async_load_from_storage,
    get_storage_for_key,
)
from homeassistant.components.update import UpdateEntityFeature, websocket_release_notes
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import Context, CoreState, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import WriteError

from custom_components.hippos_home_assistant_integration_manager import config_flow, coordinator, ha as manager_ha, takeover
from custom_components.hippos_home_assistant_integration_manager.archive import read_packages
from custom_components.hippos_home_assistant_integration_manager.const import CONF_MARKETPLACE_TAKEOVER, DOMAIN
from custom_components.hippos_home_assistant_integration_manager.github import GitHub
from custom_components.hippos_home_assistant_integration_manager.models import ManagerError

from fixtures import DOMAIN as TARGET, FIRST, REPOSITORY, REPOSITORY_ID, SECOND, TOKEN, make_archive


class HomeAssistantTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.config = Path(temporary)
        component = Path(__file__).parents[1] / "custom_components" / DOMAIN
        shutil.copytree(component, self.config / "custom_components" / DOMAIN)
        session = await self.enterAsyncContext(ClientSession())
        # Use the real Marketplace runtime without multicast or external refreshes.
        self.enterContext(
            patch.object(marketplace_base, "async_get_clientsession", return_value=session)
        )
        self.enterContext(patch.object(marketplace, "_async_start_once_started"))
        self.target = self.config / "custom_components" / TARGET
        # Network-less Docker has no multicast interface for HA's DNS resolver.
        # The GitHub client methods below are already replaced by fixtures.
        for module in (config_flow, coordinator):
            self.enterContext(patch.object(module, "async_get_clientsession", return_value=object()))
        self.repository_id = self.enterContext(
            patch.object(GitHub, "repository_id", return_value=REPOSITORY_ID)
        )
        self.latest = self.enterContext(patch.object(GitHub, "latest", return_value=FIRST))
        self.archive = self.enterContext(
            patch.object(
                GitHub, "archive",
                side_effect=lambda sha: make_archive(FIRST.version if sha == FIRST.sha else SECOND.version),
            )
        )
        await self._boot_home_assistant()

    async def _boot_home_assistant(self):
        self.hass = HomeAssistant(str(self.config))
        self.addAsyncCleanup(self._stop_home_assistant, self.hass)
        loader.async_setup(self.hass)
        self.assertIs(await async_from_config_dict({}, self.hass), self.hass)
        await self.hass.async_start()
        await self.hass.async_block_till_done()
        self.market_entry, = self.hass.config_entries.async_entries("marketplace")
        self.assertIs(self.market_entry.state, ConfigEntryState.LOADED)
        self.market = self.market_entry.runtime_data

    @staticmethod
    async def _stop_home_assistant(hass):
        if hass.state is not CoreState.stopped:
            await hass.async_stop(force=True)

    async def _marketplace_installation(self, *, catalog: bool = False):
        files = read_packages(make_archive(FIRST.version))[TARGET].files
        files["__init__.py"] = (
            b"async def async_setup_entry(hass, entry):\n    return True\n"
            b"async def async_unload_entry(hass, entry):\n    return True\n"
        )
        for path, content in files.items():
            destination = self.target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

        repository = IntegrationRepository(self.market, REPOSITORY)
        repository.data.id = str(REPOSITORY_ID)
        repository.data.domain = TARGET
        repository.data.installed = True
        repository.data.installed_version = FIRST.version
        repository.data.installed_commit = FIRST.sha
        self.market.repositories.register(repository, default=catalog)
        if not catalog:
            self.market.common.custom_repositories.add(repository.data.id)
        await self.market.data.async_write()
        async_dispatcher_send(self.hass, SIGNAL_REPOSITORY_INSTALLED, repository)

        await repository.reload_custom_components()
        entry = ConfigEntry(
            version=1, minor_version=1, domain=TARGET, title="Existing settings",
            data={"setting": "keep this"}, options={"custom_option": 7},
            source=SOURCE_USER, unique_id="existing-instance",
            discovery_keys={}, subentries_data=[],
        )
        await self.hass.config_entries.async_add(entry)
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(TARGET, "test-device")},
            name="Keep this device",
        )
        entity = er.async_get(self.hass).async_get_or_create(
            "sensor", TARGET, "test-reading", config_entry=entry, device_id=device.id,
            suggested_object_id="keep_this_sensor",
        )
        await self.hass.async_block_till_done()
        update = er.async_get(self.hass).async_get_entity_id("update", "marketplace", repository.data.id)
        self.assertIsNotNone(update)
        return repository, files, entry, device, entity, update

    async def _start_flow(self):
        result = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        return await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"repository": REPOSITORY, "token": TOKEN}
        )

    async def _configure(self):
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "confirm", result)
        self.assertFalse(self.target.exists())
        self.assertNotIn(TOKEN, repr(result))
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirm": True}
        )
        self.assertEqual(result["type"], "create_entry", result)
        entry = result["result"]
        await self.hass.async_block_till_done()
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        return entry

    async def test_fresh_config_flow_is_available_in_same_core_session(self):
        target = "example_first_install"
        self.target = self.target.with_name(target)
        # The unittest runner imported the workspace namespace before Core mounted the test config.
        self.enterContext(patch.object(custom_components, "__path__", [str(self.target.parent)]))
        self.archive.side_effect = lambda sha: make_archive(
            FIRST.version if sha == FIRST.sha else SECOND.version,
            config_flow=True, domain=target,
        )
        self.assertNotIn(target, await loader.async_get_config_flows(self.hass))

        manager_entry = await self._configure()
        self.assertIn(target, await loader.async_get_config_flows(self.hass))
        self.assertNotIn((DOMAIN, f"restart_{target}"), ir.async_get(self.hass).issues)

        flow = self.hass.config_entries.flow
        setup = await flow.async_init(target, context={"source": SOURCE_USER})
        self.assertEqual(setup["type"], "form", setup)
        setup = await flow.async_configure(setup["flow_id"], {})
        self.assertEqual(setup["type"], "create_entry", setup)
        await self.hass.async_block_till_done()
        self.assertIs(setup["result"].state, ConfigEntryState.LOADED)

        self.latest.return_value = SECOND
        await manager_entry.runtime_data.async_refresh()
        await manager_entry.runtime_data.async_install()
        self.assertIn((DOMAIN, f"restart_{target}"), ir.async_get(self.hass).issues)

    async def test_first_install_without_config_flow_requests_restart(self):
        await self._configure()
        self.assertNotIn(TARGET, await loader.async_get_config_flows(self.hass))
        self.assertIn((DOMAIN, f"restart_{TARGET}"), ir.async_get(self.hass).issues)

    async def test_first_install_requests_restart_if_discovery_fails(self):
        self.archive.side_effect = None
        self.archive.return_value = make_archive(FIRST.version, config_flow=True)
        with (
            patch.object(manager_ha, "async_get_custom_components", side_effect=OSError),
            self.assertLogs(manager_ha.__name__, level="ERROR"),
        ):
            await self._configure()
        self.assertTrue(self.target.is_dir())
        self.assertIn((DOMAIN, f"restart_{TARGET}"), ir.async_get(self.hass).issues)

    async def test_first_install_over_resolved_domain_requests_restart(self):
        self.archive.side_effect = None
        self.archive.return_value = make_archive(FIRST.version, config_flow=True)
        with (
            patch.object(manager_ha, "async_get_loaded_integration") as loaded,
            patch.object(manager_ha, "async_get_custom_components") as discovery,
        ):
            await self._configure()
        loaded.assert_called_once_with(self.hass, TARGET)
        discovery.assert_not_awaited()
        self.assertIn((DOMAIN, f"restart_{TARGET}"), ir.async_get(self.hass).issues)

    async def test_native_update_and_offline_rollback(self):
        entry = await self._configure()
        coordinator = entry.runtime_data
        updates = er.async_get(self.hass).async_get_entity_id("update", DOMAIN, TARGET)
        self.assertIsNotNone(updates)
        self.assertEqual(self.hass.states.get(updates).state, "off")
        self.assertEqual(self.hass.states.get(updates).attributes["installed_version"], FIRST.version)

        self.latest.return_value = SECOND
        await coordinator.async_refresh()
        self.assertEqual(self.hass.states.get(updates).state, "on")
        self.assertEqual(self.hass.states.get(updates).attributes["latest_version"], SECOND.version)

        admin = await self.hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
        user = await self.hass.auth.async_create_user("User", group_ids=[GROUP_ID_USER])
        self.assertFalse(user.is_admin)
        for person, authorized in ((user, False), (admin, True)):
            operation = self.hass.services.async_call(
                "update", "install", {"entity_id": updates},
                blocking=True, context=Context(user_id=person.id),
            )
            if authorized:
                await operation
            else:
                with self.assertRaises(Unauthorized):
                    await operation

        self.archive.assert_awaited_with(SECOND.sha)
        self.assertEqual(self.hass.states.get(updates).state, "off")
        self.assertEqual((self.target / "blueprints/automation/example.yaml").read_text(), "1.1.0")
        self.assertIn((DOMAIN, f"restart_{TARGET}"), ir.async_get(self.hass).issues)

        self.latest.side_effect = ManagerError("cannot_connect")
        await coordinator.async_refresh()
        self.assertEqual(self.hass.states.get(updates).state, "unavailable")
        options = self.hass.config_entries.options
        result = await options.async_init(entry.entry_id)
        self.assertIn("rollback", result["menu_options"])
        result = await options.async_configure(result["flow_id"], {"next_step_id": "rollback"})
        self.assertEqual(result["description_placeholders"]["version"], FIRST.version)
        requests_before = self.archive.await_count
        result = await options.async_configure(result["flow_id"], {"confirm": True})
        self.assertEqual(result["reason"], "rolled_back")
        self.assertEqual(self.archive.await_count, requests_before)
        self.assertTrue(coordinator.data.installed.paused)
        self.assertEqual((self.target / "blueprints/automation/example.yaml").read_text(), "1.0.0")

        self.latest.side_effect = None
        result = await options.async_init(entry.entry_id)
        result = await options.async_configure(result["flow_id"], {"next_step_id": "resume"})
        self.assertEqual(result["reason"], "resumed")
        self.assertEqual(self.hass.states.get(updates).state, "on")

    async def test_release_notes_use_home_assistant_update_api(self):
        entry = await self._configure()
        updates = er.async_get(self.hass).async_get_entity_id("update", DOMAIN, TARGET)
        admin = await self.hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
        user = await self.hass.auth.async_create_user("User", group_ids=[GROUP_ID_USER])
        connection = Mock(user=admin)
        message = {"id": 1, "type": "update/release_notes", "entity_id": updates}
        notes = "## Private release\n\n- Keep the existing settings."
        self.latest.return_value = replace(SECOND, notes=notes)
        await entry.runtime_data.async_refresh()

        state = self.hass.states.get(updates)
        self.assertEqual(state.state, "on")
        self.assertIsNone(state.attributes["release_url"])
        self.assertTrue(state.attributes["supported_features"] & UpdateEntityFeature.RELEASE_NOTES)
        self.assertNotIn(notes, repr(state.attributes))
        requests = (self.repository_id.await_count, self.latest.await_count, self.archive.await_count)
        with self.assertRaises(Unauthorized):
            websocket_release_notes(self.hass, Mock(user=user), message)
        websocket_release_notes(self.hass, connection, message)
        await self.hass.async_block_till_done()
        connection.send_result.assert_called_once_with(1, notes)
        self.assertEqual(
            (self.repository_id.await_count, self.latest.await_count, self.archive.await_count),
            requests,
        )

        self.latest.return_value = SECOND
        await entry.runtime_data.async_refresh()
        state = self.hass.states.get(updates)
        self.assertEqual(state.state, "on")
        self.assertIsNone(state.attributes["release_url"])
        self.assertEqual(state.attributes["supported_features"], UpdateEntityFeature.INSTALL)

    async def test_confirmation_refuses_changed_repository_identity(self):
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "confirm", result)
        self.repository_id.return_value = REPOSITORY_ID + 1
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirm": True}
        )
        self.assertEqual(result["reason"], "repository_changed")
        self.assertFalse(self.target.exists())

    async def test_marketplace_takeover_preserves_setup_and_survives_restart(self):
        repository, files, target_entry, device, entity, old_update = (
            await self._marketplace_installation()
        )
        self.latest.return_value = SECOND
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "takeover", result)
        self.assertEqual(result["description_placeholders"]["version"], FIRST.version)
        self.assertTrue(repository.data.installed)
        self.assertIsNotNone(self.hass.states.get(old_update))
        market_device_id = er.async_get(self.hass).async_get(old_update).device_id
        devices = dr.async_get(self.hass)
        other_market_device = devices.async_get_or_create(
            config_entry_id=self.market_entry.entry_id,
            identifiers={("marketplace", str(REPOSITORY_ID + 1))},
            name="Another Marketplace repository",
        )
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirm": True}
        )
        managed = result["result"]
        await self.hass.async_block_till_done()
        self.assertIs(managed.state, ConfigEntryState.LOADED, managed.reason)
        self.assertEqual(self.archive.await_count, 1)
        self.assertNotIn((DOMAIN, f"restart_{TARGET}"), ir.async_get(self.hass).issues)
        self.assertIsNone(devices.async_get(market_device_id))
        self.assertEqual(devices.async_get(other_market_device.id), other_market_device)

        await self._stop_home_assistant(self.hass)
        await self._boot_home_assistant()
        current = self.hass.config_entries.async_get_entry(target_entry.entry_id)
        self.assertEqual(dict(current.data), dict(target_entry.data))
        self.assertEqual(dict(current.options), dict(target_entry.options))
        self.assertEqual(dr.async_get(self.hass).async_get(device.id).name, device.name)
        registry = er.async_get(self.hass)
        self.assertEqual(registry.async_get(entity.entity_id).id, entity.id)
        self.assertIsNone(registry.async_get(old_update))
        self.assertIsNone(self.hass.states.get(old_update))
        self.assertIsNone(dr.async_get(self.hass).async_get(market_device_id))
        self.assertIsNone(self.market.repositories.get_by_id(repository.data.id))
        self.assertNotIn(repository.data.id, self.market.common.custom_repositories)
        managed = self.hass.config_entries.async_get_entry(managed.entry_id)
        self.assertIs(managed.state, ConfigEntryState.LOADED, managed.reason)
        self.assertIsNone(managed.runtime_data.data.installed.sha)
        self.assertEqual(managed.runtime_data.data.candidate, SECOND)
        self.assertNotIn(CONF_MARKETPLACE_TAKEOVER, managed.data)
        for path, content in files.items():
            self.assertEqual((self.target / path).read_bytes(), content)

        update = registry.async_get_entity_id("update", DOMAIN, TARGET)
        await self.hass.services.async_call("update", "install", {"entity_id": update}, blocking=True)
        self.assertEqual((self.target / "blueprints/automation/example.yaml").read_text(), SECOND.version)
        self.assertEqual(managed.runtime_data.data.previous.version, FIRST.version)
        self.assertEqual(registry.async_get(entity.entity_id).id, entity.id)

    async def test_catalog_repository_stays_listed_after_takeover(self):
        repository, _, _, _, _, update = await self._marketplace_installation(catalog=True)
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "takeover", result)
        result = await self.hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirm": True}
        )
        self.assertIs(result["result"].state, ConfigEntryState.LOADED, result["result"].reason)
        self.assertIs(self.market.repositories.get_by_id(repository.data.id), repository)
        self.assertTrue(self.market.repositories.is_default(repository.data.id))
        self.assertFalse(repository.data.installed)
        self.assertIsNone(er.async_get(self.hass).async_get(update))

    async def test_marketplace_write_failure_restores_previous_ownership(self):
        repository, files, target_entry, device, entity, old_update = (
            await self._marketplace_installation()
        )
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "takeover", result)
        repository_store = get_storage_for_key(self.hass, "repositories")
        write = type(repository_store)._async_write_data
        fail = True

        async def fail_once(store, data):
            nonlocal fail
            if store is repository_store and fail:
                fail = False
                raise WriteError("Synthetic Marketplace storage failure")
            await write(store, data)

        with patch.object(type(repository_store), "_async_write_data", fail_once):
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"confirm": True}
            )
        managed = result["result"]
        self.assertIs(managed.state, ConfigEntryState.SETUP_RETRY, managed.reason)
        self.assertTrue(repository.data.installed)
        self.assertIs(self.market.repositories.get_by_id(repository.data.id), repository)
        saved = await async_load_from_storage(self.hass, "repositories")
        self.assertTrue(saved[repository.data.id]["installed"])
        self.assertIn(repository.data.id, self.market.common.custom_repositories)
        self.assertIsNotNone(er.async_get(self.hass).async_get(old_update))
        self.assertIsNotNone(er.async_get(self.hass).async_get(entity.entity_id))
        self.assertIsNotNone(dr.async_get(self.hass).async_get(device.id))
        self.assertIs(self.hass.config_entries.async_get_entry(target_entry.entry_id), target_entry)
        for path, content in files.items():
            self.assertEqual((self.target / path).read_bytes(), content)

        await self.hass.config_entries.async_reload(managed.entry_id)
        self.assertIs(managed.state, ConfigEntryState.LOADED, managed.reason)
        self.assertIsNone(er.async_get(self.hass).async_get(old_update))

    async def test_restart_completes_handover_interrupted_after_marketplace_save(self):
        repository, _, target_entry, device, entity, old_update = (
            await self._marketplace_installation()
        )
        result = await self._start_flow()
        self.assertEqual(result.get("step_id"), "takeover", result)
        with patch.object(
            takeover, "_remove_marketplace_update",
            side_effect=ManagerError("marketplace_save_failed"),
        ):
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"confirm": True}
            )
        managed = result["result"]
        self.assertIs(managed.state, ConfigEntryState.SETUP_RETRY)
        self.assertIn(CONF_MARKETPLACE_TAKEOVER, managed.data)
        self.assertIsNone(self.market.repositories.get_by_id(repository.data.id))
        self.assertIsNotNone(er.async_get(self.hass).async_get(old_update))

        await self._stop_home_assistant(self.hass)
        await self._boot_home_assistant()
        managed = self.hass.config_entries.async_get_entry(managed.entry_id)
        self.assertIs(managed.state, ConfigEntryState.LOADED, managed.reason)
        self.assertNotIn(CONF_MARKETPLACE_TAKEOVER, managed.data)
        self.assertIsNone(er.async_get(self.hass).async_get(old_update))
        self.assertIsNotNone(er.async_get(self.hass).async_get(entity.entity_id))
        self.assertIsNotNone(dr.async_get(self.hass).async_get(device.id))
        self.assertIsNotNone(self.hass.config_entries.async_get_entry(target_entry.entry_id))

    async def test_manual_token_rotation_reloads_same_entry(self):
        entry = await self._configure()
        flow = self.hass.config_entries.flow
        result = await flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        self.assertEqual(result.get("step_id"), "reconfigure", result)
        new_token = "github_pat_" + "b" * 40
        result = await flow.async_configure(result["flow_id"], {"token": new_token})
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.data["token"], new_token)
        self.assertEqual(self.archive.await_count, 2)

    async def test_automatic_settings_require_stable_polling(self):
        entry = await self._configure()
        self.latest.return_value = SECOND
        options = self.hass.config_entries.options
        result = await options.async_init(entry.entry_id)
        self.assertEqual(result.get("step_id"), "settings", result)
        result = await options.async_configure(
            result["flow_id"], {"interval": "0", "auto_install": True}
        )
        self.assertEqual(result["errors"]["base"], "automatic_requires_stable_polling")
        result = await options.async_configure(
            result["flow_id"], {"interval": "24", "auto_install": True}
        )
        self.assertEqual(result["type"], "create_entry")
        await self.hass.async_block_till_done()
        coordinator = entry.runtime_data
        self.assertEqual(coordinator.data.installed.sha, FIRST.sha)
        self.assertEqual(coordinator.data.candidate, SECOND)
        await coordinator.async_refresh()
        self.assertEqual(coordinator.data.installed.sha, SECOND.sha)
