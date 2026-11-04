#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

metainfo="com.google.Antigravity.metainfo.xml"
manifest="com.google.Antigravity.yaml"

versions="$(sed -n 's#.*/antigravity-hub/\([0-9][0-9.]*\)-[0-9]*/linux-x64/Antigravity\.deb.*#\1#p' "$manifest")"
version="$(printf '%s\n' "$versions" | head -n 1)"

if [ -z "$version" ] || [ "$(printf '%s\n' "$versions" | grep -c .)" -ne 1 ]; then
  echo "Error: expected exactly one x64 Antigravity .deb URL in $manifest" >&2
  exit 1
fi

date="$(date +%Y-%m-%d)"

if grep -q "version=\"$version\"" "$metainfo"; then
  sed -i "s/<release version=\"$version\" date=\"[^\"]*\"/<release version=\"$version\" date=\"$date\"/" "$metainfo"
else
  sed -i "/<releases>/a\\    <release version=\"$version\" date=\"$date\"/>" "$metainfo"
fi

echo "Metainfo updated for Antigravity $version"
