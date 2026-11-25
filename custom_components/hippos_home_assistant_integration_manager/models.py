"""Data shared by the GitHub client and installer."""

from dataclasses import dataclass

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class ManagerError(HomeAssistantError):
    """An operation failed with a message safe to show in Home Assistant."""

    def __init__(self, key: str) -> None:
        """Keep remote data and credentials out of the error."""
        super().__init__(key, translation_domain=DOMAIN, translation_key=key)
        self.key = key


@dataclass(frozen=True, slots=True)
class Revision:
    """A branch or release resolved to the exact GitHub commit."""

    sha: str
    version: str
    url: str
    stable: bool


@dataclass(frozen=True, slots=True)
class Package:
    """The validated, installable files for one integration."""

    domain: str
    name: str
    version: str
    files: dict[str, bytes]


@dataclass(frozen=True, slots=True)
class Installation:
    """The provenance of the files currently installed on disk."""

    repository_id: int
    sha: str | None
    version: str
    label: str
    paused: bool = False
