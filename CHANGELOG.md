# hippos-home-assistant-integration-manager

## 0.1.8

### Patch Changes

- 3632bff: Add a Restart button to repairs that require a Home Assistant restart. Replace an older restart notice when a different version has been installed so the displayed time reflects that installation.

## 0.1.7

### Patch Changes

- 8151db8: Show the integration's display name in Home Assistant restart notices.

## 0.1.6

### Patch Changes

- 6f0d86b: Show the replacement warning for existing integration files and confirm installation or migration directly with the action button.
- 6f0d86b: Choose a GitHub token from another Manager entry or enter a new one during setup and token replacement.

## 0.1.5

### Patch Changes

- 0789db4: Make newly installed integrations with a setup dialog available immediately after installation. Show a restart repair when Home Assistant cannot load the new integration in the current session.

## 0.1.4

### Patch Changes

- 73804bb: Show release notes and development commit messages directly in the Home Assistant update dialog.

## 0.1.3

### Patch Changes

- cf5148c: Shorten the display name in Home Assistant and the Marketplace to Hippo's Integration Manager.

## 0.1.2

### Patch Changes

- 1c599fc: Use the current Home Assistant device removal API when taking over a Marketplace integration.

## 0.1.1

### Patch Changes

- Add an integration icon based on the Toolbox mascot.

## 0.1.0

- Install custom integrations from GitHub and offer updates through Home Assistant.
- Take over Marketplace installations while keeping their installed version and Home Assistant setup.
- Support stable releases, development branches, and local rollback.
