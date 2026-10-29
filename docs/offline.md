# Offline installs

Air-gapped servers need two things the internet would normally give them: the dnf packages
for Ansible and the base OS components, and the Mattermost and third-party tarballs.

## Packages

The installer needs `ansible-core`, `python3`, `gnupg2`, `openssl`, `firewalld` and a few
others, and the roles install PostgreSQL and Nginx from AppStream. Most air-gapped RHEL
estates already have Satellite or a local mirror, and that's all we need. If not:

- mount the RHEL DVD ISO and pass `--local-repo /mnt/rhel9` to `install.sh`, or
- build the bundle with `--with-rpms` on a RHEL server with the same major version. The
  installer falls back to those RPMs if dnf can't reach anything. That covers the installer's
  own prerequisites, not PostgreSQL or Nginx, so for those you still need a repo.

## Building a bundle

On any machine with internet access and Python 3:

```bash
tools/build-offline-bundle.sh --arch amd64 --edition enterprise --channel both
```

`--channel both` puts the latest stable and the latest ESR Mattermost builds in the bundle,
so the answer file can choose either on the day. `--mattermost-version 11.7.14` pins one
instead. Third-party versions default to the pins in `inventory/group_vars/all/defaults.yml`;
override them with `--version opensearch=3.3.0` and so on.

The script downloads every artifact and checks it against the vendor's checksum. Where gpg is
installed, it checks the Mattermost and OpenSearch signatures too. It then writes:

```text
mm-bundle-20261028/
  manifest.json          what is in the bundle, versions, checksums, which channel each Mattermost build is
  SHA256SUMS             sha256 of every artifact
  mattermost/            tarballs and their .sig files
  rtcd/ opensearch/ prometheus/ node_exporter/ postgres_exporter/ grafana/
  keys/                  vendor GPG keys (the installer uses its own copies, see below)
  collections/           ansible.posix tarball and manifest.txt
  rpms/                  only with --with-rpms
mm-bundle-20261028.tar.gz
```

Copy the tarball across, unpack it and run:

```bash
./install.sh --offline-dir /srv/mm-bundle-20261028
```

Or put `artifact_source: /srv/mm-bundle-20261028` in the answer file. An `https://` URL works
too, if you publish the bundle directory on an internal web server.

## Hand-filled directories

If someone has already downloaded the files, point `artifact_source` at the directory. With no
`manifest.json` the resolver scans it for the vendors' file names, such as
`mattermost-11.7.14-linux-amd64.tar.gz`, `rtcd-linux-amd64` and
`prometheus-3.15.1.linux-amd64.tar.gz`. It reads checksums from any `SHA256SUMS`,
`sha256sums.txt` or `*.sha256` files it finds alongside them.

The Mattermost and OpenSearch `.sig` files must sit next to their tarballs. Without them the
install stops, unless you turn off `artifact_require_signature`. Anything with neither a
checksum nor a signature also stops the install, unless you set `artifact_allow_unverified`.
Don't set that without checking the files some other way first.

## How verification works

Every artifact is checked twice before anything is unpacked.

The checksum comes from the vendor when we are online: the Mattermost version archive, GitHub's
asset digests, Prometheus's `sha256sums.txt`, OpenSearch's `.sha512` files and Grafana's
downloads API. Offline, it comes from the bundle manifest, which was checked against the vendor
when the bundle was built.

The GPG signature, for Mattermost and OpenSearch, is checked against keys held in this repo
(`roles/artifacts/files/`) with the fingerprints pinned in `defaults.yml`:

| Vendor | Fingerprint |
|---|---|
| Mattermost | `A1B31D46F0F3A10B02CF2D44F8F2C31744774B28` |
| OpenSearch | `C5B7498965EFD1C2924BA9D539D319879310D3FC` |

The keys shipped in a bundle are only there for people who want to check things by hand. The
installer never trusts a key from the download source. If a vendor rotates its key, update the
file and the fingerprint in this repo, and re-run `make test`.

Mattermost's version archive lists SHA-256 values for amd64 tarballs only. On arm64 the
signature does the job on its own, and the report says so.
