#!/usr/bin/env python3
"""Turn one architecture-wizard "estate plan" into the answers.yml for one server.

An estate plan (see estate.example.yml) describes a whole Mattermost deployment:
every server, its role, and the settings shared across all of them. install.sh's
--estate FILE works out which node in the plan IS this server, then uses this
module to derive that node's normal answers: profiles, and the cross-references
(mattermost_db_host, *_allowed_clients, monitoring_estate, ...) that README.md's
"Example: a split estate" today has you copy between files by hand.

Settings that point at a file on disk (the licence, TLS cert/key, the OpenSearch
CA) are never derived here: they have to be put on the server by hand, and
install.sh --edit asks for them afterwards.

Passwords are not in the plan either, because a plan is made in a browser and
passed around. They can come from an estate secrets file instead: one
ansible-vault encrypted file for the whole estate, made once with new-secrets
and carried to each server, so the database password doesn't have to be read off
one server and typed into another. Without it, nothing changes: each server
generates its own and the operator copies them, exactly as before.

Needs PyYAML, same as install.sh's own load_previous()/carry_over_answers() -
by the time install.sh calls this (after install_prereqs), ansible-core has
already brought it in.

Subcommands:
  apply        decode a plan, pick a node and print that node's answers as YAML
  checks       print the preflight checks this node should pass, one per line
  new-secrets  generate an estate secrets file and encrypt it with ansible-vault
  list-nodes   print every node name in a plan, one per line
  decode       debug helper: print a plan's decoded YAML
  encode       debug helper: print a plain YAML file as an MME1: encoded blob
"""

import argparse
import base64
import gzip
import ipaddress
import os
import secrets
import string
import subprocess
import sys
import tempfile

import yaml

ENCODING_PREFIX = "MME1:"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOGUE_PATH = os.path.join(REPO_ROOT, "inventory", "group_vars", "all", "catalogue.yml")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "inventory", "group_vars", "all", "defaults.yml")

# Keep in step with inventory/group_vars/all/profiles.yml's mmd_profiles.
# tests/test_estate_plan.py fails if they drift.
PROFILE_COMPONENTS = {
    "core": ["postgresql", "mattermost", "nginx"],
    "app": ["mattermost", "nginx"],
    "database": ["postgresql"],
    "calls": ["rtcd"],
    "search": ["opensearch"],
    "storage": ["nfs"],
    "monitoring": ["prometheus", "loki", "grafana"],
    "metrics": ["prometheus", "loki"],
    "dashboards": ["grafana"],
    "all-in-one": ["postgresql", "mattermost", "nginx", "rtcd", "opensearch", "prometheus", "loki", "grafana"],
}


# Exporters and the log collector are installed next to what they watch, so a
# preflight and a sizing check have to count them too. Mirrors README's
# "Exporters are added automatically next to whatever they watch".
EXPORTER_ALONGSIDE = {
    "postgresql": "postgres_exporter",
    "nginx": "nginx_exporter",
    "opensearch": "opensearch_exporter",
}
EVERY_MONITORED_SERVER = ["node_exporter", "otel_collector"]

# Which defaults.yml setting holds each component's main port. postgresql_port is
# itself "{{ mattermost_db_port }}", so that's the one worth reading.
PORT_SETTING = {
    "postgresql": "mattermost_db_port",
    "opensearch": "opensearch_port",
    "rtcd": "rtcd_api_port",
    "nfs": "nfs_port",
    "loki": "loki_port",
    "prometheus": "prometheus_port",
    "node_exporter": "node_exporter_port",
}

# An estate secrets file holds one of each, generated once for the whole estate.
# Names and lengths match roles/common/tasks/secrets.yml, so a server given one
# of these stores exactly what it would have generated for itself.
SECRET_LENGTHS = {
    "db_password": 32,
    "opensearch_admin_password": 32,
    "opensearch_mattermost_password": 32,
    "grafana_admin_password": 24,
    "replication_password": 32,
}


class EstatePlanError(Exception):
    pass


class NoMatchingNode(EstatePlanError):
    def __init__(self, names):
        self.names = names
        super().__init__("No node in the plan matches this server.")


class AmbiguousNode(EstatePlanError):
    def __init__(self, names):
        self.names = names
        super().__init__("More than one node in the plan matches this server.")


# ------------------------------------------------------------------ decode/encode

def decode(text):
    """A plan as MME1:<base64 gzip> or plain YAML, either way -> a dict."""
    text = text.strip()
    if text.startswith(ENCODING_PREFIX):
        try:
            raw = gzip.decompress(base64.b64decode(text[len(ENCODING_PREFIX):]))
        except Exception as exc:  # noqa: BLE001 - any of these mean a bad paste
            raise EstatePlanError("That doesn't look like an estate plan: %s" % exc) from exc
        text = raw.decode("utf-8")
    try:
        plan = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EstatePlanError("Could not read the estate plan as YAML: %s" % exc) from exc
    problems = plan_problems(plan)
    if problems:
        raise EstatePlanError(" ".join(problems))
    return plan


def encode(text):
    plan = yaml.safe_load(text) or {}
    problems = plan_problems(plan)
    if problems:
        raise EstatePlanError(" ".join(problems))
    return ENCODING_PREFIX + base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii")


def plan_problems(plan):
    if not isinstance(plan, dict):
        return ["An estate plan should be a mapping of settings, not a list."]
    problems = []
    if plan.get("estate_version") != 1:
        problems.append("estate_version should be 1.")
    nodes = plan.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        problems.append("nodes should be a non-empty list of servers.")
        return problems
    seen_names = set()
    addresses = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name") or not node.get("address"):
            problems.append("Every node needs a name and an address.")
            continue
        if node["name"].lower() in seen_names:
            problems.append("Two nodes are both called %s." % node["name"])
        seen_names.add(node["name"].lower())
        addresses.setdefault(str(node["address"]), []).append(node["name"])
        for profile in node.get("profiles") or []:
            if profile not in PROFILE_COMPONENTS:
                problems.append("%s: %s isn't a profile." % (node["name"], profile))
    # Two nodes on one address means one server, and then nothing can work out
    # which node it is meant to be.
    for address, names in addresses.items():
        if len(names) > 1:
            problems.append("%s share the address %s. Put the roles on one node instead."
                            % (" and ".join(names), address))

    # A database node with a `primary` is a read replica of it. The name has to
    # exist, and something has to be the primary, or Mattermost has nowhere to write.
    db_nodes = [n for n in nodes if isinstance(n, dict) and "postgresql" in expand_components(n.get("profiles"))]
    for node in db_nodes:
        target = node.get("primary")
        if target and target.lower() not in seen_names:
            problems.append("%s follows %s, which isn't a node in this plan." % (node["name"], target))
    if db_nodes and all(n.get("primary") for n in db_nodes):
        problems.append("Every database node is a read replica. One of them has to be the primary.")
    return problems


# ------------------------------------------------------------------ components

def expand_components(profiles):
    out = []
    for profile in profiles or []:
        for component in PROFILE_COMPONENTS.get(profile, []):
            if component not in out:
                out.append(component)
    return out


def nodes_with(nodes, component):
    return [n for n in nodes if component in expand_components(n.get("profiles"))]


# Everything that actually gets installed on a node, including the exporters and
# the log collector the toolkit adds next to what they watch.
def installed_components(plan, node):
    out = expand_components(node.get("profiles"))
    scraped = plan.get("observability") == "managed" or bool(plan.get("monitoring_scrape_sources"))
    ships_logs = plan.get("observability") == "managed" or bool(plan.get("logging_loki_url"))
    if scraped:
        for component in list(out):
            extra = EXPORTER_ALONGSIDE.get(component)
            if extra and extra not in out:
                out.append(extra)
        if "node_exporter" not in out:
            out.append("node_exporter")
    if ships_logs and "otel_collector" not in out:
        out.append("otel_collector")
    return out


# ------------------------------------------------------------------ repo data

_YAML_CACHE = {}


def _repo_yaml(path):
    if path not in _YAML_CACHE:
        try:
            with open(path, encoding="utf-8") as handle:
                _YAML_CACHE[path] = yaml.safe_load(handle)
        except OSError as exc:
            raise EstatePlanError("Could not read %s: %s" % (path, exc)) from exc
    return _YAML_CACHE[path]


def catalogue():
    """What each component needs, straight from the file the report reads."""
    return _repo_yaml(CATALOGUE_PATH)["mmd_catalogue"]


def settings():
    """group_vars defaults. Ports are plain numbers there, templated strings aren't read."""
    return _repo_yaml(DEFAULTS_PATH)


def node_requirements(plan, node):
    ram = cpus = 0
    for component in installed_components(plan, node):
        entry = catalogue().get(component) or {}
        ram += int(entry.get("min_ram_mb") or 0)
        cpus += int(entry.get("min_cpus") or 0)
    return ram, max(1, cpus)


# ------------------------------------------------------------------ preflight

def _host_port(url, fallback):
    rest = url.split("://", 1)[-1].split("/", 1)[0]
    host, sep, port = rest.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return rest, fallback


def checks_for_node(plan, node_name):
    """What this server has to be able to do before an install can work.

    Only outbound checks: what this server needs to reach. A port nothing
    listens on yet still tells you something useful, because a refused
    connection means the route and the firewall are fine and the service
    simply isn't installed, while a timeout means something is dropping it.
    """
    nodes = plan["nodes"]
    by_name = {n["name"]: n for n in nodes}
    if node_name not in by_name:
        raise EstatePlanError("%s isn't a node in this plan." % node_name)
    node = by_name[node_name]
    components = expand_components(node.get("profiles"))
    conf = settings()
    checks = []

    def port_of(component):
        return int(conf[PORT_SETTING[component]])

    def peers(component, why):
        for other in nodes_with(nodes, component):
            if other["name"] == node_name:
                continue  # local, so nothing crosses the network
            checks.append({"kind": "tcp", "target": other["address"], "port": port_of(component),
                           "label": "%s on %s, for %s" % (component, other["name"], why)})

    for other in nodes:
        if other["name"] != node_name:
            checks.append({"kind": "resolve", "target": other["address"], "port": "",
                           "label": "%s is resolvable" % other["name"]})

    if "mattermost" in components:
        peers("postgresql", "the database")
        peers("opensearch", "search")
        peers("rtcd", "Calls")
        peers("nfs", "file storage")
    if "postgresql" in components and node.get("primary"):
        primary = by_name.get(node["primary"])
        if primary:
            checks.append({"kind": "tcp", "target": primary["address"], "port": port_of("postgresql"),
                           "label": "primary %s, for replication" % primary["name"]})
    if "prometheus" in components:
        for other in nodes:
            if other["name"] != node_name:
                checks.append({"kind": "tcp", "target": other["address"], "port": port_of("node_exporter"),
                               "label": "node_exporter on %s, to scrape it" % other["name"]})
    if "grafana" in components and "prometheus" not in components:
        peers("prometheus", "the Prometheus it queries")
    if "loki" not in components:
        if plan.get("observability") == "managed":
            peers("loki", "shipping logs")
        elif plan.get("logging_loki_url"):
            host, port = _host_port(plan["logging_loki_url"], port_of("loki"))
            checks.append({"kind": "tcp", "target": host, "port": port, "label": "your Loki, for shipping logs"})

    ram, cpus = node_requirements(plan, node)
    runs = ", ".join(installed_components(plan, node))
    checks.append({"kind": "ram", "target": ram, "port": "", "label": runs})
    checks.append({"kind": "cpus", "target": cpus, "port": "", "label": runs})
    return checks


# ------------------------------------------------------------------ estate secrets

def secret_targets(plan, node_name):
    """Which answers this node fills from the estate secrets file, and from which key.

    This is the same list install.sh otherwise prints as "copy this from that
    server's secrets.yml", so a node is given exactly what it would have been
    handed by hand, and nothing else in the estate.
    """
    nodes = plan["nodes"]
    node = {n["name"]: n for n in nodes}[node_name]
    components = expand_components(node.get("profiles"))
    has_search = bool(nodes_with(nodes, "opensearch"))
    replicating = bool(node.get("primary")) or any(n.get("primary") == node_name for n in nodes)

    out = {}
    if "postgresql" in components or "mattermost" in components:
        out["mattermost_db_password"] = "db_password"
    if "postgresql" in components and replicating:
        out["postgresql_replication_password"] = "replication_password"
    if "opensearch" in components:
        out["mattermost_opensearch_password"] = "opensearch_mattermost_password"
        out["opensearch_admin_password"] = "opensearch_admin_password"
    elif "mattermost" in components and has_search:
        out["mattermost_opensearch_password"] = "opensearch_mattermost_password"
    if "grafana" in components:
        out["grafana_admin_password"] = "grafana_admin_password"
    return out


def generate_secrets():
    alphabet = string.ascii_letters + string.digits
    return {name: "".join(secrets.choice(alphabet) for _ in range(length))
            for name, length in SECRET_LENGTHS.items()}


def write_secrets_file(path, password_file=None):
    """Generate one set of estate secrets and leave them ansible-vault encrypted."""
    if os.path.exists(path):
        raise EstatePlanError("%s already exists. Estate secrets are generated once; "
                              "delete it by hand if you really want new ones." % path)
    folder = os.path.dirname(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, dir=folder)
    try:
        os.chmod(handle.name, 0o600)
        handle.write("# Estate secrets for Mattermost Parthenon, generated once for the whole\n"
                     "# estate. install.sh --estate-secrets gives each server only the ones it\n"
                     "# needs. Keep this wherever you keep your other vault files.\n")
        handle.write(yaml.safe_dump(generate_secrets(), default_flow_style=False, sort_keys=True))
        handle.close()
        command = ["ansible-vault", "encrypt", handle.name, "--output", path]
        if password_file:
            command += ["--vault-password-file", password_file]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise EstatePlanError("ansible-vault could not encrypt the secrets: %s"
                                  % (result.stderr.strip() or result.stdout.strip()))
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
    os.chmod(path, 0o600)
    return sorted(SECRET_LENGTHS)


def read_secrets_file(path, password_file=None):
    command = ["ansible-vault", "decrypt", path, "--output", "-"]
    if password_file:
        command += ["--vault-password-file", password_file]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise EstatePlanError("Could not open the estate secrets file: %s"
                              % (result.stderr.strip() or result.stdout.strip()))
    data = yaml.safe_load(result.stdout) or {}
    if not isinstance(data, dict):
        raise EstatePlanError("The estate secrets file should be a mapping of 'name: value' lines.")
    return data


# ------------------------------------------------------------------ node selection

def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _resolve(address):
    """IPs getent knows an address as, or an empty set if it can't be looked up."""
    if _is_ip(address):
        return set()
    try:
        out = subprocess.run(["getent", "ahosts", address], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split()[0] for line in out.stdout.splitlines() if line.strip()}


def select_node(plan, node_name=None, local_names=None, local_addresses=None):
    nodes = plan["nodes"]
    by_name = {n["name"]: n for n in nodes}
    if node_name:
        if node_name not in by_name:
            raise EstatePlanError("%s isn't a node in this plan." % node_name)
        return by_name[node_name]

    local_names = {n.lower() for n in (local_names or []) if n}
    local_addresses = set(local_addresses or [])
    matches = []
    for node in nodes:
        identifiers = {node["name"].lower(), node["address"]} | _resolve(node["address"])
        if (identifiers & local_names) or ({node["address"]} | _resolve(node["address"])) & local_addresses:
            matches.append(node)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NoMatchingNode([n["name"] for n in nodes])
    raise AmbiguousNode([n["name"] for n in matches])


# ------------------------------------------------------------------ derivation

def derive_answers(plan, node_name, estate_secrets=None):
    """-> (answers: dict, followups: list[str]).

    followups are things this node still needs that the plan deliberately never
    carries: local files (a licence, a certificate, a CA), and passwords when
    they aren't coming from an estate secrets file. install.sh prints these
    after writing the derived answers, and install.sh --edit is where they go in.

    estate_secrets, when given, is the decrypted estate secrets file. Only the
    entries this node needs are taken from it, and the followups that would have
    asked for them by hand are dropped.
    """
    nodes = plan["nodes"]
    by_name = {n["name"]: n for n in nodes}
    if node_name not in by_name:
        raise EstatePlanError("%s isn't a node in this plan." % node_name)
    node = by_name[node_name]
    components = expand_components(node.get("profiles"))
    has = lambda c: c in components  # noqa: E731

    app_nodes = nodes_with(nodes, "mattermost")
    db_nodes = nodes_with(nodes, "postgresql")
    # A database node that names another in `primary` is a read replica of it.
    # Mattermost's own connection has to go to a primary, so never take the first
    # database node in the file and hope: the plan may list the replica first.
    db_primaries = [n for n in db_nodes if not n.get("primary")]
    db_primary = db_primaries[0] if db_primaries else None
    calls_nodes = nodes_with(nodes, "rtcd")
    search_nodes = nodes_with(nodes, "opensearch")
    storage_nodes = nodes_with(nodes, "nfs")
    monitoring_nodes = nodes_with(nodes, "prometheus")

    supplied = {}
    if estate_secrets:
        for answers_key, secret_key in secret_targets(plan, node_name).items():
            value = estate_secrets.get(secret_key)
            if value:
                supplied[answers_key] = value

    out = {
        "deploy_name": plan["name"],
        "deploy_profiles": list(node.get("profiles") or []),
    }
    followups = []

    if has("mattermost"):
        out["mattermost_site_url"] = plan["mattermost_site_url"]
        out["mattermost_edition"] = plan["mattermost_edition"]
        if plan.get("file_storage") == "amazons3":
            out["mattermost_file_driver"] = "amazons3"
            followups.append("Fill in the S3 bucket, region and keys (mattermost_s3_*). They aren't in the plan.")
        if db_primary:
            out["mattermost_db_host"] = db_primary["address"]
            if "mattermost_db_password" not in supplied:
                followups.append("Copy the database password from %s's secrets.yml into mattermost_db_password "
                                  "here, or use an estate secrets file." % db_primary["name"])
            # Read replicas of that primary. Mattermost sends reads to them through
            # SqlSettings.DataSourceReplicas, which is an Enterprise feature: without
            # the licence the replica still streams, Mattermost just never reads it.
            read_replicas = [n for n in db_nodes if n.get("primary") == db_primary["name"]]
            if read_replicas:
                out["mattermost_db_replica_hosts"] = [n["address"] for n in read_replicas]
                if plan.get("licence") not in (None, "", "enterprise"):
                    followups.append("This plan has %d database read replica(s), and Mattermost only reads from "
                                      "them on Enterprise. On this licence they stream but carry no traffic."
                                      % len(read_replicas))
        elif db_nodes:
            raise EstatePlanError("Every database node in this plan is a read replica. One of them has to be "
                                  "the primary: take the `primary` line off it.")
        # Calls either runs inside this server (up to 50 people, Mattermost's own
        # figure) or on rtcd servers of its own. An rtcd URL wins either way.
        if calls_nodes:
            out["mattermost_rtcd_url"] = "http://%s:8045" % calls_nodes[0]["address"]
            if len(calls_nodes) > 1:
                followups.append("This plan has %d Calls servers. Put them behind a load balancer and "
                                  "point mattermost_rtcd_url at it, or Mattermost only uses %s."
                                  % (len(calls_nodes), calls_nodes[0]["name"]))
        elif plan.get("calls") == "none":
            out["mattermost_calls_integrated"] = False
        elif plan.get("calls") == "integrated":
            followups.append("Calls runs inside this server, so UDP and TCP 8443 have to reach it "
                              "directly rather than through the proxy. Past about 50 people in calls, "
                              "move it to an rtcd server of its own.")
        if search_nodes:
            out["mattermost_opensearch_url"] = "https://%s:9200" % search_nodes[0]["address"]
            if "mattermost_opensearch_password" not in supplied:
                followups.append("Copy the password for the OpenSearch 'mattermost' user from %s's secrets.yml "
                                  "into mattermost_opensearch_password here." % search_nodes[0]["name"])
            followups.append("Copy /etc/opensearch/certs/root-ca.pem from %s to this server, then point "
                              "mattermost_opensearch_ca_file at it." % search_nodes[0]["name"])
        if plan.get("file_storage") == "nfs" and storage_nodes:
            followups.append("Mount %s's NFS share before installing Mattermost here. Its report has the "
                              "exact commands." % storage_nodes[0]["name"])

    if has("nginx"):
        out["tls_mode"] = plan["tls_mode"]
        if plan["tls_mode"] == "provided":
            followups.append("Put the certificate and key (or a chain directory) on this server, then run "
                              "install.sh --edit to add their paths.")

    if has("postgresql"):
        if app_nodes:
            out["postgresql_allowed_clients"] = [n["address"] for n in app_nodes]
        replicas = [n for n in db_nodes if n.get("primary") == node["name"]]
        if replicas:
            out["postgresql_replication"] = "primary"
            out["postgresql_replica_hosts"] = [n["address"] for n in replicas]
        elif node.get("primary"):
            primary = by_name.get(node["primary"])
            if primary is None:
                raise EstatePlanError("%s's primary (%s) isn't a node in this plan." % (node_name, node["primary"]))
            out["postgresql_replication"] = "replica"
            out["postgresql_primary_host"] = primary["address"]
            if app_nodes:
                out["postgresql_allowed_clients"] = [n["address"] for n in app_nodes]
            if "postgresql_replication_password" not in supplied:
                followups.append("Copy the db_password and replication_password from %s's secrets.yml into "
                                  "mattermost_db_password and postgresql_replication_password here."
                                  % primary["name"])

    if has("rtcd"):
        if app_nodes:
            out["rtcd_api_sources"] = [n["address"] for n in app_nodes]
        followups.append("If clients reach this server through NAT or a different address, set "
                          "rtcd_ice_host_override to the address they use. Calls connect without it, "
                          "but carry no audio.")

    if has("opensearch") and app_nodes:
        out["opensearch_allowed_clients"] = [n["address"] for n in app_nodes]

    if has("nfs") and app_nodes:
        out["nfs_allowed_clients"] = [n["address"] for n in app_nodes]
        if node.get("squash"):
            out["nfs_squash"] = node["squash"]

    # Prometheus, Loki and Grafana may be on one box (the monitoring profile) or
    # split across two (metrics and dashboards), so each is wired on its own.
    loki_nodes = nodes_with(nodes, "loki")

    if has("prometheus") or has("grafana"):
        # Grafana builds its alerts and dashboards from the same estate list.
        out["monitoring_site_url"] = plan["mattermost_site_url"]
        out["monitoring_estate"] = [
            {"name": n["name"], "address": n["address"], "components": expand_components(n.get("profiles"))}
            for n in nodes if n["name"] != node["name"]
        ]
    if has("grafana") and not has("prometheus") and monitoring_nodes:
        out["grafana_prometheus_url"] = "http://%s:9090" % monitoring_nodes[0]["address"]

    if not has("prometheus"):
        if plan.get("observability") == "managed" and monitoring_nodes:
            out["deploy_monitoring_exporters"] = True
            out["monitoring_scrape_sources"] = [n["address"] for n in monitoring_nodes]
        elif plan.get("observability") == "external" and plan.get("monitoring_scrape_sources"):
            out["deploy_monitoring_exporters"] = True
            out["monitoring_scrape_sources"] = plan["monitoring_scrape_sources"]

    if not has("loki"):
        if plan.get("observability") == "managed" and loki_nodes:
            out["logging_loki_url"] = "http://%s:3100" % loki_nodes[0]["address"]
        elif plan.get("observability") == "external" and plan.get("logging_loki_url"):
            out["logging_loki_url"] = plan["logging_loki_url"]

    # Last, so a password from the estate file always wins over anything above.
    out.update(supplied)
    return out, followups


# ------------------------------------------------------------------ cli

def read_text(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_apply = sub.add_parser("apply", help="pick a node and print its answers as YAML")
    p_apply.add_argument("--plan-file", required=True)
    p_apply.add_argument("--node", default="", help="skip matching and use this node by name")
    p_apply.add_argument("--local-name", action="append", default=[])
    p_apply.add_argument("--local-address", action="append", default=[])
    p_apply.add_argument("--secrets", default="", help="estate secrets file to take this node's passwords from")
    p_apply.add_argument("--vault-password-file", default="")

    p_checks = sub.add_parser("checks", help="print the preflight checks this node should pass")
    p_checks.add_argument("--plan-file", required=True)
    p_checks.add_argument("--node", default="")
    p_checks.add_argument("--local-name", action="append", default=[])
    p_checks.add_argument("--local-address", action="append", default=[])

    p_secrets = sub.add_parser("new-secrets", help="generate an estate secrets file, ansible-vault encrypted")
    p_secrets.add_argument("--out", required=True)
    p_secrets.add_argument("--vault-password-file", default="")

    p_list = sub.add_parser("list-nodes", help="print every node name, one per line")
    p_list.add_argument("--plan-file", required=True)

    p_decode = sub.add_parser("decode", help="debug helper: print a plan's decoded YAML")
    p_decode.add_argument("--plan-file", required=True)

    p_encode = sub.add_parser("encode", help="debug helper: print a plain YAML file as an MME1: blob")
    p_encode.add_argument("file")

    args = parser.parse_args(argv)
    try:
        if args.command == "encode":
            print(encode(read_text(args.file)))
            return 0
        if args.command == "new-secrets":
            names = write_secrets_file(args.out, args.vault_password_file or None)
            print("Wrote %s, encrypted with ansible-vault: %s" % (args.out, ", ".join(names)), file=sys.stderr)
            return 0

        plan = decode(read_text(args.plan_file))

        if args.command == "decode":
            print(yaml.safe_dump(plan, sort_keys=False, default_flow_style=False, allow_unicode=True), end="")
            return 0
        if args.command == "list-nodes":
            for node in plan["nodes"]:
                print(node["name"])
            return 0
        if args.command == "checks":
            node = select_node(plan, args.node, args.local_name, args.local_address)
            # kind \t target \t port \t label, for install.sh to run one per line.
            for check in checks_for_node(plan, node["name"]):
                print("%s\t%s\t%s\t%s" % (check["kind"], check["target"], check["port"], check["label"]))
            return 0
        if args.command == "apply":
            node = select_node(plan, args.node, args.local_name, args.local_address)
            estate_secrets = None
            if args.secrets:
                estate_secrets = read_secrets_file(args.secrets, args.vault_password_file or None)
            answers, followups = derive_answers(plan, node["name"], estate_secrets)
            print("# Derived from %s for node %s" % (args.plan_file, node["name"]))
            print(yaml.safe_dump(answers, sort_keys=False, default_flow_style=False, allow_unicode=True), end="")
            for line in followups:
                print("# FOLLOWUP: %s" % line, file=sys.stderr)
            return 0
    except NoMatchingNode as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        for name in exc.names:
            print(name, file=sys.stderr)
        return 3
    except AmbiguousNode as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        for name in exc.names:
            print(name, file=sys.stderr)
        return 4
    except EstatePlanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
