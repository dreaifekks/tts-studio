#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
tag="$(git describe --tags --match 'v[0-9]*.[0-9]*.[0-9]*' --abbrev=0 2>/dev/null || true)"
if [[ -z "$tag" ]]; then
  echo 0.0.0
elif [[ "$(git rev-parse HEAD)" == "$(git rev-list -n 1 "$tag")" ]]; then
  echo "${tag#v}"
else
  echo "${tag#v}-dev+$(git rev-parse --short HEAD)"
fi
