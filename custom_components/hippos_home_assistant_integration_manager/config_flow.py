"""Configure repository access and confirm the component to install."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .archive import read_packages
from .const import (
    CONF_AUTO_INSTALL,
    CONF_BRANCH,
    CONF_INTEGRATION,
    CONF_INTERVAL,
    CONF_MARKETPLACE_TAKEOVER,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DEFAULT_INTERVAL,
    DOMAIN,
    INTERVALS,
)
from .coordinator import ManagerEntry, UpdateData
from .github import GitHub, normalize_branch
from .ha import async_change_installation, ensure_marketplace_released, marketplace_owner
from .installer import Installer
from .models import Installation, ManagerError, Package, Revision

_CONFIRM = "confirm"
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class IntegrationManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Select a repository and authorize a single integration directory."""

    VERSION = 1
    _github: GitHub
    _token: str
    _repository_id: int
    _revision: Revision
    _branch: str
    _integrations: dict[str, tuple[str, str]]
    _domain: str
    _installation_preview: tuple[bool, Installation | None]
    _takeover_preview: dict[str, str]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Manage the update policy and locally retained version."""
        return IntegrationManagerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Check access and discover integrations at a pinned commit."""
        errors = {}
        if user_input is not None:
            try:
                self._token = user_input[CONF_TOKEN].strip()
                self._branch = normalize_branch(user_input.get(CONF_BRANCH, ""))
                self._github = GitHub(
                    async_get_clientsession(self.hass), user_input[CONF_REPOSITORY], self._token
                )
                self._repository_id = await self._github.repository_id()
                self._revision = await self._github.latest(self._branch)
                packages = await self._async_packages()
                self._integrations = {
                    domain: (package.name, package.version)
                    for domain, package in packages.items()
                    if domain != DOMAIN
                }
                if not self._integrations:
                    raise ManagerError("protected_domain")
            except ManagerError as err:
                errors["base"] = err.key
            else:
                if len(self._integrations) == 1:
                    return await self.async_step_select(
                        {CONF_INTEGRATION: next(iter(self._integrations))}
                    )
                return await self.async_step_select()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REPOSITORY): str,
                    vol.Required(CONF_TOKEN): _PASSWORD,
                    vol.Optional(CONF_BRANCH): str,
                }
            ),
            errors=errors,
        )

    async def async_step_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the integration when the repository has several."""
        if user_input is not None:
            self._domain = user_input[CONF_INTEGRATION]
            if self._domain not in self._integrations:
                return self.async_abort(reason="integration_missing")
            await self.async_set_unique_id(self._domain)
            self._abort_if_unique_id_configured()
            return await self.async_step_confirm()

        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INTEGRATION): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(label=f"{name} ({domain})", value=domain)
                                for domain, (name, _) in self._integrations.items()
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the exact version, including explicit replacement of existing files."""
        try:
            if user_input is None:
                if marketplace_owner(self.hass, self._domain) is not None:
                    return await self.async_step_takeover()
            else:
                ensure_marketplace_released(self.hass, self._domain)
            installer = Installer(self.hass.config.config_dir, self._domain, self._repository_id)
            exists, current, _ = await self.hass.async_add_executor_job(installer.inspect)
            if current is not None and current.repository_id != self._repository_id:
                raise ManagerError("installation_owned")
        except ManagerError as err:
            return self.async_abort(reason=err.key)

        name, version = self._integrations[self._domain]
        if user_input is not None and user_input[_CONFIRM]:
            try:
                if (exists, current) != self._installation_preview:
                    raise ManagerError("installation_changed")
                if await self._github.repository_id() != self._repository_id:
                    raise ManagerError("repository_changed")
                package = (await self._async_packages(self._domain))[self._domain]
                await async_change_installation(
                    self.hass, self._domain,
                    lambda: installer.install(
                        package, self._revision, adopt=exists and current is None, expected=current
                    ),
                )
            except ManagerError as err:
                return self.async_abort(reason=err.key)
            return self._create_installation_entry()

        self._installation_preview = (exists, current)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required(_CONFIRM, default=False): bool}),
            description_placeholders={
                "repository": self._github.repository,
                "integration": name,
                "domain": self._domain,
                "version": version,
                "commit": self._revision.sha[:12],
                "existing": f"custom_components/{self._domain}" if exists else "—",
            },
        )

    async def async_step_takeover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before taking over an installed Marketplace component in place."""
        try:
            owner = marketplace_owner(self.hass, self._domain)
            if owner is None:
                raise ManagerError("installation_changed")
            if owner.installing:
                raise ManagerError("marketplace_busy")
            installer = Installer(self.hass.config.config_dir, self._domain, self._repository_id)
            version = await self.hass.async_add_executor_job(installer.takeover_version)
            consent = {"id": owner.data.id, "version": version}
            if user_input is not None and user_input[_CONFIRM]:
                if consent != self._takeover_preview:
                    raise ManagerError("installation_changed")
                if await self._github.repository_id() != self._repository_id:
                    raise ManagerError("repository_changed")
                return self._create_installation_entry(consent)
        except ManagerError as err:
            return self.async_abort(reason=err.key)

        self._takeover_preview = consent
        return self.async_show_form(
            step_id="takeover",
            data_schema=vol.Schema({vol.Required(_CONFIRM, default=False): bool}),
            description_placeholders={
                "integration": self._integrations[self._domain][0],
                "version": version,
                "marketplace_repository": owner.data.full_name,
                "repository": self._github.repository,
            },
        )

    @callback
    def _create_installation_entry(
        self, takeover: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {
            CONF_REPOSITORY: self._github.repository,
            CONF_TOKEN: self._token,
            CONF_REPOSITORY_ID: self._repository_id,
            CONF_INTEGRATION: self._domain,
        }
        if takeover is not None:
            data[CONF_MARKETPLACE_TAKEOVER] = takeover
        return self.async_create_entry(
            title=self._integrations[self._domain][0],
            data=data,
            options={CONF_BRANCH: self._branch},
        )

    async def _async_packages(self, domain: str | None = None) -> dict[str, Package]:
        contents = await self._github.archive(self._revision.sha)
        return await self.hass.async_add_executor_job(read_packages, contents, domain)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Replace an expired or revoked GitHub token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Repair access without changing the repository or channel."""
        return await self._async_token_form(self._get_reauth_entry(), "reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rotate a token before GitHub expires it."""
        return await self._async_token_form(
            self._get_reconfigure_entry(), "reconfigure", user_input
        )

    async def _async_token_form(
        self, entry: ConfigEntry, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                token = user_input[CONF_TOKEN].strip()
                github = GitHub(async_get_clientsession(self.hass), entry.data[CONF_REPOSITORY], token)
                if await github.repository_id() != entry.data[CONF_REPOSITORY_ID]:
                    raise ManagerError("repository_changed")
                await github.latest(entry.options.get(CONF_BRANCH, ""))
            except ManagerError as err:
                errors["base"] = err.key
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})

        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): _PASSWORD}),
            description_placeholders={"repository": entry.data[CONF_REPOSITORY]},
            errors=errors,
        )


class IntegrationManagerOptionsFlow(OptionsFlowWithReload):
    """Configure polling and expose rollback and resume to administrators."""

    config_entry: ManagerEntry
    _rollback_preview: UpdateData

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer the operations currently available for this installation."""
        if self.config_entry.state is ConfigEntryState.LOADED:
            data = self.config_entry.runtime_data.data
            options = ["settings"]
            if data.previous is not None and data.previous.sha != data.installed.sha:
                options.append("rollback")
            if data.installed.paused:
                options.append("resume")
            if len(options) > 1:
                return self.async_show_menu(step_id="init", menu_options=options)
        return await self.async_step_settings()

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set the channel, interval, and opt in to automatic stable updates."""
        errors = {}
        if user_input is not None:
            try:
                branch = normalize_branch(user_input.get(CONF_BRANCH, ""))
                hours = int(user_input[CONF_INTERVAL])
                automatic = user_input[CONF_AUTO_INSTALL]
                if automatic and (branch or hours == 0):
                    raise ManagerError("automatic_requires_stable_polling")
                if branch != self.config_entry.options.get(CONF_BRANCH, ""):
                    data = self.config_entry.data
                    github = GitHub(
                        async_get_clientsession(self.hass), data[CONF_REPOSITORY], data[CONF_TOKEN]
                    )
                    if await github.repository_id() != data[CONF_REPOSITORY_ID]:
                        raise ManagerError("repository_changed")
                    await github.latest(branch)
            except ManagerError as err:
                errors["base"] = err.key
            else:
                return self.async_create_entry(
                    data={CONF_BRANCH: branch, CONF_INTERVAL: hours, CONF_AUTO_INSTALL: automatic}
                )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_BRANCH, default=options.get(CONF_BRANCH, "")): str,
                    vol.Required(
                        CONF_INTERVAL, default=str(options.get(CONF_INTERVAL, DEFAULT_INTERVAL))
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[str(interval) for interval in INTERVALS],
                            translation_key="check_interval",
                        )
                    ),
                    vol.Required(
                        CONF_AUTO_INSTALL, default=options.get(CONF_AUTO_INSTALL, False)
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_rollback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm a rollback to the local previous version."""
        coordinator = self.config_entry.runtime_data
        data = coordinator.data
        if data.previous is None:
            return self.async_abort(reason="rollback_unavailable")
        if user_input is not None and user_input[_CONFIRM]:
            try:
                await coordinator.async_rollback(self._rollback_preview)
            except ManagerError as err:
                return self.async_abort(reason=err.key)
            return self.async_abort(reason="rolled_back")

        self._rollback_preview = data
        return self.async_show_form(
            step_id="rollback",
            data_schema=vol.Schema({vol.Required(_CONFIRM, default=False): bool}),
            description_placeholders={"version": data.previous.label},
        )

    async def async_step_resume(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Resume checks after a rollback."""
        coordinator = self.config_entry.runtime_data
        try:
            await coordinator.async_resume(coordinator.data.installed)
        except ManagerError as err:
            return self.async_abort(reason=err.key)
        return self.async_abort(reason="resumed")
