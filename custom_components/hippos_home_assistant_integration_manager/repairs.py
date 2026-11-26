"""Restart Home Assistant from a pending Integration Manager repair."""

import probatio

from homeassistant.components.homeassistant import DOMAIN as HA_DOMAIN, SERVICE_HOMEASSISTANT_RESTART
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownStep
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, RESTART_ISSUE_PREFIX


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the confirmation for a pending restart."""
    if not issue_id.startswith(RESTART_ISSUE_PREFIX):
        raise UnknownStep("Unknown Integration Manager repair")
    return _RestartRepairFlow()


class _RestartRepairFlow(RepairsFlow):
    """Request a normal Core restart after explicit confirmation."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Open the confirmation form."""
        # Core can supply issue metadata when opening a repairs flow.
        return await self.async_step_confirm_restart()

    async def async_step_confirm_restart(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Restart only when the form is submitted."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        if issue is None or not issue.active:
            return self.async_abort(reason="issue_resolved")
        errors = {}
        if user_input is not None:
            try:
                await self.hass.services.async_call(
                    HA_DOMAIN, SERVICE_HOMEASSISTANT_RESTART, blocking=True
                )
            except HomeAssistantError:
                errors["base"] = "restart_failed"
            else:
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm_restart",
            data_schema=probatio.Schema({}),
            description_placeholders=issue.translation_placeholders,
            errors=errors,
        )
