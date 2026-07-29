#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

compose down --volumes --remove-orphans

for target in "$ORGANIZATIONS_DIR" "$ARTIFACTS_DIR"; do
  case "$target" in
    "$NETWORK_DIR"/organizations|"$NETWORK_DIR"/artifacts)
      if [ -e "$target" ]; then
        rm -rf -- "$target"
      fi
      ;;
    *)
      echo "Refusing unsafe cleanup target: $target" >&2
      exit 1
      ;;
  esac
done

echo "Removed the isolated Fabric containers, volumes, and generated artifacts."
