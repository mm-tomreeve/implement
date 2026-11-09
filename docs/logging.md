# Logs

The `monitoring` profile runs [Loki](https://grafana.com/oss/loki/) next to Prometheus and Grafana.
Every server with a Loki to send to gets an OpenTelemetry Collector (`otelcol-contrib`), which
ships its logs there. Grafana gets a Loki datasource and the logs dashboard from the Mattermost
docs guide [Deploy Grafana Loki for centralized logging](https://docs.mattermost.com/administration-guide/scale/deploy-grafana-loki-for-centralized-logging.html).

```
 app01 / db01 / calls01 ...                   mon01
 ┌──────────────────────────┐                 ┌─────────────────────────────┐
 │ mattermost.log           │                 │ Prometheus :9090            │
 │ postgresql/*.json        │  OTLP/HTTP      │ Loki       :3100            │
 │ nginx error.log          │ ──────────────▶ │ Grafana    :3000            │
 │ journal (our units)      │  :3100/otlp     │   Loki datasource           │
 │ otelcol                  │                 │   Mattermost logs dashboard │
 └──────────────────────────┘                 └─────────────────────────────┘
```

## Turning it on

On the monitoring server there's nothing to do: the profile includes Loki. On every other
server, point the collector at it:

```yaml
logging_loki_url: http://10.10.1.15:3100
```

The interactive installer asks for this, and suggests the first Prometheus address you gave it.
Answer `none` to keep the logs on the server. To leave one server out, set
`deploy_log_shipping: false` on it.

Logs are labelled with `otel_collector_host_name`, which is the short hostname by default. Use
the same name as the server's entry in `monitoring_estate`. The monitoring server checks that
every estate server has sent something in the last 15 minutes, and the log alerts name servers
the same way the metric alerts do.

## What gets shipped

| Source | From | Label `service_name` |
|---|---|---|
| Mattermost | `/var/log/mattermost/mattermost.log` (JSON) | `mattermost` |
| PostgreSQL | `/var/log/postgresql/*.json` (`jsonlog`, see below) | `postgresql` |
| Nginx | `/var/log/nginx/error.log` (and `access.log` with `otel_collector_nginx_access: true`) | `nginx` |
| OpenSearch | `/var/log/opensearch/<cluster>.log`, warnings and errors only | `opensearch` |
| The journal | every unit we install (rtcd, the exporters, Prometheus, Grafana, Loki, the collector), plus systemd's own messages about each one | the unit name |
| The kernel | error priority and above | `kernel` |

Only components on that server are read. Every stream also carries `host_name`,
`service_instance_id` (the same name, used by the docs dashboard) and `deployment`.

A few choices that differ from the docs guide:

- **Nothing is backfilled.** The collector starts at the end of each file and the journal, and
  keeps its read positions in `/var/lib/otelcol`. A restart doesn't ship lines twice, and a first
  install doesn't push weeks of history (which would fire log alerts for old problems).
- **Mattermost lines aren't shipped twice.** Mattermost writes each line to `mattermost.log` and
  to the journal. The collector drops the journal's JSON copies, but keeps anything else
  Mattermost writes to stderr. That's where Go panics and `fatal error:` crashes go, and they
  never reach `mattermost.log`.
- **systemd's messages are kept.** Start and stop, exit codes, `Scheduled restart job` and
  `Start request repeated too quickly` only appear in the journal. They're what the crash-loop
  alerts match on.
- **PostgreSQL logs in JSON.** When the collector is installed next to PostgreSQL, the
  `postgresql` role sets `log_destination = 'jsonlog'` and moves the log to
  `postgresql_log_dir` (`/var/log/postgresql`). Each line then carries `error_severity` and
  `state_code` (the SQLSTATE), so alerts can match on codes rather than English text. Without the
  collector, PostgreSQL logging is left as it was.
- **The collector queues on disk.** If Loki is down, lines wait in `/var/lib/otelcol` and are
  sent when it's back, for up to an hour of retries.
- **Severity is normalised.** Each source's level (Mattermost `level`, PostgreSQL
  `error_severity`, Nginx `[error]`, journal `PRIORITY`) becomes `debug`, `info`, `warn`,
  `error` or `fatal`, which Loki exposes as `detected_level`. PostgreSQL `FATAL` counts as
  `error`: it ends one session (a failed login, say), not the server.

## Retention and disk

Loki keeps `loki_retention_period` (336h, 14 days) in `loki_data_dir` (`/var/lib/loki`). Expect
roughly 1-3 GB a day for a typical estate, more with Nginx access logs. Loki refuses lines older
than a week, so a collector that comes back after a long outage doesn't fill the store with
history. The disk alerts on the monitoring server cover the volume.

## Security

- Loki listens on 3100 and is opened only to the servers in `monitoring_estate` (names are looked
  up, addresses are used as they are), `loki_allowed_sources` and `monitoring_scrape_sources` (so
  a Prometheus elsewhere can scrape its metrics). It's never open to everyone. With none of those
  it stays on 127.0.0.1. A Grafana on another server reaches it the same way: list that server in
  `monitoring_estate` with `grafana` in its components.
- Traffic to Loki is plain HTTP and Loki has no login, the same as the exporters. Keep it on an
  internal network. The report lists this as a risk.
- The collector runs as `otelcol`, in the `systemd-journal` group, with one capability:
  `CAP_DAC_READ_SEARCH`. That lets it read log files whatever their mode (Mattermost and
  PostgreSQL create theirs `0600`), but never write them. Its unit uses `ProtectSystem=strict`,
  and it can only write to `/var/lib/otelcol`.
- Both binaries are trusted for fapolicyd like the other tarball components. They run as
  `unconfined_service_t` under SELinux, like Prometheus and Grafana, so no policy changes are
  needed.
- Only the journal entries for our own units are read. `otel_collector_journald_extra_units`
  adds others (for example `[sshd, sudo]` for audit use). Think about who can read Grafana before
  you ship those.

## Searching logs

Open Grafana's Explore, pick the Loki datasource, or use the *Mattermost Log Aggregation*
dashboard in the community folder. Some useful queries:

```logql
# Mattermost errors on one server
{service_name="mattermost", host_name="app01"} | json | __error__="" | level="error"

# The most common Mattermost error messages in the last hour
topk(10, sum by (msg) (count_over_time({service_name="mattermost"} | json | __error__="" | level="error" [1h])))

# PostgreSQL errors by SQLSTATE
sum by (state_code) (count_over_time({service_name="postgresql"} | json | __error__="" | error_severity=~"ERROR|FATAL|PANIC" [1h]))

# Failed logins to PostgreSQL, by client address
sum by (remote_host) (count_over_time({service_name="postgresql"} | json | __error__="" | state_code=~"28P01|28000" [1h]))

# Everything systemd said about Mattermost: starts, stops, crashes, restart loops
{service_name="mattermost"} !~ "^\\{"

# Nginx couldn't reach Mattermost
{service_name="nginx"} |~ "connect\\(\\) failed \\(111|no live upstreams"

# Kernel disk and filesystem errors anywhere
{service_name="kernel"}
```

`| json` parses the JSON sources. Add `| __error__=""` in metric queries so a line that isn't
JSON (a journal entry in the same stream, say) doesn't turn the whole query into an error.

## Alerts on logs

Log alerts live with the metric alerts in Grafana, in the same folder, with the same tiers and
labels. They match specific failures rather than counting every error. See
[alerts.md](alerts.md#log-checks).

## Troubleshooting

| Symptom | Check |
|---|---|
| A server is missing from Grafana | `systemctl status otelcol` and `journalctl -u otelcol -n 50` on it. The post-deploy test *Loki is reachable* says whether it can reach `logging_loki_url`. |
| The monitoring server says a server isn't shipping | Its `otel_collector_host_name` must match its name in `monitoring_estate`. `curl -s localhost:3100/loki/api/v1/label/host_name/values` on the monitoring server lists the names Loki has seen. |
| Lines are being dropped | `curl -s localhost:3100/metrics \| grep loki_discarded_samples_total` on the monitoring server. The `reason` label says why (rate limit, too old, line too long). |
| The collector reports send failures | `curl -s localhost:8888/metrics \| grep otelcol_exporter_send_failed` on the server. Usually the firewall on the monitoring server, or Loki down. |
| Nothing from PostgreSQL | Is `log_destination` `jsonlog` (`SHOW log_destination;`)? Do files appear in `/var/log/postgresql`? |
| Nothing from the journal | `sudo -u otelcol journalctl -u mattermost -n 5` should show lines. If it says no permission, the user isn't in `systemd-journal` yet: re-run install.sh. |
