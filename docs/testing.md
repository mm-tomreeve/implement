# Testing

## Post-deploy tests

These run on the server at the end of every install and with `./install.sh --verify-only`.
They live in each role's `tasks/verify.yml`. A check is critical (a failure fails the run) or
a warning (reported, but the run still passes). Checks that don't apply to this server show as
n/a in the report.

| Component | Check | Severity |
|---|---|---|
| host | SELinux is enforcing | warning |
| host | firewalld is running (when we manage it) | warning |
| host | Clock is synchronised (chrony) | warning |
| host | Data filesystems below 85% full | warning |
| host | Every downloaded artifact passed checksum or signature checks | critical |
| postgresql | Service is active | critical |
| postgresql | Listening on its port | critical |
| postgresql | The Mattermost user can log in over TCP and run a query | critical |
| postgresql | Passwords stored as SCRAM-SHA-256 | warning |
| postgresql | TLS on when remote clients are allowed | warning |
| postgresql | max_connections covers every app server's pool (`mattermost_db_max_open_conns` each) | warning |
| postgresql | On a replica: in recovery, so read-only and following the primary | critical |
| postgresql | On a replica: the WAL receiver is streaming from the primary | warning |
| postgresql | On a primary: every replica has a slot and is connected (lists the WAL each slot holds) | warning |
| postgresql | On a primary: `max_slot_wal_keep_size` caps the WAL a replica can hold | warning |
| mattermost | Service is active | critical |
| mattermost | `/api/v4/system/ping` returns OK | critical |
| mattermost | Database and file store report OK in the ping | critical |
| mattermost | Running version matches the verified artifact | critical |
| mattermost | SiteURL is what the answer file asked for | critical |
| mattermost | Data directory writable by the service user | critical |
| mattermost | Calls plugin points at rtcd, and rtcd answers | critical |
| mattermost | OpenSearch answers from the app server | critical |
| mattermost | mmctl local mode works | warning |
| mattermost | No errors in the last 500 log lines | warning |
| mattermost | Metrics endpoint answers (needs a licence) | info |
| nginx | `nginx -t` passes and the service is active | critical |
| nginx | HTTP redirects to HTTPS, or serves Mattermost when TLS is pending | critical |
| nginx | Mattermost answers over HTTPS | critical |
| nginx | Certificate valid for 30 more days and matches the server name | warning |
| nginx | Websocket upgrade returns 101 through the proxy | warning |
| rtcd | Service active, API answers, UDP media port bound | critical |
| rtcd | ICE host override set | warning |
| opensearch | Service active, cluster green or yellow | critical |
| opensearch | Requests without credentials refused (security on) | critical |
| opensearch | analysis-icu plugin installed | warning |
| prometheus | Service active and ready | critical |
| prometheus | All scrape targets up | warning |
| grafana | Service active and health OK | critical |
| grafana | Datasource answers, Mattermost dashboards loaded | warning |
| node_exporter, postgres_exporter | Metrics served (`pg_up 1` for PostgreSQL) | warning |

Results go to `/var/lib/mattermost-implement/results/<run>.json` (`latest.json` points at the newest)
and into the report. The report is written before the run fails, so a failed run still leaves
you a document that says what went wrong.

## Repo tests

```bash
make venv     # once: a local virtualenv with ansible-core, ansible-lint, yamllint, shellcheck
make lint     # yamllint, ansible-lint (production profile), shellcheck, playbook syntax check
make test     # unit tests for the resolver, and consistency checks across the repo
make render   # render the plan logic and the report for eight server layouts
make smoke    # the core profile in an AlmaLinux 9 systemd container
make e2e      # five containers, one per role, wired together, plus a functional test
make e2e-replica  # a PostgreSQL primary and a read replica in two containers
```

The unit tests run against a saved copy of Mattermost's version archive page
(`tests/fixtures/version-archive.html`), so they need no network access. If Mattermost changes
the page layout, refresh the fixture and fix `parse_archive` in `tools/resolve_versions.py`.

The consistency tests catch the drift that is easy to miss by eye: the wrapper's profile list
against `profiles.yml`, the catalogue against the roles on disk, the resolver against the
artifacts role, the pinned GPG fingerprints against the resolver, and the example answer file
against the defaults.

`make render` runs the real plan tasks and report role against made-up facts and results for
the layouts in `tests/render/scenarios/` (core, all-in-one, split app server, database, monitoring,
and a failed install). It needs no root and no RHEL, and it catches most template and wiring
mistakes. The output in `tests/render/out/` doubles as sample documents to show a customer.

The smoke test needs podman or docker with privileged containers and internet access. It runs
`install.sh` non-interactively with `tests/smoke/answers-core.yml`, which turns off firewalld
and SELinux handling because neither works inside a container. Then it runs `--verify-only`
and copies the report to `tests/smoke/out/`. It is a good check that the roles converge. It is
no substitute for a run on a real RHEL 9 and RHEL 10 VM, online and offline, before a release.

## End-to-end test

`make e2e` (or `tests/e2e/run.sh`) builds a small estate in containers on one network:

| Server | Profile | Address |
|---|---|---|
| db01 | database (+ exporters) | 172.30.10.11 |
| app01 | app, with TLS from a throwaway CA | 172.30.10.12 |
| calls01 | calls | 172.30.10.13 |
| search01 | search, security plugin on | 172.30.10.14 |
| mon01 | monitoring, scraping the others | 172.30.10.15 |

Each server runs `install.sh` with `tests/e2e/answers/common.yml` plus its own file. Database,
calls and search go first in parallel, then the harness copies the search CA to app01, then app01
installs, then monitoring. After that `tests/e2e/functional.sh` runs on app01. It logs in through
Nginx over HTTPS, posts and reads back a message stored on db01, uploads a file, searches, opens
an authenticated websocket and checks Calls points at calls01. Finally every server re-runs its
own checks, and the reports and results land in `tests/e2e/out/`.

`IMAGE=docker.io/almalinux/10-init` tests RHEL 10 equivalents. `KEEP=true` leaves the containers
running so you can `docker exec` in and look around.

mon01 is the monitoring server. Its answer file lists the other four, so Prometheus, the alert
rules and the dashboards cover the whole estate. After the functional test, the harness runs
`tests/e2e/observability_check.py` on mon01. It runs every alert rule's query against the live
Prometheus and fails if a rule returns no data, because a rule with a wrong metric name would
never fire. Rules that only have data once there is traffic (push errors, call packet loss)
show as n/a. It also lists any alerts firing in Grafana.

With `KEEP=true`, mon01's Grafana is published on http://localhost:13000.

Set `GITHUB_TOKEN` in your shell before running it repeatedly. rtcd's "latest" lookup uses
GitHub's API, which allows 60 anonymous calls an hour.

Two things the containers can't show. SELinux is off inside them, so the SELinux check always
warns there. And on Apple silicon the containers are arm64. Mattermost's arm64 tarballs ship no
prepackaged plugins, so the Calls plugin check shows n/a; on amd64 it must pass.

## Replication test

`make e2e-replica` (or `tests/e2e/replica.sh`) installs a PostgreSQL primary (pg01) and a read
replica (pg02) in two containers, each with `install.sh` and the database profile. It then checks:

- pg02 is in recovery and refuses writes, and pg01 sees it streaming,
- a row written on pg01 shows up on pg02, and mmuser can log in on pg02,
- pg02 made its own TLS key rather than keeping the primary's,
- `max_slot_wal_keep_size` is set on pg01,
- the exporters publish the series the replication alerts read (`pg_replication_slots_*` and
  `pg_settings_max_slot_wal_keep_size_bytes` on pg01, `pg_replication_is_replica` on pg02).

Then it runs both installs again, and finally rebuilds pg02 from scratch (stop it, move the data
directory aside, run `install.sh`) and checks it streams again. Logs land in `tests/e2e/out/replica/`.
It takes the same `IMAGE` and `KEEP` settings as the main end-to-end test.
