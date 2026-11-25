"""Transfer Marketplace ownership without uninstalling a custom integration."""

import asyncio
from contextlib import nullcontext
import logging

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE
from homeassistant.components.marketplace.repositories.integration import IntegrationRepository
from homeassistant.components.marketplace.utils.storage import async_load_from_storage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from .const import CONF_MARKETPLACE_TAKEOVER
from .ha import marketplace_owner, marketplace_runtime
from .installer import Installer
from .models import ManagerError

_LOGGER = logging.getLogger(__name__)


async def async_take_over_marketplace(
    hass: HomeAssistant, entry: ConfigEntry, installer: Installer
) -> None:
    """Commit the confirmed handoff and finish an interrupted handoff on retry."""
    consent = entry.data[CONF_MARKETPLACE_TAKEOVER]
    repository_id = consent["id"]
    marketplace = marketplace_runtime(hass)
    if marketplace is None or hass.state is CoreState.stopping:
        raise ManagerError("marketplace_unavailable")
    owner = marketplace_owner(hass, installer.domain)
    if owner is not None and owner.data.id != repository_id:
        raise ManagerError("installation_changed")
    if owner is not None and owner.installing:
        raise ManagerError("marketplace_busy")

    # Core's lock order is the repository lock followed by the shared file lock.
    async with owner._install_lock if owner is not None else nullcontext():
        async with marketplace.filesystem_lock:
            if marketplace_owner(hass, installer.domain) is not owner:
                raise ManagerError("installation_changed")
            if owner is not None:
                await _async_save_consent(hass, entry, consent)
            await hass.async_add_executor_job(
                lambda: installer.claim_marketplace(consent["version"], detached=owner is None)
            )
            if owner is not None:
                await _async_detach(marketplace, owner)
            _remove_marketplace_update(hass, marketplace, repository_id)

    # Keep the consent in HA until the files and Marketplace registration
    # have committed. Repeating the setup after that point only finishes cleanup.
    data = dict(entry.data)
    del data[CONF_MARKETPLACE_TAKEOVER]
    hass.config_entries.async_update_entry(entry, data=data)


async def _async_save_consent(
    hass: HomeAssistant, entry: ConfigEntry, consent: dict[str, str]
) -> None:
    # HA normally schedules the first save after async_setup_entry returns.
    # Save the pending entry through Core's Store before dropping the old owner.
    entries = hass.config_entries
    try:
        store = entries._store
        await store.async_save(entries._data_to_save())
        saved = await store.async_load()
    except (HomeAssistantError, OSError, AttributeError):
        raise ManagerError("marketplace_save_failed") from None
    if not saved or not any(
        record["entry_id"] == entry.entry_id
        and record["data"].get(CONF_MARKETPLACE_TAKEOVER) == consent
        for record in saved["entries"]
    ):
        raise ManagerError("marketplace_save_failed")


async def _async_detach(marketplace: MarketplaceManager, owner: IntegrationRepository) -> None:
    repository_id = owner.data.id
    is_default = marketplace.repositories.is_default(repository_id)
    was_custom = repository_id in marketplace.common.custom_repositories
    version, commit = owner.data.installed_version, owner.data.installed_commit
    owner.data.installed = False
    owner.data.installed_version = None
    owner.data.installed_commit = None
    if not is_default:
        owner.remove()
    marketplace.common.custom_repositories.discard(repository_id)

    try:
        await marketplace.data.async_write()
        # Home Assistant's Store also reports some write failures only in its log.
        common = await async_load_from_storage(marketplace.hass, "common")
        repos = await async_load_from_storage(marketplace.hass, "repositories")
        if (
            repository_id in common.get("custom_repositories", [])
            or repos.get(repository_id, {}).get("installed", False)
            or (not is_default and repository_id in repos)
        ):
            raise ManagerError("marketplace_save_failed")
    except (HomeAssistantError, OSError, asyncio.CancelledError) as err:
        owner.data.installed = True
        owner.data.installed_version = version
        owner.data.installed_commit = commit
        if not is_default:
            marketplace.repositories.register(owner)
        if was_custom:
            marketplace.common.custom_repositories.add(repository_id)
        try:
            await marketplace.data.async_write()
        except (HomeAssistantError, OSError):
            _LOGGER.error("Could not save the restored Marketplace registration")
        if isinstance(err, asyncio.CancelledError):
            raise
        raise ManagerError("marketplace_save_failed") from None


@callback
def _remove_marketplace_update(
    hass: HomeAssistant, marketplace: MarketplaceManager, repository_id: str
) -> None:
    market_entry = marketplace.configuration.config_entry.entry_id
    entities = er.async_get(hass)
    entity_id = entities.async_get_entity_id("update", MARKETPLACE, repository_id)
    if entity_id is not None:
        entity = entities.async_get(entity_id)
        if entity is not None and entity.config_entry_id == market_entry:
            entities.async_remove(entity_id)

    devices = dr.async_get(hass)
    # Device identifiers are unique within the owning config entry.
    for device in dr.async_entries_for_config_entry(devices, market_entry):
        if (MARKETPLACE, repository_id) in device.identifiers:
            devices.async_remove_device(device.id)
            break
    ir.async_delete_issue(hass, MARKETPLACE, f"removed_{repository_id}")
