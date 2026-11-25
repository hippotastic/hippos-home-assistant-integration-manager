"""Expose updates through the native Home Assistant update platform."""

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_AUTO_INSTALL
from .coordinator import ManagerCoordinator, ManagerEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ManagerEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the update entity for the managed integration."""
    async_add_entities([IntegrationUpdate(entry.runtime_data, entry.title)])


class IntegrationUpdate(CoordinatorEntity[ManagerCoordinator], UpdateEntity):
    """Offer releases or branch revisions already checked by the coordinator."""

    def __init__(self, coordinator: ManagerCoordinator, title: str) -> None:
        """Use the domain as the stable identity across repository revisions."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.domain
        self._attr_name = title
        self._attr_title = title
        self._attr_suggested_object_id = f"{coordinator.domain}_managed"

    @property
    def installed_version(self) -> str:
        """Return the release or branch label installed on disk."""
        return self.coordinator.data.installed.label

    @property
    def latest_version(self) -> str:
        """Return the candidate label or the installed version if caught up."""
        candidate = self.coordinator.data.candidate
        return candidate.version if candidate is not None else self.installed_version

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Offer inline notes when the candidate includes them."""
        candidate = self.coordinator.data.candidate
        features = UpdateEntityFeature.INSTALL
        if candidate is not None and candidate.notes is not None:
            features |= UpdateEntityFeature.RELEASE_NOTES
        return features

    async def async_release_notes(self) -> str | None:
        """Return notes for the offered revision through Home Assistant's update API."""
        candidate = self.coordinator.data.candidate
        return candidate.notes if candidate is not None else None

    @property
    def auto_update(self) -> bool:
        """Report whether the coordinator will install stable releases automatically."""
        entry = self.coordinator.config_entry
        assert entry is not None
        return entry.options.get(CONF_AUTO_INSTALL, False) and not self.coordinator.data.installed.paused

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """The coordinator already checked branch identity or release ordering."""
        return self.coordinator.data.candidate is not None

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        """Install the displayed revision using the HA admin update action."""
        await self.coordinator.async_install()
