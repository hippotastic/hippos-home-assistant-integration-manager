# Contributing

Use Node.js 24, pnpm, and Docker. Run `pnpm install --frozen-lockfile` to install the release tooling.

Run `pnpm validate` before committing. It checks the Marketplace layout, manifest version, and translation keys, runs the official hassfest validator, then runs the Python tests inside the minimum supported Home Assistant Docker image. Both containers use an isolated network and read-only working-tree mounts. The tests use synthetic archives and credentials. `pnpm validate:layout`, `pnpm validate:hassfest`, and `pnpm test:ha` can be run individually. Set `HA_IMAGE` to test another Home Assistant version.

The **Validate** workflow runs the same pipeline on GitHub. It also runs the official HACS action once the repository is public. The action needs live GitHub repository metadata, so the local layout check is the pre-publication check.

Use `pnpm changeset` for user-visible changes. The release workflow creates or updates a Changesets release pull request. The release step synchronizes the version in `package.json` and the integration manifest, then publishes the matching GitHub release once validation passes. Review the exact released commit and test it on a disposable HA installation before deploying it to a live instance.
