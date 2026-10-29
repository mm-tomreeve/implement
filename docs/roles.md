# Roles, profiles and wiring

## How a run decides what to do

`roles/common/tasks/plan.yml` runs first, and it is read-only, so `install.sh --plan-only`
and the confirmation screen use it as well. It:

1. checks the OS,
2. expands `deploy_profiles` and adds `deploy_components` to get `mmd_selected`, in the
   install order from `mmd_component_order`. Exporters are added when Prometheus is local or
   `deploy_monitoring_exporters` is on.
3. works out the wiring (`wiring.yml`): where the database is, what Nginx proxies to, which
   URL Mattermost uses for rtcd and OpenSearch, what binds to localhost and what doesn't,
4. checks the hard dependencies in `mmd_dependency_checks` and stops if one fails,
5. works out the colocation and sizing advice in `mmd_colocation_rules`. These are warnings
   and never stop a run.

Every role reads the `mmd_*` facts rather than working things out for itself, so there is one
answer to "is PostgreSQL local?" and the report always agrees with what was built.

## Variables

User-facing settings live in `inventory/group_vars/all/defaults.yml` and are documented in
`answers.example.yml`. Answer files are passed with `-e @answers.yml`, so they override
everything.

Role `defaults/main.yml` files only hold internal details that nothing outside the role needs.
The report reads a lot of settings, so anything it shows has to be in group_vars, not role
defaults.

Secrets are never role defaults. `roles/common/tasks/secrets.yml` takes them from the answer
file, from `/etc/mattermost-implement/secrets.yml`, or generates them, and exposes them as
`mmd_secrets`.

## Adding a profile

Add it to `mmd_profiles` in `inventory/group_vars/all/profiles.yml` and to the three
`PROFILE_*` arrays at the top of `install.sh`. `make test` fails if the two disagree.

## Adding a component

1. Add an entry to `mmd_catalogue` in `catalogue.yml` with its title, summary, service, user,
   paths and ports. The report is built from this.
2. Add it to `mmd_component_order` and to `COMPONENT_NAMES` in `install.sh`.
3. If it ships as a tarball, teach `tools/resolve_versions.py` where to find it and its
   checksum, add it to `COMPONENT_ARTIFACTS` and `SCAN_PATTERNS`, and to
   `artifacts_tarball_components` in `roles/artifacts/defaults/main.yml`.
4. Create `roles/<name>/` with `tasks/main.yml`, `tasks/verify.yml` and `meta/main.yml`. For
   a simple single-binary service, `common/tasks/tarball_install.yml` does the unpack, symlink
   and fapolicyd work for you.
5. Use `common/tasks/firewall.yml` to open ports. Pass `fw_sources` for named clients or
   `fw_public: true` for user-facing ports. With neither, nothing opens.
6. In `verify.yml`, probe with `failed_when: false`, then hand a list of checks to
   `common/tasks/record_results.yml`. Each check has a component, name, severity
   (`critical`, `warning` or `info`), either `ok` or an explicit `status`, and a short detail.
7. Add any dependency or colocation rules, and any risks to `roles/report/templates/risks.yml.j2`.
8. Run `make lint test`.

## Upgrades

Tarball components unpack into a versioned directory under `/opt` with a symlink pointing at
the live one. Config and data sit outside the install directory. Changing the version in the
answer file and re-running moves the symlink and restarts the service, and the old version
stays on disk for a rollback. Mattermost database migrations only go forwards, so a rollback
past a minor release needs a database restore as well. The runbook in the report says so too.
