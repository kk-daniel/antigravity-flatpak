# Flatpak for Antigravity

Unofficial Flatpak packaging for Google's [Antigravity](https://antigravity.google)
app — the agentic desktop application ("Antigravity 2", the
[Antigravity App](https://antigravity.google/product/antigravity-2)), not the
standalone Antigravity IDE.

The Flatpak uses Google's official Linux `.deb` builds for **x86_64 and
aarch64**. The app is Electron, so the package follows the Claude and ChatGPT
Flatpaks: the freedesktop SDK as runtime, the Electron BaseApp with Zypak for
Chromium's sandbox, and the `.deb` as `extra-data`.

## Building and installing

```sh
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo
```

With a host `flatpak-builder`:

```sh
flatpak-builder --force-clean --user --install-deps-from=flathub --repo=repo --install builddir com.google.Antigravity/com.google.Antigravity.yaml
```

With `org.flatpak.Builder`, build only, and install with the host's
`flatpak`. Installing runs `apply_extra` in a bubblewrap sandbox, which cannot
be created from inside the Builder's own sandbox. `FLATPAK_USER_DIR` points
the Builder at your real user installation rather than its private one:

```sh
flatpak run --env=FLATPAK_USER_DIR="$HOME/.local/share/flatpak" --command=flatpak-builder org.flatpak.Builder --force-clean --user --install-deps-from=flathub --repo=repo builddir com.google.Antigravity/com.google.Antigravity.yaml
```

```sh
flatpak remote-add --user --no-gpg-verify --if-not-exists antigravity-local "$PWD/repo"
```

```sh
flatpak install --user antigravity-local com.google.Antigravity
```

```sh
flatpak run com.google.Antigravity
```

The source repository and the published Flatpak remote contain no Google
application code. Installing from either, Flatpak downloads the architecture's
`.deb` straight from Google, verifies its pinned SHA-256 and size, and runs
`apply_extra` locally.

The `.flatpak` bundles attached to releases are the exception. Flatpak never
downloads extra-data while installing a bundle, so a working bundle has to
carry the `.deb` inside it, and these do.

`apply_extra` streams the `.deb`'s data member through `bsdtar`, moves
`/opt/Antigravity` to `/app/extra/antigravity`, checks that the binaries match
the `.deb`'s architecture, and removes three things:

- `chrome-sandbox`, because Zypak provides the sandbox and Chromium aborts on a
  non-setuid helper;
- `resources/package-type`, so the bundled electron-updater stays inactive
  instead of downloading every release and trying to install it with
  `pkexec` — updates come through Flatpak;
- `resources/apparmor-profile`, which only the `.deb`'s own `postinst` uses.

## Upstream verification

Google publishes the Linux packages without a signature or a signed
repository. The download page offers `tar.gz` archives; the `.deb`s come from
the app's own electron-updater feed — the URL in its `resources/app-update.yml`
— which lists, per architecture, the current version's packages with their
`sha512` and size:

- `…/manifest/latest-x64-linux.yml`
- `…/manifest/latest-arm64-linux-arm64.yml`

That feed, fetched over TLS from Google's Cloud Run endpoint, is the trust
anchor: the same one the app itself relies on to update. `update-feed-metadata.py`

1. reads both feeds and refuses if they disagree on the version, or if it
   differs from the version the manifest asks for;
2. downloads each `.deb` and requires the feed's `sha512` and size exactly;
3. writes the feed's URL (the release path carries a build id Renovate cannot
   know), the SHA-256 Flatpak needs and the size into the manifest.

Flatpak checks the SHA-256 and size again on every install. CI's `verify-pins`
job runs `update-feed-metadata.py --check` on every build, which compares the
pins with the live feed without downloading anything, and the build's smoke
test fails if the installed payload's `app-update.yml` ever names a different
feed.

## Sandboxing

The app has no access to the real home directory. `--persist=.gemini` gives it
a private `~/.gemini` — Antigravity's config, MCP servers, plugins, skills and
workflows — stored on the host at `~/.var/app/com.google.Antigravity/.gemini`.
It is not shared with a host-installed Gemini CLI or Antigravity. Files reach
the app through the file chooser portal; to let it work on existing checkouts,
grant the directory that holds them:

```sh
flatpak override --user --filesystem=~/src com.google.Antigravity
```

The agent's language server can confine commands with Landlock. Whether
Flatpak's seccomp filter lets it do so inside the sandbox is not verified yet.

### Login keyring

The app is not granted `--talk-name=org.freedesktop.secrets`. Secret Service
has no per-application isolation: a client allowed onto it can read other
unlocked secrets too, and here that would extend to every command the agent
runs. To opt into the login keyring:

```sh
flatpak override --user --talk-name=org.freedesktop.secrets com.google.Antigravity
```

Undo it with `--no-talk-name=org.freedesktop.secrets`. Expect to sign in again
after switching.

## Adding tools

VS Code tool extensions such as `podman`, `fish` and `git-lfs` are detected
automatically under `/app/tools` — note the `//25.08` branch, which has to
match:

```sh
flatpak install flathub com.visualstudio.code.tool.podman//25.08
```

SDK extensions are opt-in through `FLATPAK_ENABLE_SDK_EXT`, as a
comma-separated list of short names or `*` for everything installed:

```sh
flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang com.google.Antigravity
```

The launcher records what it wired up:

```sh
tail ~/.var/app/com.google.Antigravity/cache/antigravity-flatpak/launcher.log
```

## Updating Antigravity

Renovate reads the x64 feed through a custom datasource and bumps the version
in both URLs in one automerged `antigravity` group. The post-Renovate workflow
then resolves the build id, SHA-256 and size from the feed and adds the
metainfo release:

```sh
./com.google.Antigravity/update-feed-metadata.py
./com.google.Antigravity/update-metainfo.sh
```

The updater needs `curl` and a `python3` with PyYAML. `--print-version` prints
`antigravity=<version>` without touching the network.

The feed only publishes the newest release. The updater therefore pins
whatever the feed serves at that moment, and only if it is the version the
manifest asks for.

## CI

`.github/workflows/flatpak-build.yaml` builds natively on `ubuntu-24.04` and
`ubuntu-24.04-arm`, installs each result — which runs `apply_extra` against
that architecture's `.deb` — and checks the payload and its library
dependencies inside the sandbox. A `publish` job merges both architectures into
one signed OSTree repository for GitHub Pages, and release bundles are attached
per architecture.

It expects:

- variables `GPG_KEY_ID`, `GPG_KEY_GREP`, and `APP_CLIENT_ID`
- secrets `GPG_PRIVATE_KEY`, `GPG_PASSPHRASE`, and `APP_PRIVATE_KEY`
- GitHub Pages enabled with GitHub Actions as its source
- the GitHub App installed on the repository with Contents read and write
- arm64 hosted runners, which are free for public repositories

Once published:

```sh
flatpak remote-add --if-not-exists --user antigravity-flatpak https://kk-daniel.github.io/antigravity-flatpak/antigravity.flatpakrepo
```

```sh
flatpak install --user antigravity-flatpak com.google.Antigravity
```

## Licence

MIT for the packaging in this repository. Antigravity itself is proprietary,
© Google LLC, and covered by [Antigravity's terms](https://antigravity.google/terms).
It is downloaded directly from Google at Flatpak installation time and is not
redistributed by this source repository or its Flatpak remote; the release
bundles do embed it, as described above.
