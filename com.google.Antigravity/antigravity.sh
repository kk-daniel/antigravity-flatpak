#!/usr/bin/env bash
set -euo pipefail
# The extension scans below iterate over mount points that are empty unless the
# user installed something; without nullglob the loops would run once on a
# literal "*". No other glob in this script relies on the default behaviour.
shopt -s nullglob

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/antigravity-flatpak"
mkdir -p "$log_dir"
log_file="$log_dir/launcher.log"

log() {
  printf '%s\n' "$*" >> "$log_file"
}

# Extension diagnostics go to stderr rather than the launcher log, because a
# misspelled extension name has to be visible to whoever typed it -- the log is
# for the post-mortem, not for the one line that says why nothing happened.
msg() {
  printf '%s\n' "$*" >&2
}

# ~/.gemini is the persisted directory (--persist=.gemini). Creating it up
# front means nothing the app or its language server writes there races a
# missing parent.
ensure_gemini_dir() {
  if [ ! -d "$HOME/.gemini" ]; then
    mkdir -p "$HOME/.gemini"
    log "Created $HOME/.gemini"
  fi
}

# Tool extensions land at /app/tools/<name> via the com.visualstudio.code.tool
# extension point declared in the manifest, so the Flathub extensions built for
# VS Code work here unmodified:
#   flatpak install flathub com.visualstudio.code.tool.podman//25.08
# Nothing gates this -- as in VS Code's launcher, whatever is mounted is wired up.
enable_tool_extensions() {
  local python_sitedir tool_dir tool_bindir tool_pythondir
  python_sitedir="$(python3 - <<'PYTHON'
import os
import site

print(os.path.relpath(site.getusersitepackages(), site.getuserbase()))
PYTHON
)"

  for tool_dir in /app/tools/*; do
    tool_bindir="$tool_dir/bin"
    if [ -d "$tool_bindir" ]; then
      export PATH="$PATH:$tool_bindir"
      log "Added $tool_bindir to PATH"
    fi

    tool_pythondir="$tool_dir/$python_sitedir"
    if [ -d "$tool_pythondir" ]; then
      if [ -z "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="$tool_pythondir"
      else
        export PYTHONPATH="$PYTHONPATH:$tool_pythondir"
      fi
      log "Added $tool_pythondir to PYTHONPATH"
    fi
  done
}

# Opt-in SDK extensions. Nothing is declared in the manifest for these: the
# runtime is org.freedesktop.Sdk, whose own extension point already mounts any
# installed org.freedesktop.Sdk.Extension.* at /usr/lib/sdk/<name>.
# FLATPAK_ENABLE_SDK_EXT takes a comma-separated list of short names -- golang,
# not org.freedesktop.Sdk.Extension.golang -- or "*" for everything installed:
#
#   flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
#   flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang com.google.Antigravity
#
# Unset enables nothing, which is the right default: these are toolchains the
# agent can run, and they should be there because someone asked for them.
enable_sdk_extensions() {
  local spec="${FLATPAK_ENABLE_SDK_EXT:-}"
  local sdk=() dir ext
  [ -n "$spec" ] || return 0

  if [ "$spec" = "*" ]; then
    for dir in /usr/lib/sdk/*; do
      sdk+=("${dir##*/}")
    done
  else
    IFS=',' read -ra sdk <<< "$spec"
  fi

  for ext in "${sdk[@]}"; do
    [ -n "$ext" ] || continue
    if [ ! -d "/usr/lib/sdk/$ext" ]; then
      msg "Requested SDK extension \"$ext\" is not installed"
      log "Requested SDK extension \"$ext\" is not installed"
      continue
    fi
    log "Enabling SDK extension \"$ext\""
    if [ -f "/usr/lib/sdk/$ext/enable.sh" ]; then
      # Third-party script we do not control: drop errexit and nounset so a
      # stray unset variable or failing command in it cannot take the launcher
      # down before the app ever starts. pipefail is left alone.
      set +eu
      # shellcheck source=/dev/null
      . "/usr/lib/sdk/$ext/enable.sh"
      set -eu
    else
      export PATH="$PATH:/usr/lib/sdk/$ext/bin"
    fi
  done
}

electron_args=()
# No --password-store here on purpose. The app has no route to the Secret
# Service -- see the manifest for why that grant is not given -- and forcing
# gnome-libsecret with nothing to talk to makes safeStorage fail rather than
# fall back. README.md has the opt-in.

if [ -n "${WAYLAND_DISPLAY:-}" ] && [ "${ELECTRON_OZONE_PLATFORM_HINT:-wayland}" = "wayland" ]; then
  electron_args+=("--enable-features=UseOzonePlatform,WaylandWindowDecorations" --enable-wayland-ime --wayland-text-input-version=3)
fi

if [ -n "${XRDP_SESSION:-}" ]; then
  electron_args+=(--disable-gpu --disable-software-rasterizer)
fi

ensure_gemini_dir
enable_tool_extensions
enable_sdk_extensions
log "PATH=$PATH"

export CHROME_DESKTOP=com.google.Antigravity.desktop
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-wayland}"

exec /app/bin/zypak-wrapper.sh /app/extra/antigravity/antigravity "${electron_args[@]}" "$@"
