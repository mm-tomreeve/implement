# Alerts and dashboards

When a server runs the `monitoring` profile, Mattermost Implement asks which other servers are in
the estate and what each one runs. From that it builds:

- Prometheus jobs for every exporter, labelled with the server's name,
- outside-in checks of the site (`/api/v4/system/ping`), its certificate and the Calls media port,
- the alert rules below, in Grafana, in the *Mattermost Implement* folder,
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
