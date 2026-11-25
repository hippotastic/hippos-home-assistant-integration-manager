"""Hippo's Home Assistant Integration Manager."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import CONF_MARKETPLACE_TAKEOVER, DOMAIN
from .coordinator import ManagerCoordinator, ManagerEntry
from .models import ManagerError
from .takeover import async_take_over_marketplace

_PLATFORMS = (Platform.BUTTON, Platform.UPDATE)


async def async_setup_entry(hass: HomeAssistant, entry: ManagerEntry) -> bool:
    """Start tracking an integration installed by the config flow."""
    try:
        coordinator = ManagerCoordinator(hass, entry)
        if CONF_MARKETPLACE_TAKEOVER in entry.data:
            await async_take_over_marketplace(hass, entry, coordinator.installer)
        coordinator.data = await coordinator.async_read_installation()
    except ManagerError as err:
        if err.key in {"marketplace_busy", "marketplace_save_failed", "marketplace_unavailable"}:
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=err.key) from None
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key=err.key) from None
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_create_task(hass, coordinator.async_refresh(), "Check for integration updates")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ManagerEntry) -> bool:
    """Stop managing the integration while leaving its installation in place."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
