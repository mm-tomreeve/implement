"""Unit tests for tools/resolve_versions.py. No network access needed."""

import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import resolve_versions as rv  # noqa: E402

with open(os.path.join(HERE, "fixtures", "version-archive.html"), encoding="utf-8") as handle:
    ARCHIVE = handle.read()

MM = {"edition": "enterprise", "channel": "latest-stable", "version": ""}


class ParseArchive(unittest.TestCase):
    def test_finds_both_editions_newest_first(self):
        entries = rv.parse_archive(ARCHIVE)
        ent = [e for e in entries if e["edition"] == "enterprise"]
        team = [e for e in entries if e["edition"] == "team"]
        self.assertEqual(ent[0]["version"], "12.0.1")
        self.assertEqual(team[0]["version"], "12.0.1")
        self.assertEqual(len(ent), 5)

    def test_reads_checksum_signature_and_esr_flag(self):
        first = rv.parse_archive(ARCHIVE)[0]
        self.assertEqual(first["sha256"], "f74b26f3a245b336b5dc7978a71ac7cd17ba22601de2c68b223dd0114cb2676c")
        self.assertTrue(first["sig_url"].endswith("mattermost-12.0.1-linux-amd64.tar.gz.sig"))
        self.assertFalse(first["esr"])
        esr = [e for e in rv.parse_archive(ARCHIVE) if e["esr"]]
        self.assertEqual(esr[0]["version"], "11.7.14")


class ResolveMattermost(unittest.TestCase):
    def resolve(self, **kwargs):
        args = dict(edition="enterprise", channel="latest-stable", version="", arch="amd64")
        args.update(kwargs)
        return rv.resolve_mattermost(args["edition"], args["channel"], args["version"], args["arch"],
                                     archive_page=ARCHIVE, warnings=[])

    def test_latest_stable(self):
        art = self.resolve()
        self.assertEqual(art["version"], "12.0.1")
        self.assertEqual(art["url"], "https://releases.mattermost.com/12.0.1/mattermost-12.0.1-linux-amd64.tar.gz")
        self.assertEqual(art["checksum"], "sha256:f74b26f3a245b336b5dc7978a71ac7cd17ba22601de2c68b223dd0114cb2676c")
        self.assertEqual(art["gpg_fingerprint"], "A1B31D46F0F3A10B02CF2D44F8F2C31744774B28")

    def test_latest_esr(self):
        art = self.resolve(channel="latest-esr")
        self.assertEqual(art["version"], "11.7.14")
        self.assertTrue(art["esr"])

    def test_team_edition_file_name(self):
        art = self.resolve(edition="team")
        self.assertEqual(art["filename"], "mattermost-team-12.0.1-linux-amd64.tar.gz")
        self.assertTrue(art["checksum"].startswith("sha256:"))

    def test_arm64_has_signature_but_no_archive_checksum(self):
        art = self.resolve(arch="arm64")
        self.assertEqual(art["filename"], "mattermost-12.0.1-linux-arm64.tar.gz")
        self.assertEqual(art["checksum"], "")
        self.assertTrue(art["sig_url"].endswith(".sig"))

    def test_pinned_version_in_archive(self):
        art = self.resolve(channel="pinned", version="11.10.4")
        self.assertEqual(art["version"], "11.10.4")
        self.assertTrue(art["checksum"].startswith("sha256:d0e312ed"))

    def test_pinned_without_version_fails(self):
        with self.assertRaises(rv.ResolveError):
            self.resolve(channel="pinned")


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundle_manifest_picks_the_requested_channel(self):
        artifacts = []
        for version, channel in (("12.0.1", "latest-stable"), ("11.7.14", "latest-esr")):
            rel = "mattermost/mattermost-%s-linux-amd64.tar.gz" % version
            digest = write(os.path.join(self.dir, rel), version.encode())
            artifacts.append({"name": "mattermost", "component": "mattermost", "version": version,
                              "edition": "enterprise", "arch": "amd64", "path": rel, "sig_path": rel + ".sig",
                              "filename": os.path.basename(rel), "checksum": "sha256:" + digest,
                              "channels": [channel], "gpg_key": "mattermost-pubkey.gpg",
                              "gpg_fingerprint": "A1B31D46F0F3A10B02CF2D44F8F2C31744774B28"})
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"artifacts": artifacts}, handle)
        result = rv.resolve_offline(self.dir, ["mattermost"], "amd64", dict(MM, channel="latest-esr"), {}, [])
        self.assertEqual(result["mattermost"]["version"], "11.7.14")
        self.assertEqual(result["mattermost"]["source"], "bundle")
        self.assertTrue(result["mattermost"]["sig_url"].endswith(".sig"))

    def test_hand_filled_directory_is_scanned(self):
        digest = write(os.path.join(self.dir, "mattermost-11.7.14-linux-amd64.tar.gz"))
        write(os.path.join(self.dir, "mattermost-11.7.14-linux-amd64.tar.gz.sig"))
        write(os.path.join(self.dir, "prometheus-3.15.1.linux-amd64.tar.gz"))
        with open(os.path.join(self.dir, "SHA256SUMS"), "w", encoding="utf-8") as handle:
            handle.write("%s  mattermost-11.7.14-linux-amd64.tar.gz\n" % digest)
        warnings = []
        result = rv.resolve_offline(self.dir, ["mattermost", "prometheus"], "amd64", MM,
                                    {"prometheus": "3.15.1"}, warnings)
        self.assertEqual(result["mattermost"]["checksum"], "sha256:" + digest)
        self.assertEqual(result["mattermost"]["source"], "directory")
        self.assertEqual(result["prometheus"]["checksum"], "")
        self.assertTrue(any("scanned" in w for w in warnings))

    def test_log_shippers_are_scanned_with_their_checksum_files(self):
        loki = write(os.path.join(self.dir, "loki-linux-amd64.zip"))
        write(os.path.join(self.dir, "loki-canary-linux-amd64.zip"))
        otel = write(os.path.join(self.dir, "otelcol-contrib_0.165.0_linux_amd64.tar.gz"))
        with open(os.path.join(self.dir, "SHA256SUMS"), "w", encoding="utf-8") as handle:
            handle.write("%s  loki-linux-amd64.zip\n" % loki)
        # otelcol-contrib publishes one file per asset holding only the digest.
        with open(os.path.join(self.dir, "otelcol-contrib_0.165.0_linux_amd64.tar.gz.sha256"), "w",
                  encoding="utf-8") as handle:
            handle.write(otel + "\n")
        result = rv.resolve_offline(self.dir, ["loki", "otel_collector"], "amd64", MM,
                                    {"otel_collector": "0.165.0"}, [])
        self.assertEqual(result["loki"]["filename"], "loki-linux-amd64.zip")
        self.assertEqual(result["loki"]["checksum"], "sha256:" + loki)
        self.assertEqual(result["otel_collector"]["version"], "0.165.0")
        self.assertEqual(result["otel_collector"]["checksum"], "sha256:" + otel)

    def test_wrong_pinned_version_is_an_error(self):
        write(os.path.join(self.dir, "prometheus-3.15.1.linux-amd64.tar.gz"))
        with self.assertRaises(rv.ResolveError):
            rv.resolve_offline(self.dir, ["prometheus"], "amd64", MM, {"prometheus": "2.55.0"}, [])

    def test_other_architecture_is_ignored(self):
        write(os.path.join(self.dir, "node_exporter-1.12.1.linux-arm64.tar.gz"))
        with self.assertRaises(rv.ResolveError):
            rv.resolve_offline(self.dir, ["node_exporter"], "amd64", MM, {}, [])

    def test_missing_source_is_an_error(self):
        with self.assertRaises(rv.ResolveError):
            rv.resolve_offline(os.path.join(self.dir, "nope"), ["mattermost"], "amd64", MM, {}, [])


class Inventory(unittest.TestCase):
    def test_lists_a_hand_filled_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "mattermost-11.7.14-linux-amd64.tar.gz"))
            write(os.path.join(tmp, "mattermost-11.7.14-linux-amd64.tar.gz.sig"))
            write(os.path.join(tmp, "rtcd-linux-arm64"))
            write(os.path.join(tmp, "collections", "ansible-posix-1.6.2.tar.gz"))
            inv = rv.inventory(tmp, "amd64")
        self.assertEqual(inv["kind"], "folder")
        self.assertEqual([a["name"] for a in inv["artifacts"]], ["mattermost"])
        self.assertTrue(inv["artifacts"][0]["signature"])
        self.assertTrue(inv["extras"]["collections"])
        self.assertFalse(inv["extras"]["rpms"])

    def test_missing_folder(self):
        with self.assertRaises(rv.ResolveError):
            rv.inventory("/nonexistent/mmi", "amd64")


class Helpers(unittest.TestCase):
    def test_needed_artifacts(self):
        self.assertEqual(rv.needed_artifacts(["postgresql", "nginx"]), [])
        self.assertEqual(rv.needed_artifacts(["opensearch"]), ["opensearch", "opensearch_icu"])

    def test_sums_lookup_reads_both_formats(self):
        digest = "ab" * 32
        self.assertEqual(rv.sums_lookup("%s  loki-linux-amd64.zip\n" % digest, "loki-linux-amd64.zip"), digest)
        self.assertEqual(rv.sums_lookup(digest.upper() + "\n", "anything"), digest)
        self.assertEqual(rv.sums_lookup("%s  other.zip\n" % digest, "loki-linux-amd64.zip"), "")

    def test_version_key_orders_numerically(self):
        self.assertGreater(rv.version_key("11.10.4"), rv.version_key("11.9.2"))


if __name__ == "__main__":
    unittest.main()
