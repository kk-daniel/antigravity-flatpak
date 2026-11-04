#!/usr/bin/env python3
"""Refresh the Antigravity .deb pins in the Flatpak manifest.

Google publishes the Linux packages without a signature or a signed repository.
What exists is the app's own electron-updater feed -- the URL its
resources/app-update.yml names -- which lists, per architecture, the current
version's .deb with its sha512 and size. That feed, fetched over TLS from
Google's Cloud Run endpoint, is the trust anchor here: the same one the app
itself relies on to update.

The feed carries sha512; Flatpak's extra-data wants sha256. So each .deb is
downloaded, required to match the feed's sha512 and size exactly, and only
then hashed for the manifest. Flatpak checks the sha256 and size again on every
install.

Renovate learns new versions from the same feed but cannot know the build id
in the release path, so it only bumps the version. This script then rewrites
the whole URL from the feed, and refuses if the feed's version differs from
the one the manifest asks for, or if the two architectures disagree.

The manifest is read and rewritten the way the sibling Flatpaks do it: sources
located structurally with PyYAML, values replaced by node mark so comments and
formatting survive, and the result re-parsed and required to say exactly what
was resolved before it is moved into place.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        f"Error: PyYAML is required but is not available to {sys.executable}.\n"
        "The manifest is parsed as YAML rather than scanned, so a structural\n"
        "check is possible at all. Install it (dnf install python3-pyyaml /\n"
        "apt install python3-yaml), or run this script with an interpreter that\n"
        "has it."
    )

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "com.google.Antigravity.yaml"

# From the payload's resources/app-update.yml. CI asserts the installed
# payload still names this URL, so a feed move fails the build.
FEED = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/"
# The manifest module these pins live in, and how --print-version labels them.
MODULE = "antigravity"
# The arch as it appears in the release path, mapped to the value only-arches
# must carry, the filename apply_extra looks for, and the feed file the app
# reads for that architecture (electron-updater channel latest-<arch> plus its
# Linux suffix).
ARCHES = {
    "x64": {"flatpak": "x86_64", "filename": "antigravity-x64.deb", "feed": "latest-x64-linux.yml"},
    "arm": {"flatpak": "aarch64", "filename": "antigravity-arm64.deb", "feed": "latest-arm64-linux-arm64.yml"},
}

RELEASE_URL = re.compile(
    r"https://storage\.googleapis\.com/antigravity-public/antigravity-hub/"
    r"(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<build>[0-9]+)/linux-(?P<arch>x64|arm)/Antigravity\.deb\Z"
)
SHA512_B64 = re.compile(r"[A-Za-z0-9+/]{86}==\Z")


def die(message: str) -> NoReturn:
    sys.exit(f"Error: {message}")


def field(mapping, key):
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            return v
    return None


def manifest_sources(node) -> dict:
    """Map release-path arch -> the manifest's extra-data source for it.

    The module is found by name and only its own sources are read. Within it,
    an extra-data source that is not one of the two .debs would be fetched by
    the build and pinned by nothing here, so it is refused rather than ignored.
    """
    modules = field(node, "modules")
    if modules is None:
        die(f"{MANIFEST.name} has no modules")

    found = [
        m
        for m in modules.value
        if isinstance(m, yaml.MappingNode) and getattr(field(m, "name"), "value", None) == MODULE
    ]
    if len(found) != 1:
        die(f"{MANIFEST.name} has {len(found)} modules named {MODULE}, expected 1")

    sources = field(found[0], "sources")
    if sources is None:
        die(f"the {MODULE} module has no sources")

    by_arch = {}
    for source in sources.value:
        if getattr(field(source, "type"), "value", None) != "extra-data":
            continue

        url_node = field(source, "url")
        if url_node is None:
            die(f"an extra-data source in the {MODULE} module has no url")
        match = RELEASE_URL.fullmatch(url_node.value)
        if match is None:
            die(
                f"the {MODULE} module fetches {url_node.value}, which is not an "
                "Antigravity release .deb this script knows how to pin"
            )

        arch = match.group("arch")
        if arch in by_arch:
            die(f"{MANIFEST.name} has two {arch} extra-data sources")
        spec = ARCHES[arch]

        only = field(source, "only-arches")
        listed = [getattr(n, "value", n) for n in only.value] if only is not None else None
        if listed != [spec["flatpak"]]:
            die(
                f"the {arch} .deb has only-arches {listed or 'missing'}, expected "
                f"[{spec['flatpak']}]. A swapped pair builds one architecture's "
                "flatpak around the other's binary."
            )

        filename = field(source, "filename")
        if filename is None or filename.value != spec["filename"]:
            die(
                f"the {arch} .deb has filename "
                f"{filename.value if filename is not None else 'missing'}, expected "
                f"{spec['filename']}. apply_extra looks the payload up by that name."
            )

        for key in ("sha256", "size"):
            if field(source, key) is None:
                die(f"the {arch} extra-data source has no {key}")

        by_arch[arch] = {
            "version": match.group("version"),
            "url": url_node,
            "sha256": field(source, "sha256"),
            "size": field(source, "size"),
        }

    missing = [a for a in ARCHES if a not in by_arch]
    if missing:
        die(f"the {MODULE} module has no extra-data source for {', '.join(missing)}")
    return by_arch


def fetch_feed(arch: str) -> dict:
    """The .deb entry of one architecture's update feed."""
    url = FEED + ARCHES[arch]["feed"]
    request = urllib.request.Request(url, headers={"User-Agent": "antigravity-flatpak-updater"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            # A truncated body fails to parse or fails the checks below, so a
            # short read cannot be mistaken for a feed.
            document = yaml.safe_load(response.read())
    except urllib.error.HTTPError as error:
        die(f"cannot fetch {url}: HTTP {error.code} {error.reason}")
    except (urllib.error.URLError, TimeoutError) as error:
        die(f"cannot fetch {url}: {error}")
    except yaml.YAMLError as error:
        die(f"{url} is not valid YAML: {error}")

    if not isinstance(document, dict) or not isinstance(document.get("files"), list):
        die(f"{url} is not an electron-updater feed")
    version = str(document.get("version", ""))

    debs = [
        f
        for f in document["files"]
        if isinstance(f, dict) and str(f.get("url", "")).endswith("/Antigravity.deb")
    ]
    if len(debs) != 1:
        die(f"{url} lists {len(debs)} .deb files, expected 1")
    entry = debs[0]

    match = RELEASE_URL.fullmatch(str(entry.get("url")))
    if match is None:
        die(f"{url} serves the .deb from {entry.get('url')}, not a release path this script knows")
    if match.group("arch") != arch:
        die(f"{url} lists the {match.group('arch')} .deb, expected {arch}")
    if match.group("version") != version:
        die(f"{url} says version {version} but lists {entry['url']}")
    if not SHA512_B64.fullmatch(str(entry.get("sha512"))):
        die(f"{url} has a malformed sha512 for the .deb: {entry.get('sha512')!r}")
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        die(f"{url} has size {size!r} for the .deb")

    return {"version": version, "url": entry["url"], "sha512": entry["sha512"], "size": size}


def download_and_hash(url: str, work: Path, sha512_b64: str, size: int) -> str:
    """Download the .deb, require the feed's sha512 and size, return its sha256.

    curl rather than urllib: a connection that dies mid-body is a failed
    transfer here, where urllib's bounded reads can return the short prefix
    without raising.
    """
    target = work / "package.deb"
    result = subprocess.run(
        [
            "curl", "-fL", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "3", "--connect-timeout", "30", "--max-time", "1200",
            "-o", str(target), url,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"cannot fetch {url}: curl exited {result.returncode}\n{result.stderr.strip()}")

    if target.stat().st_size != size:
        die(f"{url} is {target.stat().st_size} bytes, the feed says {size}")
    sha512 = hashlib.sha512()
    sha256 = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha512.update(chunk)
            sha256.update(chunk)
    if base64.b64encode(sha512.digest()).decode() != sha512_b64:
        die(f"{url} does not match the sha512 the feed publishes")
    target.unlink()
    return sha256.hexdigest()


def value_span(text: str, node) -> tuple[int, int]:
    """The span of a scalar's own value, excluding any &anchor or !!tag."""
    start, end = node.start_mark.index, node.end_mark.index
    while start < end and text[start] in "!&":
        while start < end and not text[start].isspace():
            start += 1
        while start < end and text[start].isspace():
            start += 1
    return start, end


def write_manifest(text: str, sources: dict, resolved: dict) -> str:
    """Replace each url/sha256/size value in place, by node mark.

    Only the value is replaced, re-emitted in the style it already had.
    Offsets are absolute rather than (line, column), because PyYAML counts NEL,
    LS and PS as line breaks where str.split does not.
    """
    edits = []
    for arch in ARCHES:
        for key in ("url", "sha256", "size"):
            node = sources[arch][key]
            style = node.style or None
            value = resolved[arch][key]
            if style in ("'", '"'):
                value = f"{style}{value}{style}"
            elif style is not None:
                die(f"cannot rewrite a block scalar (style {style!r}) for {arch} {key}")
            edits.append((node, value))

    # Descending, so replacing one value cannot shift the offsets of the next.
    for node, value in sorted(edits, key=lambda e: e[0].start_mark.index, reverse=True):
        start, end = value_span(text, node)
        text = text[:start] + value + text[end:]
    return text


def check_written(text: str, resolved: dict) -> None:
    """Re-parse the result and require it to fetch exactly what was resolved."""
    document = yaml.safe_load(text)
    modules = [
        m
        for m in document.get("modules", [])
        if isinstance(m, dict) and m.get("name") == MODULE
    ]
    if len(modules) != 1:
        die(f"after rewriting, {MANIFEST.name} has {len(modules)} modules named {MODULE}")

    seen = {}
    for source in modules[0].get("sources", []) or []:
        if not isinstance(source, dict):
            die(f"after rewriting, the {MODULE} module has a source that is not a mapping")
        if source.get("type") != "extra-data":
            continue
        match = RELEASE_URL.fullmatch(str(source.get("url", "")))
        if match is None:
            die(f"after rewriting, the {MODULE} module fetches {source.get('url')}")
        seen[match.group("arch")] = source

    for arch in ARCHES:
        source, want = seen.get(arch), resolved[arch]
        if source is None:
            die(f"after rewriting, {MANIFEST.name} has no {arch} extra-data source")
        for key in ("url", "sha256", "size"):
            if str(source.get(key)) != str(want[key]):
                die(f"after rewriting, the {arch} {key} is {source.get(key)}, expected {want[key]}")


def main() -> None:
    args = sys.argv[1:]
    if args not in ([], ["--print-version"], ["--check"]):
        die(f"usage: {Path(sys.argv[0]).name} [--print-version | --check]")

    # Read and validate the manifest before the network is touched.
    text = MANIFEST.read_text(encoding="utf-8")
    sources = manifest_sources(yaml.compose(text))

    versions = {sources[arch]["version"] for arch in ARCHES}
    if len(versions) != 1:
        die(
            "manifest arch versions disagree: "
            + ", ".join(f"{a}={sources[a]['version']}" for a in ARCHES)
        )
    version = versions.pop()

    if args == ["--print-version"]:
        # CI reads the pin through the same structural code the update path uses.
        print(f"{MODULE}={version}")
        return

    feeds = {arch: fetch_feed(arch) for arch in ARCHES}
    feed_versions = {feeds[arch]["version"] for arch in ARCHES}
    if len(feed_versions) != 1:
        die(
            "the feeds disagree: "
            + ", ".join(f"{a}={feeds[a]['version']}" for a in ARCHES)
            + ". Wait until both architectures are published."
        )
    feed_version = feed_versions.pop()

    if args == ["--check"]:
        # Cheap provenance check for every CI run: no download. The feed only
        # knows the newest release, so a pin that is merely behind is not an
        # error -- Renovate proposes the bump. A pin at the feed's version must
        # name exactly the files and sizes the feed publishes; the sha256 is
        # checked by the Flatpak install in the build job.
        if feed_version != version:
            print(f"The feed is at {feed_version}, the manifest pins {version}; Renovate will propose the bump.")
            return
        for arch in ARCHES:
            pinned = sources[arch]
            if pinned["url"].value != feeds[arch]["url"]:
                die(f"the {arch} url is {pinned['url'].value}, the feed publishes {feeds[arch]['url']}")
            if str(pinned["size"].value) != str(feeds[arch]["size"]):
                die(f"the {arch} size is {pinned['size'].value}, the feed publishes {feeds[arch]['size']}")
        print(f"Antigravity {version} pins match the update feed")
        return

    if feed_version != version:
        die(
            f"the manifest asks for {version} but the feed serves {feed_version}. "
            "The feed only publishes the newest release; bump the version in the "
            "manifest URLs (Renovate does this) and run again."
        )

    resolved = {}
    with tempfile.TemporaryDirectory() as tmp:
        for arch in ARCHES:
            feed = feeds[arch]
            # Flushed: print() block-buffers to a pipe, which is how Actions
            # captures a step, so a stalled download would log nothing at all.
            print(f"Downloading and verifying the {arch} .deb...", flush=True)
            sha256 = download_and_hash(feed["url"], Path(tmp), feed["sha512"], feed["size"])
            resolved[arch] = {"url": feed["url"], "sha256": sha256, "size": str(feed["size"])}

    updated = write_manifest(text, sources, resolved)
    check_written(updated, resolved)

    # Write beside the manifest so the rename stays on one filesystem, and
    # carry the manifest's own mode rather than the temp file's 0600.
    mode = MANIFEST.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", dir=MANIFEST.parent, prefix=MANIFEST.name + ".", delete=False, encoding="utf-8"
    )
    try:
        handle.write(updated)
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, MANIFEST)
    except BaseException:
        os.unlink(handle.name)
        raise

    print(f"Pinned Antigravity {version} from the update feed")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']} size={resolved[arch]['size']}")


if __name__ == "__main__":
    main()
