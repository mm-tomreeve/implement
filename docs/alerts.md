# Alerts and dashboards

When a server runs the `monitoring` profile, Mattermost Implement asks which other servers are in
the estate and what each one runs. From that it builds:

- Prometheus jobs for every exporter, labelled with the server's name,
- outside-in checks of the site (`/api/v4/system/ping`), its certificate and the Calls media port,
- the alert rules below, in Grafana, in the *Mattermost Implement* folder,
- log checks against Loki, when the estate ships its logs there ([logging.md](logging.md)),
- dashboards per module: an estate overview, servers, Mattermost, PostgreSQL, Nginx, OpenSearch and Calls.

Only rules for modules that exist in the estate are created.

## How the tiers work

Each check has up to three tiers: info, warning and critical. They don't overlap. A disk at 85%
fires the warning only, and at 96% the critical only, so one problem raises one alert. Every rule
carries these labels:

| Label | Values |
|---|---|
| `severity` | `info`, `warning`, `critical` |
| `module` | `host`, `site`, `mattermost`, `postgresql`, `nginx`, `opensearch`, `rtcd`, `monitoring` |
| `mmi_rule` | the check's id, as in the tables below |
| `signal` | `metrics` (Prometheus) or `logs` (Loki) |
| `deployment` | `deploy_name` from the answer file |

Rules with no data (a module with no traffic yet, or Mattermost metrics without a licence) stay
quiet. Rules whose query fails raise Grafana's own DatasourceError alert, so a dead Prometheus
doesn't go unnoticed.

## Getting notified

The rules are evaluated as soon as Grafana starts, but they have nowhere to go until you add a
contact point (Alerting > Contact points). Then edit the default notification policy and add
child policies matching on `severity`:

| Severity | Route to | Repeat |
|---|---|---|
| critical | On-call: phone, SMS or a paging tool | every hour |
| warning | Team email or a Mattermost ops channel | every 4 hours |
| info | A Mattermost channel or a daily digest. Never page for these. | every 24 hours |

Grafana can't tell you it's down itself. Point an external uptime check at
`http://<monitoring server>:3000/api/health` if you need that covered.

## Changing thresholds

The rules are provisioned from files, so change them through the answer file and re-run
`install.sh` on the monitoring server:

```yaml
monitoring_alert_overrides:
  disk-used: {tiers: {info: 75, warning: 85}}   # move tiers
  cpu-busy: {for: 30m}                          # wait longer before firing
  swap-used: {tiers: {info: null}}              # drop one tier
monitoring_alerts_disabled: [host-rebooted]      # drop a check completely
```

## Log checks

When servers ship their logs to Loki on the monitoring server, the rules include checks on what the
logs say. They use the same tiers and labels, and their descriptions carry the LogQL to paste into
Explore to see the lines behind the alert.

There's no rule for "any error". Mattermost and PostgreSQL both log harmless errors all day
(unique-key violations, clients going away, plugin chatter), so a count of errors would page
someone every night and soon be ignored. Instead there are three kinds of log check:

- **Signatures.** One failure mode each, matched on what the log actually says: a Go panic, a
  PostgreSQL SQLSTATE such as `53300` (too many clients) or `XX001` (corrupt page), systemd's
  `Scheduled restart job`, Nginx's `connect() failed (111` to Mattermost. Most stay silent for months.
  The catastrophic ones (panic, corruption, a service systemd gave up on) fire on the first line.
  The rest count over a window, with tiers.
- **Errors well above last week.** `mm-log-error-spike` and `pg-log-error-spike` compare the
  error count in the last 30 minutes with the same 30 minutes a week earlier, so a busy Monday
  morning is judged against last Monday morning. `logging_alert_baseline_smoothing` (20) is added
  to last week's count, so a quiet site doesn't alert on 3 errors after 0. Until there's a week of
  history the count is judged against the smoothing alone.
- **A new kind of error.** `mm-log-new-error` raises an info alert, one per message, when
  Mattermost logs an error whose `msg` it hasn't logged in the previous 7 days. It's usually the
  first sign of a config change, an upgrade or a failing integration. It stays quiet for the first
  week after Loki is installed, when every error would look new.

Log checks don't repeat the metric checks. PostgreSQL being down, a full disk and an OOM kill already
alert from Prometheus. The log checks add what metrics can't see: why a service keeps restarting,
which plugin is crashing, who is failing to log in to the database. They also cover Mattermost on
sites without an Enterprise licence, where Mattermost's own metrics, and so the `mm-*` metric
checks, are switched off.

Errors you know about and have accepted can be left out of the last two kinds:

```yaml
logging_alert_ignore:
  - "websocket: close 1001"
  - "Unable to get the file info"
```

Each entry is a regex matched against the raw line. Signatures ignore the list: if Mattermost
can't reach its database, you want to know. Thresholds and windows change through
`monitoring_alert_overrides` like any other check, and `monitoring_alerts_disabled: [mm-log-new-error]`
switches a check off.

Every log check is tested against real log lines (`examples` in `tools/alert_catalogue.py`): the
unit tests run its filters on them in Python, and the end-to-end test pushes them into Loki and runs
the real query.

## Every check

`<` means the alert fires when the value drops below the threshold. The id after each check's
name is what `monitoring_alert_overrides` and `monitoring_alerts_disabled` use.

<!-- generated: python3 tools/observability.py catalogue -->
### Servers

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Metrics target down `target-down` |  |  | < 1 | 3m | The server or the exporter is down, or the firewall is blocking the scrape. Check `systemctl status` for the service on that host, then `firewall-cmd --list-all`. |
| Mattermost Implement service not running `service-not-active` |  |  | < 1 | 2m | Run `systemctl status {{ $labels.name }}` and `journalctl -u {{ $labels.name }} -n 100` on the host. |
| Disk usage `disk-used` | > 70% | > 80% | > 95% | 10m | Find what is growing with `du -xh --max-depth=2 {{ $labels.mountpoint }} \| sort -h \| tail`. Usual suspects: Mattermost data, PostgreSQL WAL, OpenSearch indices, logs in /var/log. |
| Disk predicted full within 24 hours `disk-fills-24h` |  | < 0 GiB |  | 30m | Growth over the last 6 hours points to a full disk tomorrow. Find the cause before the usage alerts start. |
| Disk predicted full within 4 hours `disk-fills-4h` |  |  | < 0 GiB | 10m | Act now: free space or extend the volume. A full disk stops PostgreSQL and Mattermost. |
| Inode usage `inodes-used` | > 70% | > 80% | > 95% | 10m | Lots of small files. Check plugin caches, session files and log rotation. |
| Filesystem went read-only `filesystem-readonly` |  |  | > 0 | 1m | Usually a storage error. Check `dmesg` and the storage team before anything writes there again. |
| Memory available `memory-available` | < 20% | < 10% | < 5% | 10m | Check which process has grown with `ps aux --sort=-rss \| head`. On shared boxes OpenSearch's heap is the usual culprit. |
| Swap usage `swap-used` | > 25% | > 50% | > 80% | 15m | Sustained swapping slows everything on the host. Add memory or move a component elsewhere. |
| Out-of-memory kill `oom-kill` |  |  | > 0 | 0s | Find the victim with `journalctl -k \| grep -i 'killed process'` and check the service it belonged to restarted. |
| CPU usage `cpu-busy` | > 70% | > 85% | > 95% | 15m | Check `top`. Busy calls (rtcd), bulk indexing (OpenSearch) and big imports are the common causes. |
| Load per CPU core `load-per-core` | > 1 | > 1.5 | > 2.5 | 15m | High load with idle CPU usually means processes waiting on disk. Check `iostat -x 5`. |
| Disk I/O saturation `disk-io-busy` | > 60% | > 80% | > 95% | 15m | The disk can't keep up. PostgreSQL and OpenSearch suffer first. Look at the storage tier or spread the load. |
| Open file descriptors `fd-used` | > 70% | > 80% | > 95% | 10m | Look for a leak with `lsof \| awk '{print $1}' \| sort \| uniq -c \| sort -n \| tail`. |
| Clock drift `clock-offset` | > 50 ms | > 100 ms | > 500 ms | 10m | Check `chronyc tracking` and that the NTP servers are reachable. Drift breaks TLS and confuses message order. |
| Clock not synchronised `clock-unsynced` |  | < 1 |  | 15m | Run `chronyc sources -v` and fix NTP reachability. |
| Network interface errors `network-errors` |  | > 1/s | > 10/s | 10m | Check the NIC and switch port. Errors here show up as slow pages and broken calls. |
| Server restarted `host-rebooted` | < 15 min |  |  | 0s | Expected if it was patched. If not, find out why with `journalctl -b -1 -n 200`. |
| systemd unit failed `systemd-unit-failed` |  | > 0 |  | 5m | Run `systemctl status {{ $labels.name }}`. Clear it with `systemctl reset-failed` once fixed. |

### Site availability

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Mattermost site unreachable `site-down` |  |  | < 1 | 2m | Users can't reach Mattermost. Check Nginx, then Mattermost, then the database, in that order. |
| Mattermost site slow to respond `site-slow` | > 1 s | > 2 s | > 5 s | 10m | Check database latency and app server CPU. Slow health checks come before user complaints. |
| TLS certificate expiring `tls-expiry` | < 30 days | < 14 days | < 7 days | 1h | Renew it, put it where tls_cert_file points and re-run install.sh on the app servers. |

### Mattermost

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Mattermost API errors `mm-api-errors` | > 1% | > 5% | > 10% | 10m | Look for the error in /var/log/mattermost/mattermost.log. Database and file store problems are the usual cause. |
| Mattermost API latency (p95) `mm-api-latency` | > 1 s | > 2 s | > 5 s | 10m | Check database CPU and slow queries (pg_stat_statements), then app server CPU. |
| Mattermost database connection pool `mm-db-connections` | > 70% | > 85% | > 95% | 10m | Requests will queue when the pool is full. Look for slow queries before raising SqlSettings.MaxOpenConns. |
| Failed logins `mm-login-failures` | > 25 | > 100 | > 500 | 0s | A spike can be password spraying. Check the audit log for source addresses and consider rate limiting. |
| Push notification errors `mm-push-errors` |  | > 10 | > 100 | 0s | Check the push proxy settings and that the server can reach push.mattermost.com (or your own proxy). |
| Mattermost can't write its logs `mm-logger-errors` |  | > 0 |  | 0s | Usually a full or read-only log directory. Check /var/log/mattermost. |
| Mattermost restarted `mm-restarted` | > 0 |  |  | 0s | Expected after an upgrade or config change. If not, check `journalctl -u mattermost` for a crash. |

### PostgreSQL

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| PostgreSQL down `pg-down` |  |  | < 1 | 1m | Mattermost stops working without its database. Check `systemctl status postgresql` and the log in the data directory. |
| PostgreSQL connections `pg-connections` | > 70% | > 80% | > 90% | 10m | New connections will be refused when this hits 100%. Look for idle sessions and app servers with oversized pools. |
| Long-running PostgreSQL transaction `pg-long-transaction` | > 300 s | > 900 s | > 3600 s | 2m | Long transactions block vacuum and can lock tables. Find it in pg_stat_activity and decide whether to cancel it. |
| PostgreSQL deadlocks `pg-deadlocks` |  | > 0 | > 10 | 0s | Check the PostgreSQL log for the statements involved. Report repeat offenders to Mattermost support. |
| PostgreSQL cache hit ratio `pg-cache-hit` | < 99% | < 95% | < 90% | 30m | The working set no longer fits in shared_buffers. Add memory or move PostgreSQL to its own server. |
| PostgreSQL rollback rate `pg-rollbacks` | > 5% | > 15% |  | 15m | Some rollbacks are normal. A rise usually matches errors in the Mattermost log. |
| Sessions idle in transaction `pg-idle-in-transaction` |  | > 10 | > 50 | 5m | These hold locks and connections. idle_in_transaction_session_timeout should clear them; find the client if it doesn't. |
| PostgreSQL replica lag `pg-replication-lag` | > 60 s | > 300 s | > 1800 s | 5m | Reads Mattermost sends to the replica are this far out of date. Check disk I/O on the replica and the network to the primary, and run `SELECT * FROM pg_stat_wal_receiver` on the replica. |
| PostgreSQL replica disconnected `pg-replica-disconnected` |  | < 1 |  | 5m | The replica is down or can't reach the primary. Until it reconnects the primary keeps WAL for it, up to max_slot_wal_keep_size. Check `systemctl status postgresql` and the log on the replica. |
| WAL held for a PostgreSQL replica `pg-slot-wal-held` | > 25% | > 50% | > 80% | 5m | The replica is down or far behind. At 100% PostgreSQL gives the slot up to save the primary's disk, and the replica has to be rebuilt. Bring the replica back, or if it's gone for good run `SELECT pg_drop_replication_slot('<slot>')` on the primary to free the WAL now. |
| PostgreSQL replica can't catch up `pg-slot-lost` |  |  | > 0 | 1m | The replica can't catch up any more, so rebuild it. On the replica, stop postgresql, move the data directory aside and run install.sh again. It drops the old slot and takes a fresh copy. |

### Nginx

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Nginx down `nginx-down` |  |  | < 1 | 1m | Check `systemctl status nginx` and `nginx -t`. |
| Nginx dropping connections `nginx-dropped` |  | > 0/s | > 5/s | 5m | Nginx has run out of worker connections or file descriptors. Raise worker_connections and LimitNOFILE. |

### OpenSearch

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| OpenSearch not answering `os-unreachable` |  |  | < 1 | 2m | Search falls back to the database or fails. Check `systemctl status opensearch` and its log. |
| OpenSearch cluster red `os-red` |  |  | > 0 | 2m | Some primary shards are missing, so search results are incomplete. Check `_cluster/allocation/explain`. |
| OpenSearch cluster yellow `os-yellow` | > 0 |  |  | 30m | Replica shards have nowhere to go. Normal on a single node; on a cluster it means a node is missing. |
| OpenSearch JVM heap `os-heap` | > 75% | > 85% | > 95% | 10m | Sustained high heap means long GC pauses and then crashes. Raise opensearch_heap_mb if the host has the RAM. |
| OpenSearch data disk `os-disk` | > 75% | > 85% | > 90% | 10m | At 85% OpenSearch stops placing replicas, at 90% it moves shards away and at 95% indices go read-only. |
| OpenSearch rejecting work `os-rejections` |  | > 0/s | > 5/s | 5m | Searches or index updates are being dropped. The node is overloaded; add CPU or heap. |

### Calls (rtcd)

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Calls media port unreachable `calls-port-unreachable` |  |  | < 1 | 3m | Calls will connect but carry no audio. Check rtcd is running and the firewall allows the media port. |
| rtcd CPU usage `rtcd-cpu` | > 60% | > 80% | > 95% | 10m | Calls will start to stutter. Add CPU or another rtcd server. |
| rtcd open file descriptors `rtcd-fds` | > 70% | > 80% | > 90% | 10m | rtcd opens a socket per connection. Raise LimitNOFILE in the rtcd unit, or add another rtcd server. |
| UDP receive buffer overflows `udp-buffer-errors` |  | > 0/s | > 50/s | 5m | Media packets are being lost before rtcd reads them. Check net.core.rmem_max and CPU on the host. |
| rtcd media errors `rtcd-errors` | > 10 | > 50 | > 200 | 0s | Check `journalctl -u rtcd` for the error types. Network paths and ICE settings are the usual cause. |
| Calls packet loss reported by clients `rtcd-packet-loss` | > 2% | > 5% | > 10% | 10m | Users will hear choppy audio. Check bandwidth to the rtcd server and UDP buffer overflows. |

### Monitoring

| Check | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|
| Prometheus config reload failed `prometheus-config` |  | < 1 |  | 5m | Run `/opt/prometheus/promtool check config /etc/prometheus/prometheus.yml`, then fix and reload. |
| Scrapes taking too long `prometheus-scrape-slow` | > 5 s | > 10 s |  | 15m | The exporter or the network path is struggling. Dashboards and alerts lag behind reality. |
| Logs failing to reach Loki `log-ship-failing` |  | > 0/s |  | 10m | Lines queue on disk in /var/lib/otelcol for up to an hour, then they're lost. Check Loki is up and that the monitoring server's firewall lets this server in on port 3100 (`journalctl -u otelcol -n 50`). |
| Loki refusing log lines `loki-discarding` |  | > 0/s |  | 10m | Log alerts can't see lines Loki throws away. rate_limited or stream_limit: raise the limits in /etc/loki/config.yml or ship less. greater_than_max_sample_age: a collector is sending week-old lines; check its clock. |

### Servers log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| Service restarting over and over `unit-crash-loop` | any service's logs where line matches `Scheduled restart job, restart counter is at`, per `unit` | 15m |  |  | > 2 | 0s | systemd keeps restarting it, so it may look up between checks. Read why it exits: `journalctl -u {{ $labels.unit }} -n 200`. |
| systemd gave up restarting a service `unit-start-limit` | any service's logs where line matches `Start request repeated too quickly`, per `unit` | 15m |  |  | > 0 | 0s | It stays down until someone acts. Fix the cause (`journalctl -u {{ $labels.unit }} -n 200`), then `systemctl reset-failed {{ $labels.unit }}` and start it. |
| Service crashed with a core dump `unit-core-dump` | any service's logs where line matches `Main process exited, code=dumped`, per `unit` | 15m |  | > 0 |  | 0s | A segfault or abort, not a clean exit. `coredumpctl list` shows the dump; keep it for the vendor. |
| Kernel disk or filesystem errors `kernel-io-errors` | `kernel` logs where line matches `I/O error\|EXT4-fs error\|XFS \([^)]+\):.*([Ee]rror\|[Cc]orruption)\|Remounting filesystem read-only\|Aborting journal` | 15m |  |  | > 0 | 0s | Storage is failing or a filesystem is damaged. Read `dmesg -T \| tail -50` and call the storage team before PostgreSQL or Mattermost write anything else there. |

### Mattermost log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| Mattermost logged a fatal error `mm-log-fatal` | `mattermost` logs where `level` is `fatal\|panic\|critical` | 5m |  |  | > 0 | 0s | Mattermost stops, or loses a part of itself, after one of these. Read the lines, then `journalctl -u mattermost -n 100` on the server. |
| Mattermost crashed (Go panic) `mm-go-panic` | `mattermost` logs where line matches `^(panic: \|fatal error: )` | 10m |  |  | > 0 | 0s | systemd restarts it, but the crash will come back. Save the stack trace (`journalctl -u mattermost --since -15m`) and send it to Mattermost support with the version. |
| Mattermost can't use its database `mm-log-db` | `mattermost` logs where line matches `Failed to ping DB\|pq: sorry, too many clients\|pq: the database system is (shutting down\|starting up\|in recovery mode)\|driver: bad connection\|dial tcp [^ \"]+:5432: connect: connection refused` and `level` is `error\|critical\|fatal` | 10m |  | > 10 | > 100 | 2m | Check PostgreSQL is up and accepting connections from this server, and max_connections. This is the only database alert when the database is outside the monitored estate. |
| Mattermost can't write uploaded files `mm-log-storage` | `mattermost` logs where line matches `no space left on device\|read-only file system\|/var/opt/mattermost/data[^\"]*: permission denied\|Access Denied\.\|The specified bucket does not exist\|The AWS Access Key Id you provided does not exist\|The request signature we calculated does not match` and `level` is `warn\|error\|critical\|fatal` | 10m |  | > 0 | > 20 | 0s | Uploads are failing. For local or NFS storage check free space, mounts and ownership of the data directory. For S3 check the bucket, the keys and the endpoint. |
| Mattermost plugin crashing `mm-log-plugin-crash` | `mattermost` logs where `msg` is `Health check failed for plugin\|Failed to restart plugin`, per `id` | 1h |  | > 0 | > 2 | 0s | Mattermost restarts a crashed plugin, and disables it after repeated crashes. Check the plugin's own log lines just before, and its version against the server version. |
| Mattermost can't send email `mm-log-smtp` | `mattermost` logs where line matches `unable to connect to the SMTP server\|unable to send hello message\|Error while sending the email` and `level` is `warn\|error` | 30m |  | > 5 |  | 0s | Notifications, invites and password resets aren't going out. Check the SMTP settings in the System Console and that this server can reach the mail server. |
| Search indexing failing `mm-log-search-index` | `mattermost` logs where `msg` is `Worker: Failed to index batch for job\|Worker: Failed to setup bulk processor` | 15m |  | > 0 |  | 0s | New messages won't show in search results. Check OpenSearch is green and has disk space, then run a bulk index from System Console > Environment > Elasticsearch. |
| Mattermost background job failed `mm-log-jobs` | `mattermost` logs where `msg` is `.*job execution error` | 1h |  | > 0 |  | 0s | Data retention, compliance exports, LDAP sync and the like run as jobs. Find which one failed in System Console > Jobs or in the lines around this one. |
| Mattermost errors well above last week `mm-log-error-spike` | `mattermost` logs where `level` is `error`, as a multiple of the same window a week earlier | 30m | > 3x | > 5x | > 10x | 15m | Something changed. Look at the top error messages now (Explore, or the Mattermost logs dashboard) and compare with last week. Add known, accepted errors to logging_alert_ignore. |
| New kind of Mattermost error `mm-log-new-error` | `mattermost` logs where `level` is `error`, per `msg`, not seen in the 7 days before | 1h | > 0 |  |  | 0s | Usually the first sign of a config change, an upgrade or a failing integration. Harmless ones can go in logging_alert_ignore. This rule stays quiet for the first week after Loki is installed. |

### PostgreSQL log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| PostgreSQL data corruption or PANIC `pg-log-corruption` | `postgresql` logs where line matches `"state_code":\s*"XX00[12]"\|"error_severity":\s*"PANIC"` | 10m |  |  | > 0 | 0s | Stop and think before restarting anything. Check the storage (`dmesg`), take a copy of the data directory, and plan a restore from backup for the affected table or index. |
| PostgreSQL I/O or disk full errors `pg-log-io` | `postgresql` logs where `state_code` is `58...\|53100` | 10m |  |  | > 0 | 0s | Free space on the data volume first (WAL in pg_wal is the usual culprit when a replica is down), then check the storage for errors. |
| PostgreSQL refusing connections `pg-log-conn-refused` | `postgresql` logs where `state_code` is `53300` | 5m |  |  | > 0 | 0s | Requests are failing now. Look for idle sessions in pg_stat_activity and app servers with oversized pools. pg-connections is the early warning for this. |
| PostgreSQL login failures `pg-log-auth-failures` | `postgresql` logs where `state_code` is `28P01\|28000`, per `remote_host` | 10m |  | > 5 | > 50 | 0s | An app server with an old password after a rotation, or someone guessing. Known app servers: fix mattermost_db_password there. Unknown addresses: tighten postgresql_allowed_clients. |
| PostgreSQL recovered from a crash `pg-log-crash-recovery` | `postgresql` logs where line matches `database system was (not properly shut down\|interrupted)` | 30m |  | > 0 |  | 0s | It replayed WAL and carried on, so data should be intact. Find out why it went down: `journalctl -u postgresql -b -1`, the OOM killer, or a host reboot. |
| PostgreSQL checkpoints too frequent `pg-log-checkpoints` | `postgresql` logs where line matches `checkpoints are occurring too frequently` | 1h | > 0 |  |  | 0s | Write-heavy periods (imports, bulk deletes) fill WAL faster than max_wal_size allows. Raise max_wal_size if this keeps happening. |
| PostgreSQL errors well above last week `pg-log-error-spike` | `postgresql` logs where `error_severity` is `ERROR\|FATAL\|PANIC` and `state_code` isn't `23...\|40P01\|28...\|57P0[1-3]`, as a multiple of the same window a week earlier | 30m | > 3x | > 5x | > 10x | 15m | Group the errors by state_code in Explore to see what changed. A new Mattermost version or a failing integration are the usual causes. |

### Nginx log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| Nginx can't reach Mattermost `nginx-log-upstream-down` | `nginx` logs where line matches `connect\(\) failed \(111: Connection refused\) while connecting to upstream\|no live upstreams while connecting to upstream` | 5m |  | > 10 | > 100 | 2m | Users are getting 502s. Check Mattermost is running on the upstream servers and listening on 8065. |
| Mattermost too slow to answer Nginx `nginx-log-upstream-slow` | `nginx` logs where line matches `upstream timed out \(110: Connection timed out\) while reading response header from upstream` | 10m |  | > 10 |  | 0s | Users see 504s. Usually slow database queries; check PostgreSQL CPU and long-running queries. |
| Nginx out of connections or files `nginx-log-limits` | `nginx` logs where line matches `worker_connections are not enough\|Too many open files` | 10m |  | > 0 |  | 0s | This is why connections get dropped. Raise worker_connections and LimitNOFILE for Nginx. |

### OpenSearch log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| OpenSearch ran out of heap `os-log-oom` | `opensearch` logs where line matches `java\.lang\.OutOfMemoryError` | 15m |  |  | > 0 | 0s | The JVM is unstable after this; restart OpenSearch, then raise opensearch_heap_mb or add memory. |
| OpenSearch circuit breaker tripping `os-log-circuit-breaker` | `opensearch` logs where line matches `CircuitBreakingException` | 10m |  | > 5 |  | 0s | Searches or indexing are being refused. The heap is too small for the load; raise opensearch_heap_mb. |

### Monitoring log checks

| Check | Watches for | Window | Info | Warning | Critical | For | What to do |
|---|---|---|---|---|---|---|---|
| Server stopped sending logs `log-host-silent` | a server in the estate that sent no log lines at all | 15m |  | < 1 |  | 15m | The log alerts are blind for this server. Check `systemctl status otelcol` on it, that logging_loki_url points here, and that otel_collector_host_name matches its name in monitoring_estate. |
