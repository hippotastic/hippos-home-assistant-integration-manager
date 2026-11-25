# Hippo's Home Assistant Integration Manager

Allows installing Home Assistant integrations from private GitHub repositories using repository-scoped read-only credentials. Managed integrations show their updates in the standard Home Assistant update view.

## Install the manager

Requires Home Assistant 2026.11 or later. Open **Settings → Marketplace → Add from link**, add `hippotastic/hippos-home-assistant-integration-manager` as an integration, and install it. The Marketplace can update the manager from the same link.

## Add a managed integration

1. Create a [fine-grained GitHub token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) for the selected repository with **Contents: Read-only**. Give each Home Assistant installation its own token so you can revoke access independently.
2. In **Settings → Devices & services → Add integration**, choose **Hippo's Home Assistant Integration Manager** and enter the repository and token. Leave **Development branch** empty to use the default release mode.
3. Review the integration and version in the confirmation dialog. For a Marketplace installation, use [Take over from Marketplace](#take-over-an-existing-installation) to keep the current version.
4. If the manager installed new integration files, restart Home Assistant. Set up the installed integration normally if it is new.

### Version source

- **Stable releases (default).** For installation and update checks, the manager uses the latest stable release published on GitHub. It downloads the source at that release's tag. The repository needs a published stable release with a tag such as `v1.2.3` and a matching manifest version.
- **Development branch (opt-in).** Enter a branch such as `main` to track its latest commit. The update entity includes the commit in the displayed version, so new commits can be offered even when the manifest version stays the same.

The manager installs the selected `custom_components/<domain>/` tree. A repository with `__init__.py` and `manifest.json` at its root is also supported. Packages that need a newer HA version or a Marketplace `persistent_directory` are blocked before installation.

## Update and roll back

The manager checks daily by default. Use **Check for updates** on the config entry for an immediate check. Install an offered revision in Home Assistant's update view. HA shows a repair when you need to restart to load the new code.

Use the config entry's options to change the check interval, select a development branch, or enable automatic installation of stable releases. The first check after startup, options changes, or resuming updates only reports the available version.

To rotate a GitHub token, choose **Reconfigure** from the config entry menu. Home Assistant also offers reauthentication if GitHub rejects the existing token.

Once two managed versions have been installed, the options menu can restore the previous one from disk. A rollback pauses update checks until you resume them. The manager retains one previous installation. Removing the manager's config entry leaves the managed integration files and the retained copy on disk.

## Take over an existing installation

If the integration is installed through the Marketplace, choose **Take over from Marketplace** during setup. The manager keeps the installed version, existing Home Assistant settings, devices, and entity IDs. The takeover itself needs no restart. Future updates come from the GitHub repository you entered.

The manager replaces the Marketplace update entity with its own. A repository added by link disappears from the local Marketplace list. Catalog entries remain available in the catalog as uninstalled. To continue an existing automation for installing updates, select the manager's new update entity.

The confirmation dialog can also replace a manually installed integration with a GitHub revision for the same domain, preserving the integration's HA config entries.

## Credentials and trusted releases

The token stays in the Home Assistant config entry. Protect the configuration directory and its backups. The manager sends the token to `api.github.com` and uses GitHub's short-lived URL for the archive download. The archive is bounded and checked before any integration files are replaced.

Custom integrations run with Home Assistant's privileges. Only install releases from maintainers you trust. GitHub [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) can lock published tags to their commits. A fine-grained token limits GitHub access to the selected repository when you configure it with the permissions above.

See [CONTRIBUTING.md](CONTRIBUTING.md) for validation and the release workflow.
