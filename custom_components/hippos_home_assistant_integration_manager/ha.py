"""Home Assistant ownership and restart handling."""

import asyncio
from collections.abc import Callable
from contextlib import nullcontext

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.repositories import RepositoryCategory
from homeassistant.components.marketplace.repositories.integration import IntegrationRepository
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import Installation, ManagerError


@callback
def marketplace_runtime(hass: HomeAssistant) -> MarketplaceManager | None:
    """Return the loaded Marketplace runtime when this instance has one."""
    for entry in hass.config_entries.async_entries("marketplace"):
        if entry.state is not ConfigEntryState.LOADED:
            raise ManagerError("marketplace_unavailable")
        manager = getattr(entry, "runtime_data", None)
        if not isinstance(manager, MarketplaceManager):
            raise ManagerError("marketplace_unavailable")
        return manager
    return None


@callback
def ensure_marketplace_released(hass: HomeAssistant, domain: str) -> None:
    """Avoid taking over a domain the Marketplace still owns."""
    if marketplace_owner(hass, domain) is not None:
        raise ManagerError("marketplace_owned")


@callback
def marketplace_owner(hass: HomeAssistant, domain: str) -> IntegrationRepository | None:
    """Find the local Marketplace owner of a custom integration domain."""
    marketplace = marketplace_runtime(hass)
    if marketplace is None:
        return None
    owners = [
        repository
        for repository in marketplace.repositories.list_installed
        if repository.data.category == RepositoryCategory.INTEGRATION
        and repository.data.domain == domain
    ]
    if len(owners) > 1:
        raise ManagerError("marketplace_ambiguous")
    if not owners:
        return None
    owner = owners[0]
    if not isinstance(owner, IntegrationRepository):
        raise ManagerError("marketplace_unavailable")
    return owner


async def async_change_installation(
    hass: HomeAssistant, domain: str, operation: Callable[[], Installation]
) -> Installation:
    """Serialize the disk exchange with the Marketplace and report a restart."""
    marketplace = marketplace_runtime(hass)
    # Core 2026.11 uses this lock for all Marketplace installation writes.
    async with marketplace.filesystem_lock if marketplace is not None else nullcontext():
        ensure_marketplace_released(hass, domain)
        job = hass.async_add_executor_job(operation)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # Keep the Marketplace lock until the executor has stopped writing.
            await job
            raise
        finally:
            if job.done() and not job.cancelled() and job.exception() is None:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    f"restart_{domain}",
                    is_fixable=False,
                    issue_domain=domain,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="restart_required",
                    translation_placeholders={"integration": domain},
                )
