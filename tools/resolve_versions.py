#!/usr/bin/env python3
"""Work out what to download for a Mattermost deployment, and how to check it.

Online it asks the vendors directly. Offline it reads a bundle built by
tools/build-offline-bundle.sh (manifest.json), or scans a directory someone
filled by hand. Standard library only, so it runs on a bare RHEL host.

Subcommands:
  resolve        print a JSON description of every artifact the components need
  fetch-bundle   download and verify artifacts into a directory, write manifest.json
  inventory      list the installers an offline folder or mirror holds
  parse-archive  debug helper: list releases found in a saved version-archive page
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ARCHIVE_URL = "https://docs.mattermost.com/product-overview/version-archive.html"
MM_RELEASES = "https://releases.mattermost.com"
GITHUB_API = "https://api.github.com"
OPENSEARCH_ARTIFACTS = "https://artifacts.opensearch.org/releases"
GRAFANA_API = "https://grafana.com/api/downloads/grafana/versions"
USER_AGENT = "mattermost-implement-resolver/1.0"

MATTERMOST_KEY = {"gpg_key": "mattermost-pubkey.gpg",
                  "gpg_fingerprint": "A1B31D46F0F3A10B02CF2D44F8F2C31744774B28"}
OPENSEARCH_KEY = {"gpg_key": "opensearch.pgp",
                  "gpg_fingerprint": "C5B7498965EFD1C2924BA9D539D319879310D3FC"}

# Which artifacts each component needs. PostgreSQL and Nginx come from dnf.
COMPONENT_ARTIFACTS = {
    "mattermost": ["mattermost"],
    "rtcd": ["rtcd"],
    "opensearch": ["opensearch", "opensearch_icu"],
    "prometheus": ["prometheus"],
    "node_exporter": ["node_exporter"],
    "postgres_exporter": ["postgres_exporter"],
    "grafana": ["grafana"],
    "nginx_exporter": ["nginx_exporter"],
    "opensearch_exporter": ["opensearch_exporter"],
    "blackbox_exporter": ["blackbox_exporter"],
    "loki": ["loki"],
    "otel_collector": ["otel_collector"],
}

# GitHub-hosted artifacts: repo and asset name template.
GITHUB_ASSETS = {
    "rtcd": ("mattermost/rtcd", "rtcd-linux-{arch}"),
    "prometheus": ("prometheus/prometheus", "prometheus-{version}.linux-{arch}.tar.gz"),
    "node_exporter": ("prometheus/node_exporter", "node_exporter-{version}.linux-{arch}.tar.gz"),
    "postgres_exporter": ("prometheus-community/postgres_exporter",
                          "postgres_exporter-{version}.linux-{arch}.tar.gz"),
    "nginx_exporter": ("nginx/nginx-prometheus-exporter", "nginx-prometheus-exporter_{version}_linux_{arch}.tar.gz"),
    "opensearch_exporter": ("prometheus-community/elasticsearch_exporter",
                            "elasticsearch_exporter-{version}.linux-{arch}.tar.gz"),
    "blackbox_exporter": ("prometheus/blackbox_exporter", "blackbox_exporter-{version}.linux-{arch}.tar.gz"),
    "loki": ("grafana/loki", "loki-linux-{arch}.zip"),
    "otel_collector": ("open-telemetry/opentelemetry-collector-releases",
                       "otelcol-contrib_{version}_linux_{arch}.tar.gz"),
}

# Checksum files published next to each release, so pinned versions can skip the
# rate-limited GitHub API entirely. None means the project only offers API digests.
GITHUB_SUMS = {
    "rtcd": None,
    "prometheus": "sha256sums.txt",
    "node_exporter": "sha256sums.txt",
    "postgres_exporter": "sha256sums.txt",
    "blackbox_exporter": "sha256sums.txt",
    "opensearch_exporter": "sha256sums.txt",
    "nginx_exporter": "nginx-prometheus-exporter_{version}_checksums.txt",
    "loki": "SHA256SUMS",
    # One file per asset holding just the digest.
    "otel_collector": "{asset}.sha256",
}

# Filename patterns used when scanning a hand-made offline directory.
SCAN_PATTERNS = {
    "mattermost": r"^mattermost-(?P<team>team-)?(?P<version>\d+\.\d+\.\d+)-linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "rtcd": r"^rtcd-(?:(?P<version>\d+\.\d+\.\d+)-)?linux-(?P<arch>amd64|arm64)$",
    "opensearch": r"^opensearch-(?P<version>\d+\.\d+\.\d+)-linux-(?P<arch>x64|arm64)\.tar\.gz$",
    "opensearch_icu": r"^analysis-icu-(?P<version>\d+\.\d+\.\d+)\.zip$",
    "prometheus": r"^prometheus-(?P<version>\d+\.\d+\.\d+)\.linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "node_exporter": r"^node_exporter-(?P<version>\d+\.\d+\.\d+)\.linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "postgres_exporter": r"^postgres_exporter-(?P<version>\d+\.\d+\.\d+)\.linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "nginx_exporter": r"^nginx-prometheus-exporter_(?P<version>\d+\.\d+\.\d+)_linux_(?P<arch>amd64|arm64)\.tar\.gz$",
    "opensearch_exporter": r"^elasticsearch_exporter-(?P<version>\d+\.\d+\.\d+)\.linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "blackbox_exporter": r"^blackbox_exporter-(?P<version>\d+\.\d+\.\d+)\.linux-(?P<arch>amd64|arm64)\.tar\.gz$",
    "loki": r"^loki-(?:(?P<version>\d+\.\d+\.\d+)-)?linux-(?P<arch>amd64|arm64)\.zip$",
    "otel_collector": r"^otelcol-contrib_(?P<version>\d+\.\d+\.\d+)_linux_(?P<arch>amd64|arm64)\.tar\.gz$",
    "grafana": r"^grafana(?:-enterprise)?[-_]v?(?P<version>\d+\.\d+\.\d+)(?:[._][^/]*?)?[._]linux[-_](?P<arch>amd64|arm64)\.tar\.gz$",
}


class ResolveError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def http_get(url, timeout=30):
    headers = {"User-Agent": USER_AGENT}
    if url.startswith(GITHUB_API):
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if url.startswith(GITHUB_API) and exc.code in (403, 429):
            raise ResolveError("GitHub's API rate limit was hit (%s). Set GITHUB_TOKEN to a GitHub token "
                               "before running install.sh, pin the version in the answer file, or use an "
                               "offline bundle." % url)
        raise ResolveError("GET %s failed: HTTP %s" % (url, exc.code))
    except (urllib.error.URLError, OSError) as exc:
        raise ResolveError("GET %s failed: %s" % (url, exc))


def http_json(url):
    return json.loads(http_get(url).decode("utf-8"))


def version_key(version):
    return tuple(int(x) for x in re.findall(r"\d+", version or "0"))


def is_latest(version):
    return version in (None, "", "latest")


def opensearch_arch(arch):
    return "x64" if arch == "amd64" else arch


def first_hash(text):
    match = re.search(r"\b([0-9a-fA-F]{64,128})\b", text)
    return match.group(1).lower() if match else ""


def checksum_prefix(digest):
    return {64: "sha256", 128: "sha512"}.get(len(digest), "")


def artifact(name, component, version, url, filename=None, checksum="", checksum_source="",
             sig_url="", key=None, **extra):
    data = {
        "name": name,
        "component": component,
        "version": version,
        "filename": filename or url.rsplit("/", 1)[-1].split("?")[0],
        "url": url,
        "checksum": checksum,
        "checksum_source": checksum_source,
        "sig_url": sig_url,
        "sig_filename": sig_url.rsplit("/", 1)[-1] if sig_url else "",
        "gpg_key": (key or {}).get("gpg_key", ""),
        "gpg_fingerprint": (key or {}).get("gpg_fingerprint", ""),
    }
    data.update(extra)
    return data


# ------------------------------------------------------------------ mattermost

ENTRY_SPLIT = re.compile(r"(?=<p>Mattermost (?:Enterprise|Team) Edition v)")
ENTRY_HEAD = re.compile(r"^<p>Mattermost (Enterprise|Team) Edition v([0-9]+(?:\.[0-9]+)+)")


def parse_archive(page):
    """Return releases from the version-archive page, newest first, as listed."""
    entries = []
    for chunk in ENTRY_SPLIT.split(page)[1:]:
        head = ENTRY_HEAD.match(chunk)
        if not head:
            continue
        # Stop at the next paragraph so an entry never borrows a neighbour's data.
        body = chunk.split("</ul>", 1)[0]
        url = re.search(r"<code>(https://releases\.mattermost\.com/[^<\s]+?\.tar\.gz)</code>", body)
        sha = re.search(r"SHA-256 Checksum:\s*<code>([0-9a-fA-F]{64})</code>", body)
        sig = re.search(r"GPG Signature:\s*<a href=\"?([^\"\s>]+\.sig)", body)
        entries.append({
            "edition": head.group(1).lower(),
            "version": head.group(2),
            "esr": "Extended Support Release" in body.split("<ul>", 1)[0],
            "url": html.unescape(url.group(1)) if url else "",
            "sha256": sha.group(1).lower() if sha else "",
            "sig_url": html.unescape(sig.group(1)) if sig else "",
        })
    return entries


def mattermost_filename(edition, version, arch):
    prefix = "mattermost-team-" if edition == "team" else "mattermost-"
    return "%s%s-linux-%s.tar.gz" % (prefix, version, arch)


def resolve_mattermost(edition, channel, version, arch, archive_page=None, warnings=None):
    warnings = warnings if warnings is not None else []
    entries = []
    try:
        page = archive_page if archive_page is not None else http_get(ARCHIVE_URL).decode("utf-8")
        entries = [e for e in parse_archive(page) if e["edition"] == edition]
    except ResolveError as exc:
        warnings.append("Could not read the Mattermost version archive: %s" % exc)

    chosen = None
    if channel == "pinned":
        if not version:
            raise ResolveError("mattermost_version_channel is pinned but no version was given")
        chosen = next((e for e in entries if e["version"] == version), None)
        resolved = version
    elif channel == "latest-esr":
        chosen = next((e for e in entries if e["esr"]), None)
        if not chosen:
            raise ResolveError("Could not find the latest ESR in the Mattermost version archive. "
                               "Pin a version with mattermost_version_channel: pinned.")
        resolved = chosen["version"]
    elif channel == "latest-stable":
        if entries:
            chosen = entries[0]
            resolved = chosen["version"]
        else:
            release = http_json(GITHUB_API + "/repos/mattermost/mattermost/releases/latest")
            resolved = release["tag_name"].lstrip("v")
            warnings.append("Used GitHub to find the latest Mattermost release; no published SHA-256 "
                            "is available, so the GPG signature is the integrity check.")
    else:
        raise ResolveError("Unknown Mattermost channel: %s" % channel)

    filename = mattermost_filename(edition, resolved, arch)
    url = "%s/%s/%s" % (MM_RELEASES, resolved, filename)
    checksum, source = "", ""
    # The archive lists amd64 tarballs. Only use its checksum when the file matches.
    if chosen and chosen["sha256"] and chosen["url"].endswith("/" + filename):
        checksum, source = "sha256:" + chosen["sha256"], "mattermost-version-archive"
    elif chosen is None and channel == "pinned":
        warnings.append("Mattermost %s is not in the version archive; relying on the GPG signature." % resolved)
    return artifact("mattermost", "mattermost", resolved, url, filename, checksum, source,
                    url + ".sig", MATTERMOST_KEY, edition=edition, channel=channel, arch=arch,
                    esr=bool(chosen and chosen["esr"]))


# ------------------------------------------------------------------ github

def github_release(repo, version):
    if is_latest(version):
        return http_json("%s/repos/%s/releases/latest" % (GITHUB_API, repo))
    last_error = None
    for tag in ("v" + version.lstrip("v"), version.lstrip("v")):
        try:
            return http_json("%s/repos/%s/releases/tags/%s" % (GITHUB_API, repo, tag))
        except ResolveError as exc:
            last_error = exc
    raise last_error


def sums_lookup(text, wanted):
    words = text.split()
    if len(words) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", words[0]):
        return words[0].lower()  # a per-asset file with only the digest in it
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == wanted:
            return parts[0].lower()
    return ""


def resolve_github(name, version, arch):
    repo, template = GITHUB_ASSETS[name]
    sums = GITHUB_SUMS.get(name)
    if not is_latest(version) and sums:
        # Pinned: build the download URL and read the published checksum file. No API call.
        resolved = version.lstrip("v")
        wanted = template.format(version=resolved, arch=arch)
        base = "https://github.com/%s/releases/download/v%s/" % (repo, resolved)
        sums_name = sums.format(version=resolved, asset=wanted)
        digest = sums_lookup(http_get(base + sums_name).decode("utf-8"), wanted)
        if not digest:
            raise ResolveError("%s %s: %s has no entry for %s" % (repo, resolved, sums_name, wanted))
        return artifact(name, name, resolved, base + wanted, wanted, "sha256:" + digest, sums_name, arch=arch)
    release = github_release(repo, version)
    resolved = release["tag_name"].lstrip("v")
    wanted = template.format(version=resolved, arch=arch)
    assets = {a["name"]: a for a in release.get("assets", [])}
    if wanted not in assets:
        raise ResolveError("%s %s has no asset called %s" % (repo, resolved, wanted))
    asset = assets[wanted]
    checksum, source = "", ""
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        checksum, source = digest, "github-asset-digest"
    elif any(n in assets for n in ("sha256sums.txt",)) or any(n.endswith("_checksums.txt") for n in assets):
        sums_name = "sha256sums.txt" if "sha256sums.txt" in assets else next(n for n in assets if n.endswith("_checksums.txt"))
        sums = http_get(assets[sums_name]["browser_download_url"]).decode("utf-8")
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == wanted:
                checksum, source = "sha256:" + parts[0].lower(), sums_name
    component = name
    return artifact(name, component, resolved, asset["browser_download_url"], wanted, checksum,
                    source, arch=arch)


# ------------------------------------------------------------------ opensearch

def resolve_opensearch(version, arch):
    if is_latest(version):
        version = github_release("opensearch-project/OpenSearch", "latest")["tag_name"].lstrip("v")
    filename = "opensearch-%s-linux-%s.tar.gz" % (version, opensearch_arch(arch))
    url = "%s/bundle/opensearch/%s/%s" % (OPENSEARCH_ARTIFACTS, version, filename)
    digest = first_hash(http_get(url + ".sha512").decode("utf-8"))
    return artifact("opensearch", "opensearch", version, url, filename,
                    "sha512:" + digest if digest else "", "vendor .sha512", url + ".sig",
                    OPENSEARCH_KEY, arch=arch)


def resolve_opensearch_icu(version):
    filename = "analysis-icu-%s.zip" % version
    url = "%s/plugins/analysis-icu/%s/%s" % (OPENSEARCH_ARTIFACTS, version, filename)
    digest = first_hash(http_get(url + ".sha512").decode("utf-8"))
    return artifact("opensearch_icu", "opensearch", version, url, filename,
                    "sha512:" + digest if digest else "", "vendor .sha512", url + ".sig", OPENSEARCH_KEY)


# ------------------------------------------------------------------ grafana

def resolve_grafana(version, arch):
    if is_latest(version):
        version = github_release("grafana/grafana", "latest")["tag_name"].lstrip("v")
    try:
        data = http_json("%s/%s" % (GRAFANA_API, version))
        for pkg in data.get("packages", []):
            if pkg.get("os") == "linux" and pkg.get("arch") == arch and pkg.get("url", "").endswith(".tar.gz"):
                if "enterprise" in pkg["url"]:
                    continue
                return artifact("grafana", "grafana", version, pkg["url"], None,
                                "sha256:" + pkg["sha256"] if pkg.get("sha256") else "",
                                "grafana.com downloads API", arch=arch)
    except ResolveError:
        pass
    url = "https://dl.grafana.com/oss/release/grafana-%s.linux-%s.tar.gz" % (version, arch)
    digest = first_hash(http_get(url + ".sha256").decode("utf-8"))
    return artifact("grafana", "grafana", version, url, None, "sha256:" + digest if digest else "",
                    "vendor .sha256", arch=arch)


# ------------------------------------------------------------------ online

def needed_artifacts(components):
    names = []
    for component in components:
        for name in COMPONENT_ARTIFACTS.get(component, []):
            if name not in names:
                names.append(name)
    return names


def resolve_online(components, arch, mm, versions, warnings):
    result = {}
    for name in needed_artifacts(components):
        if name == "mattermost":
            result[name] = resolve_mattermost(mm["edition"], mm["channel"], mm["version"], arch,
                                              warnings=warnings)
        elif name in GITHUB_ASSETS:
            result[name] = resolve_github(name, versions.get(name, "latest"), arch)
        elif name == "opensearch":
            result[name] = resolve_opensearch(versions.get("opensearch", "latest"), arch)
        elif name == "opensearch_icu":
            os_version = result["opensearch"]["version"] if "opensearch" in result else versions.get("opensearch")
            result[name] = resolve_opensearch_icu(os_version)
        elif name == "grafana":
            result[name] = resolve_grafana(versions.get("grafana", "latest"), arch)
        result[name]["source"] = "online"
        if not result[name]["checksum"]:
            if result[name].get("sig_url"):
                warnings.append("%s %s: no published checksum for this build; the GPG signature is the "
                                "integrity check." % (name, result[name]["version"]))
            else:
                warnings.append("%s %s: no published checksum found." % (name, result[name]["version"]))
    return result


# ------------------------------------------------------------------ offline

def join_source(source, relpath):
    if re.match(r"^https?://", source):
        return source.rstrip("/") + "/" + relpath.lstrip("/")
    return os.path.join(source, relpath)


def read_source(source, relpath):
    location = join_source(source, relpath)
    if re.match(r"^https?://", location):
        return http_get(location).decode("utf-8")
    with open(location, "r", encoding="utf-8") as handle:
        return handle.read()


def load_manifest(source):
    try:
        return json.loads(read_source(source, "manifest.json"))
    except (OSError, ResolveError, ValueError):
        return None


def scan_directory(source, arch):
    """Build a manifest from a directory that someone filled by hand."""
    sums = {}
    found = []
    for root, _dirs, files in os.walk(source):
        for fname in files:
            path = os.path.join(root, fname)
            if fname in ("SHA256SUMS", "sha256sums.txt") or fname.endswith((".sha256", ".sha256sum")):
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        parts = line.split()
                        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                            sums[os.path.basename(parts[-1].lstrip("*"))] = parts[0].lower()
                        elif len(parts) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                            sums[fname.rsplit(".sha256", 1)[0]] = parts[0].lower()
            for name, pattern in SCAN_PATTERNS.items():
                match = re.match(pattern, fname)
                if not match:
                    continue
                groups = match.groupdict()
                file_arch = groups.get("arch")
                if file_arch and file_arch not in (arch, opensearch_arch(arch)):
                    continue
                entry = {
                    "name": name,
                    "component": "opensearch" if name == "opensearch_icu" else name,
                    "version": groups.get("version") or "unknown",
                    "filename": fname,
                    "path": os.path.relpath(path, source),
                    "arch": arch,
                }
                if name == "mattermost":
                    entry["edition"] = "team" if groups.get("team") else "enterprise"
                    entry.update(MATTERMOST_KEY)
                if name in ("opensearch", "opensearch_icu"):
                    entry.update(OPENSEARCH_KEY)
                if os.path.exists(path + ".sig"):
                    entry["sig_path"] = entry["path"] + ".sig"
                found.append(entry)
    for entry in found:
        digest = sums.get(entry["filename"], "")
        entry["checksum"] = "sha256:" + digest if digest else ""
        entry["checksum_source"] = "SHA256SUMS in artifact directory" if digest else ""
    return {"artifacts": found}


def pick(candidates, name, version, warnings, channel=None):
    if not candidates:
        raise ResolveError("No %s artifact found in the offline source." % name)
    if channel in ("latest-stable", "latest-esr"):
        tagged = [c for c in candidates if channel in (c.get("channels") or [c.get("channel")])]
        if tagged:
            return max(tagged, key=lambda c: version_key(c["version"]))
        warnings.append("Offline source has no %s build tagged %s; using the newest one present." % (name, channel))
        return max(candidates, key=lambda c: version_key(c["version"]))
    if not is_latest(version) and channel != "latest":
        exact = [c for c in candidates if c["version"] == version.lstrip("v")]
        if not exact:
            have = ", ".join(sorted({c["version"] for c in candidates}))
            raise ResolveError("Offline source has %s %s, but %s was requested." % (name, have, version))
        return exact[0]
    return max(candidates, key=lambda c: version_key(c["version"]))


def resolve_offline(source, components, arch, mm, versions, warnings):
    manifest = load_manifest(source)
    mode = "bundle"
    if manifest is None:
        if re.match(r"^https?://", source):
            raise ResolveError("No manifest.json at %s. A mirror URL needs a bundle built by "
                               "tools/build-offline-bundle.sh." % source)
        if not os.path.isdir(source):
            raise ResolveError("Offline source %s does not exist." % source)
        manifest = scan_directory(source, arch)
        mode = "directory"
        warnings.append("No manifest.json in %s; scanned the directory instead." % source)

    result = {}
    for name in needed_artifacts(components):
        candidates = [a for a in manifest.get("artifacts", [])
                      if a.get("name") == name and a.get("arch") in (None, arch, opensearch_arch(arch))]
        if name == "mattermost":
            candidates = [a for a in candidates if a.get("edition", "enterprise") == mm["edition"]]
            channel = mm["channel"] if mm["channel"] != "pinned" else None
            chosen = pick(candidates, name, mm["version"] if mm["channel"] == "pinned" else "latest",
                          warnings, channel)
        elif name == "opensearch_icu":
            os_version = result.get("opensearch", {}).get("version", versions.get("opensearch"))
            chosen = pick(candidates, name, os_version, warnings)
        else:
            chosen = pick(candidates, name, versions.get(name, "latest"), warnings)
        entry = dict(chosen)
        entry.setdefault("component", name)
        entry["url"] = join_source(source, entry["path"])
        entry["sig_url"] = join_source(source, entry["sig_path"]) if entry.get("sig_path") else ""
        entry["sig_filename"] = entry["sig_url"].rsplit("/", 1)[-1] if entry["sig_url"] else ""
        entry.setdefault("gpg_key", "")
        entry.setdefault("gpg_fingerprint", "")
        entry.setdefault("checksum", "")
        entry.setdefault("checksum_source", "")
        if entry["checksum"] and mode == "bundle":
            entry["checksum_source"] = "bundle manifest (%s)" % (chosen.get("checksum_source") or "verified at bundle build")
        entry["source"] = mode
        result[name] = entry
    return result


def inventory(source, arch):
    """Describe what an offline source holds, for the installer to show the user."""
    if not re.match(r"^https?://", source) and not os.path.isdir(source):
        raise ResolveError("%s is not a directory or http(s) URL." % source)
    manifest = load_manifest(source)
    kind = "bundle"
    if manifest is None:
        if re.match(r"^https?://", source):
            raise ResolveError("No manifest.json at %s. A mirror URL needs a bundle built by "
                               "tools/build-offline-bundle.sh." % source)
        manifest = scan_directory(source, arch)
        kind = "folder"
    items = []
    for a in manifest.get("artifacts", []):
        if a.get("arch") not in (None, arch, opensearch_arch(arch)):
            continue
        items.append({
            "name": a["name"], "version": a.get("version", "?"), "edition": a.get("edition", ""),
            "channels": a.get("channels") or ([a["channel"]] if a.get("channel") else []),
            "checksum": bool(a.get("checksum")), "signature": bool(a.get("sig_path")),
        })
    local = not re.match(r"^https?://", source)
    extras = {
        "collections": local and os.path.isdir(os.path.join(source, "collections"))
                       and any(f.endswith(".tar.gz") for f in os.listdir(os.path.join(source, "collections"))),
        "rpms": local and os.path.isdir(os.path.join(source, "rpms"))
                and any(f.endswith(".rpm") for f in os.listdir(os.path.join(source, "rpms"))),
    }
    return {"kind": kind, "arch": arch, "artifacts": sorted(items, key=lambda i: (i["name"], i["version"])),
            "extras": extras}


# ------------------------------------------------------------------ bundle

def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, 1024 * 1024)
    except (urllib.error.URLError, OSError) as exc:
        raise ResolveError("Download of %s failed: %s" % (url, exc))
    os.replace(tmp, dest)


def file_digest(path, algorithm):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def gpg_verify(path, sig, keyfile, fingerprint):
    """Return 'verified', 'gpg-missing' or raise on a bad signature."""
    if not shutil.which("gpg"):
        return "gpg-missing"
    home = tempfile.mkdtemp(prefix="mmd-gpg-")
    try:
        subprocess.run(["gpg", "--batch", "--homedir", home, "--import", keyfile],
                       check=True, capture_output=True)
        out = subprocess.run(["gpg", "--batch", "--homedir", home, "--status-fd", "1", "--verify", sig, path],
                             capture_output=True, text=True)
        for line in out.stdout.splitlines():
            if line.startswith("[GNUPG:] VALIDSIG"):
                if line.split()[-1].upper() == fingerprint.upper():
                    return "verified"
        raise ResolveError("GPG signature check failed for %s" % path)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def fetch_bundle(out_dir, components, arch, mm, versions, keys_dir, warnings):
    os.makedirs(out_dir, exist_ok=True)
    channels = ["latest-stable", "latest-esr"] if mm["channel"] == "both" else [mm["channel"]]
    entries = []
    seen = {}
    for channel in channels:
        resolved = resolve_online(components, arch, dict(mm, channel=channel), versions, warnings)
        for name, art in resolved.items():
            key = (name, art["version"], art.get("edition"))
            if key in seen:
                seen[key].setdefault("channels", []).append(channel)
                continue
            subdir = os.path.join(out_dir, art["component"])
            os.makedirs(subdir, exist_ok=True)
            dest = os.path.join(subdir, art["filename"])
            print("Downloading %s %s" % (name, art["version"]), file=sys.stderr)
            download(art["url"], dest)
            if art["checksum"]:
                algo, expected = art["checksum"].split(":", 1)
                if file_digest(dest, algo) != expected:
                    raise ResolveError("Checksum mismatch for %s" % art["filename"])
            elif not mm.get("allow_unverified"):
                raise ResolveError("%s has no published checksum. Re-run with --allow-unverified to accept it." % name)
            entry = dict(art)
            entry.pop("url", None)
            entry["path"] = os.path.relpath(dest, out_dir)
            entry["sha256"] = file_digest(dest, "sha256")
            if not entry["checksum"]:
                entry["checksum"] = "sha256:" + entry["sha256"]
                entry["checksum_source"] = "computed at bundle build (no vendor checksum)"
            if art.get("sig_url"):
                sig_dest = dest + ".sig"
                download(art["sig_url"], sig_dest)
                entry["sig_path"] = entry["path"] + ".sig"
                entry["signature_at_build"] = gpg_verify(dest, sig_dest, os.path.join(keys_dir, art["gpg_key"]),
                                                         art["gpg_fingerprint"])
            entry["channels"] = [channel] if name == "mattermost" else []
            entry.pop("sig_url", None)
            entry.pop("sig_filename", None)
            seen[key] = entry
            entries.append(entry)
    manifest = {"format": 1, "arch": arch, "artifacts": entries, "warnings": warnings}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    with open(os.path.join(out_dir, "SHA256SUMS"), "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write("%s  %s\n" % (entry["sha256"], entry["path"]))
    return manifest


# ------------------------------------------------------------------ cli

def parse_versions(pairs):
    versions = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ResolveError("--version expects name=version, got %s" % pair)
        name, value = pair.split("=", 1)
        versions[name.strip()] = value.strip()
    return versions


def split_components(text):
    if text == "all":
        return list(COMPONENT_ARTIFACTS)
    return [c.strip() for c in (text or "").split(",") if c.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--components", required=True, help="comma-separated components, or 'all'")
        p.add_argument("--arch", default="amd64", choices=["amd64", "arm64"])
        p.add_argument("--mattermost-edition", default="enterprise", choices=["enterprise", "team"])
        p.add_argument("--mattermost-version", default="")
        p.add_argument("--version", action="append", default=[], metavar="NAME=VERSION",
                       help="pin a third-party artifact, e.g. opensearch=2.19.7 (default latest)")

    p_resolve = sub.add_parser("resolve", help="print the artifacts to download as JSON")
    common(p_resolve)
    p_resolve.add_argument("--mattermost-channel", default="latest-stable",
                           choices=["latest-stable", "latest-esr", "pinned"])
    p_resolve.add_argument("--source", default="", help="offline directory or mirror URL")

    p_bundle = sub.add_parser("fetch-bundle", help="download and verify artifacts into a directory")
    common(p_bundle)
    p_bundle.add_argument("--mattermost-channel", default="both",
                          choices=["latest-stable", "latest-esr", "pinned", "both"])
    p_bundle.add_argument("--out", required=True)
    p_bundle.add_argument("--keys-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             "..", "roles", "artifacts", "files"))
    p_bundle.add_argument("--allow-unverified", action="store_true")

    p_inv = sub.add_parser("inventory", help="list the installers an offline folder or mirror holds")
    p_inv.add_argument("--source", required=True)
    p_inv.add_argument("--arch", default="amd64", choices=["amd64", "arm64"])

    p_parse = sub.add_parser("parse-archive", help="list releases in a saved version-archive page")
    p_parse.add_argument("file")

    args = parser.parse_args(argv)
    warnings = []
    try:
        if args.command == "inventory":
            print(json.dumps(inventory(args.source, args.arch), indent=2))
            return 0

        if args.command == "parse-archive":
            with open(args.file, "r", encoding="utf-8") as handle:
                print(json.dumps(parse_archive(handle.read()), indent=2))
            return 0

        mm = {"edition": args.mattermost_edition, "channel": args.mattermost_channel,
              "version": args.mattermost_version}
        versions = parse_versions(args.version)
        components = split_components(args.components)

        if args.command == "resolve":
            if args.source:
                result = resolve_offline(args.source, components, args.arch, mm, versions, warnings)
                mode = "offline"
            else:
                result = resolve_online(components, args.arch, mm, versions, warnings)
                mode = "online"
            print(json.dumps({"mode": mode, "artifacts": result, "warnings": warnings}, indent=2, sort_keys=True))
            return 0

        if args.command == "fetch-bundle":
            mm["allow_unverified"] = args.allow_unverified
            manifest = fetch_bundle(args.out, components, args.arch, mm, versions, args.keys_dir, warnings)
            print(json.dumps({"artifacts": len(manifest["artifacts"]), "warnings": warnings}, indent=2))
            return 0
    except ResolveError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
