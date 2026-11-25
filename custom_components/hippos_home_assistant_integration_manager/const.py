"""Constants for Hippo's Home Assistant Integration Manager."""

import re

DOMAIN = "hippos_home_assistant_integration_manager"

CONF_REPOSITORY = "repository"
CONF_REPOSITORY_ID = "repository_id"
CONF_INTEGRATION = "integration"
CONF_BRANCH = "branch"
CONF_AUTO_INSTALL = "auto_install"
CONF_INTERVAL = "interval"
CONF_MARKETPLACE_TAKEOVER = "marketplace_takeover"

DEFAULT_INTERVAL = 24
INTERVALS = (0, 1, 6, 12, 24, 168)

DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
SHA_PATTERN = re.compile(r"[a-f0-9]{40}", re.ASCII)
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", re.ASCII)

MARKER = ".hippos-integration-manager.json"
