"""Provide an on-demand integration update check."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ManagerCoordinator, ManagerEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ManagerEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the refresh button for a managed integration."""
    async_add_entities([CheckUpdates(entry.runtime_data)])


class CheckUpdates(CoordinatorEntity[ManagerCoordinator], ButtonEntity):
    """Refresh remote metadata on demand."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "check_updates"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: ManagerCoordinator) -> None:
        """Set the entity identity independently of the repository name."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.domain}_check_updates"
        self._attr_suggested_object_id = f"{coordinator.domain}_check_updates"

    @property
    def available(self) -> bool:
        """Allow retrying a previously failed check."""
        return True

    async def async_press(self) -> None:
        """Request a fresh check."""
        await self.coordinator.async_request_refresh()
