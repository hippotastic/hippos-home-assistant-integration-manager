#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
case "${1:-}" in
    hassfest)
        exec docker run --rm --platform linux/amd64 --pull "${HASSFEST_PULL_POLICY:-missing}" \
            --network none --volume "$root:/github/workspace:ro" --workdir /github/workspace \
            --env PYTHONDONTWRITEBYTECODE=1 ghcr.io/home-assistant/hassfest:latest
        ;;
    test)
        minimum=$(node -p 'require(process.argv[1]).homeassistant' "$root/hacs.json")
        exec docker run --rm --pull "${HA_IMAGE_PULL_POLICY:-missing}" --network none \
            --volume "$root:/workspace:ro" --workdir /workspace --env PYTHONDONTWRITEBYTECODE=1 \
            --entrypoint python3 "${HA_IMAGE:-ghcr.io/home-assistant/home-assistant:$minimum}" \
            -m unittest discover -s test -v
        ;;
    *)
        echo 'Usage: sh tools/validate-in-container.sh hassfest|test' >&2
        exit 2
        ;;
esac
