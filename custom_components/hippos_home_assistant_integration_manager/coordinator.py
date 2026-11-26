"""Coordinate release checks and installation for one managed component."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging

from packaging.version import Version

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .archive import read_packages
from .const import (
    CONF_AUTO_INSTALL,
    CONF_BRANCH,
    CONF_INTEGRATION,
    CONF_INTERVAL,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DEFAULT_INTERVAL,
    DOMAIN,
)
from .github import GitHub
from .ha import async_change_installation
from .installer import Installer
from .models import Installation, ManagerError, Revision

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpdateData:
    """Current files, previous version, and the candidate offered in HA."""

    installed: Installation
    previous: Installation | None
    candidate: Revision | None


type ManagerEntry = ConfigEntry[ManagerCoordinator]


class ManagerCoordinator(DataUpdateCoordinator[UpdateData]):
    """Keep remote checks and file changes serialized for one integration."""

    def __init__(self, hass: HomeAssistant, entry: ManagerEntry) -> None:
        """Configure polling and bind the runtime to the selected repository."""
        hours = entry.options.get(CONF_INTERVAL, DEFAULT_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=hours) if hours else None,
            always_update=False,
        )
        self.domain = entry.data[CONF_INTEGRATION]
        self.installer = Installer(
            hass.config.config_dir, self.domain, entry.data[CONF_REPOSITORY_ID]
        )
        self.github = GitHub(
            async_get_clientsession(hass), entry.data[CONF_REPOSITORY], entry.data[CONF_TOKEN]
        )
        self._branch = entry.options.get(CONF_BRANCH, "")
        self._automatic = bool(hours and not self._branch and entry.options.get(CONF_AUTO_INSTALL))
        self._lock = asyncio.Lock()
        # An initial check reports the current candidate without writing files.
        self._initial_check = True

    async def async_read_installation(self) -> UpdateData:
        """Load local state independently of GitHub availability."""
        _, installed, previous = await self.hass.async_add_executor_job(self.installer.inspect)
        if installed is None or installed.repository_id != self.installer.repository_id:
            raise ManagerError("installation_changed")
        return UpdateData(installed, previous, None)

    async def _async_update_data(self) -> UpdateData:
        async with self._lock:
            try:
                data = await self.async_read_installation()
                installed, previous = data.installed, data.previous
                candidate = None
                if not installed.paused:
                    if await self.github.repository_id() != installed.repository_id:
                        raise ManagerError("repository_changed")
                    revision = await self.github.latest(self._branch)
                    if revision.sha != installed.sha and (
                        not revision.stable or Version(revision.version) > Version(installed.version)
                    ):
                        candidate = revision

                if candidate is not None and self._automatic and not self._initial_check:
                    old = installed
                    installed = await self._async_apply(candidate, installed)
                    previous = old
                    candidate = None
                self._initial_check = False
                return UpdateData(installed, previous, candidate)
            except ManagerError as err:
                if err.key in {"invalid_token", "repository_inaccessible"}:
                    raise ConfigEntryAuthFailed(err.key) from None
                raise UpdateFailed(err.key) from None

    async def async_install(self) -> None:
        """Install the exact revision last offered by this update entity."""
        async with self._lock:
            data = self.data
            if not self.last_update_success or data.candidate is None or data.installed.paused:
                raise ManagerError("no_update")
            if await self.github.repository_id() != data.installed.repository_id:
                raise ManagerError("repository_changed")
            installed = await self._async_apply(data.candidate, data.installed)
            self.async_set_updated_data(UpdateData(installed, data.installed, None))

    async def _async_apply(self, revision: Revision, installed: Installation) -> Installation:
        contents = await self.github.archive(revision.sha)
        packages = await self.hass.async_add_executor_job(read_packages, contents, self.domain)
        package = packages[self.domain]
        # The installer rechecks the durable marker under its file lock.
        return await async_change_installation(
            self.hass, self.domain,
            lambda: self.installer.install(package, revision, expected=installed),
            integration_name=package.name,
        )

    async def async_rollback(self, preview: UpdateData) -> None:
        """Restore the previous managed version through the options flow."""
        async with self._lock:
            if (
                self.data.installed != preview.installed or self.data.previous != preview.previous
            ):
                raise ManagerError("installation_changed")
            assert self.config_entry is not None
            installed = await async_change_installation(
                self.hass, self.domain,
                lambda: self.installer.rollback(preview.installed, preview.previous),
                integration_name=self.config_entry.title,
            )
            self.async_set_updated_data(UpdateData(installed, preview.installed, None))

    async def async_resume(self, expected: Installation) -> None:
        """Clear the rollback pause and refresh without automatically installing."""
        async with self._lock:
            await self.hass.async_add_executor_job(self.installer.resume, expected)
            self._initial_check = True
        await self.async_refresh()
