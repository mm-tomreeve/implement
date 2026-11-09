"""The alert catalogue for Mattermost Implement.

One entry per thing worth knowing about. tools/observability.py turns each entry
into one Grafana alert rule per tier (info, warning, critical). Tiers never overlap:
a disk at 96% raises the critical alert only, not all three.

Fields:
  id          stable identifier, used for overrides and rule UIDs
  module      host | mattermost | postgresql | nginx | opensearch | rtcd | site | monitoring
  title       short name shown in Grafana
  expr        PromQL returning one series per thing being judged. No comparison here;
              the threshold lives in Grafana so it shows in the UI and can be overridden.
  direction   above (bigger is worse) or below (smaller is worse)
  tiers       {info|warning|critical: threshold}. Leave a tier out if it isn't useful.
  for         how long the condition must hold before the alert fires
  unit        how to print the value in the summary ("%", "s", "days", "" ...)
  summary     one line, Grafana template syntax ($labels, $values.A.Value)
  action      what the on-call person should do. Goes in the description.
  quiet       true when the series only exist once there is traffic (a call, a login
              failure, a licence). The e2e check reports these instead of failing.
  requires    "logs" for checks that only make sense when the estate ships logs to Loki.

Log checks (LOG_CATALOGUE, further down) query Loki instead. See the notes there.

Thresholds and "for" can be changed per site with monitoring_alert_overrides in the
answer file, and any rule switched off with monitoring_alerts_disabled.
"""

import json

FS = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|nsfs|ramfs|autofs|fuse.*",mountpoint!~"/(run|dev|sys|proc)($|/.*)|/etc/.+"'
# Virtual and tunnel interfaces the kernel creates on its own; never interesting.
NETDEV = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*|gre.*|gretap.*|erspan.*|ip6.*|sit.*|tunl.*|ip_vti.*|ip6_vti.*|nbd.*"'
BLOCKDEV = 'device!~"dm-.*|loop.*|sr.*|nbd.*|ram.*|zram.*"'

CATALOGUE = [
    # ------------------------------------------------------------------ every server
    {
        "id": "target-down", "module": "host", "title": "Metrics target down",
        # The log shipper has its own, gentler checks (log-ship-failing, log-host-silent).
        "expr": 'up{job!~"blackbox.*|otelcol"}',
        "direction": "below", "tiers": {"critical": 1}, "for": "3m", "unit": "",
        "summary": "{{ $labels.job }} on {{ $labels.instance }} is not answering Prometheus",
        "action": "The server or the exporter is down, or the firewall is blocking the scrape. "
                  "Check `systemctl status` for the service on that host, then `firewall-cmd --list-all`.",
    },
    {
        "id": "service-not-active", "module": "host", "title": "Mattermost Implement service not running",
        "expr": 'node_systemd_unit_state{state="active"}',
        "direction": "below", "tiers": {"critical": 1}, "for": "2m", "unit": "",
        "summary": "{{ $labels.name }} is not active on {{ $labels.instance }}",
        "action": "Run `systemctl status {{ $labels.name }}` and `journalctl -u {{ $labels.name }} -n 100` on the host.",
    },
    {
        "id": "disk-used", "module": "host", "title": "Disk usage",
        "expr": "100 * (1 - node_filesystem_avail_bytes{%s} / node_filesystem_size_bytes{%s})" % (FS, FS),
        "direction": "above", "tiers": {"info": 70, "warning": 80, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.mountpoint }} on {{ $labels.instance }} is {{ printf \"%.0f\" $values.A.Value }}% full",
        "action": "Find what is growing with `du -xh --max-depth=2 {{ $labels.mountpoint }} | sort -h | tail`. "
                  "Usual suspects: Mattermost data, PostgreSQL WAL, OpenSearch indices, logs in /var/log.",
    },
    {
        "id": "disk-fills-24h", "module": "host", "title": "Disk predicted full within 24 hours",
        "expr": "predict_linear(node_filesystem_avail_bytes{%s}[6h], 24 * 3600) / 1024 / 1024 / 1024" % FS,
        "direction": "below", "tiers": {"warning": 0}, "for": "30m", "unit": " GiB",
        "summary": "At the current rate {{ $labels.mountpoint }} on {{ $labels.instance }} fills within a day",
        "action": "Growth over the last 6 hours points to a full disk tomorrow. Find the cause before the usage alerts start.",
    },
    {
        "id": "disk-fills-4h", "module": "host", "title": "Disk predicted full within 4 hours",
        "expr": "predict_linear(node_filesystem_avail_bytes{%s}[1h], 4 * 3600) / 1024 / 1024 / 1024" % FS,
        "direction": "below", "tiers": {"critical": 0}, "for": "10m", "unit": " GiB",
        "summary": "{{ $labels.mountpoint }} on {{ $labels.instance }} will be full within 4 hours at the current rate",
        "action": "Act now: free space or extend the volume. A full disk stops PostgreSQL and Mattermost.",
    },
    {
        "id": "inodes-used", "module": "host", "title": "Inode usage",
        "expr": "100 * (1 - node_filesystem_files_free{%s} / node_filesystem_files{%s})" % (FS, FS),
        "direction": "above", "tiers": {"info": 70, "warning": 80, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.mountpoint }} on {{ $labels.instance }} has used {{ printf \"%.0f\" $values.A.Value }}% of its inodes",
        "action": "Lots of small files. Check plugin caches, session files and log rotation.",
    },
    {
        "id": "filesystem-readonly", "module": "host", "title": "Filesystem went read-only",
        # Fires when a filesystem flips to read-only, not for mounts that are read-only by design.
        "expr": "node_filesystem_readonly{%s} - min_over_time(node_filesystem_readonly{%s}[6h])" % (FS, FS),
        "direction": "above", "tiers": {"critical": 0}, "for": "1m", "unit": "",
        "summary": "{{ $labels.mountpoint }} on {{ $labels.instance }} has gone read-only",
        "action": "Usually a storage error. Check `dmesg` and the storage team before anything writes there again.",
    },
    {
        "id": "memory-available", "module": "host", "title": "Memory available",
        "expr": "100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
        "direction": "below", "tiers": {"info": 20, "warning": 10, "critical": 5}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.instance }} has {{ printf \"%.0f\" $values.A.Value }}% of its memory available",
        "action": "Check which process has grown with `ps aux --sort=-rss | head`. On shared boxes OpenSearch's heap is the usual culprit.",
    },
    {
        "id": "swap-used", "module": "host", "title": "Swap usage",
        "expr": "100 * (1 - node_memory_SwapFree_bytes / clamp_min(node_memory_SwapTotal_bytes, 1)) and node_memory_SwapTotal_bytes > 0",
        "direction": "above", "tiers": {"info": 25, "warning": 50, "critical": 80}, "for": "15m", "unit": "%",
        "summary": "{{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of its swap",
        "action": "Sustained swapping slows everything on the host. Add memory or move a component elsewhere.",
        "quiet": True,
    },
    {
        "id": "oom-kill", "module": "host", "title": "Out-of-memory kill",
        "expr": "increase(node_vmstat_oom_kill[15m])",
        "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "The kernel killed a process for lack of memory on {{ $labels.instance }}",
        "action": "Find the victim with `journalctl -k | grep -i 'killed process'` and check the service it belonged to restarted.",
    },
    {
        "id": "cpu-busy", "module": "host", "title": "CPU usage",
        "expr": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "direction": "above", "tiers": {"info": 70, "warning": 85, "critical": 95}, "for": "15m", "unit": "%",
        "summary": "CPU on {{ $labels.instance }} has been {{ printf \"%.0f\" $values.A.Value }}% busy for 15 minutes",
        "action": "Check `top`. Busy calls (rtcd), bulk indexing (OpenSearch) and big imports are the common causes.",
    },
    {
        "id": "load-per-core", "module": "host", "title": "Load per CPU core",
        "expr": 'node_load5 / on (instance) count by (instance) (node_cpu_seconds_total{mode="idle"})',
        "direction": "above", "tiers": {"info": 1.0, "warning": 1.5, "critical": 2.5}, "for": "15m", "unit": "",
        "summary": "Load on {{ $labels.instance }} is {{ printf \"%.2f\" $values.A.Value }} per core",
        "action": "High load with idle CPU usually means processes waiting on disk. Check `iostat -x 5`.",
    },
    {
        "id": "disk-io-busy", "module": "host", "title": "Disk I/O saturation",
        "expr": 'max by (instance, device) (rate(node_disk_io_time_seconds_total{%s}[5m])) * 100' % BLOCKDEV,
        "direction": "above", "tiers": {"info": 60, "warning": 80, "critical": 95}, "for": "15m", "unit": "%",
        "summary": "{{ $labels.device }} on {{ $labels.instance }} is busy {{ printf \"%.0f\" $values.A.Value }}% of the time",
        "action": "The disk can't keep up. PostgreSQL and OpenSearch suffer first. Look at the storage tier or spread the load.",
    },
    {
        "id": "fd-used", "module": "host", "title": "Open file descriptors",
        "expr": "100 * node_filefd_allocated / node_filefd_maximum",
        "direction": "above", "tiers": {"info": 70, "warning": 80, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of the system file descriptor limit",
        "action": "Look for a leak with `lsof | awk '{print $1}' | sort | uniq -c | sort -n | tail`.",
    },
    {
        "id": "clock-offset", "module": "host", "title": "Clock drift",
        "expr": "abs(node_timex_offset_seconds) * 1000",
        "direction": "above", "tiers": {"info": 50, "warning": 100, "critical": 500}, "for": "10m", "unit": " ms",
        "summary": "The clock on {{ $labels.instance }} is {{ printf \"%.0f\" $values.A.Value }} ms off",
        "action": "Check `chronyc tracking` and that the NTP servers are reachable. Drift breaks TLS and confuses message order.",
    },
    {
        "id": "clock-unsynced", "module": "host", "title": "Clock not synchronised",
        "expr": "node_timex_sync_status",
        "direction": "below", "tiers": {"warning": 1}, "for": "15m", "unit": "",
        "summary": "{{ $labels.instance }} is not synchronised to a time source",
        "action": "Run `chronyc sources -v` and fix NTP reachability.",
    },
    {
        "id": "network-errors", "module": "host", "title": "Network interface errors",
        "expr": 'sum by (instance, device) (rate(node_network_receive_errs_total{%s}[5m]) '
                '+ rate(node_network_transmit_errs_total{%s}[5m]))' % (NETDEV, NETDEV),
        "direction": "above", "tiers": {"warning": 1, "critical": 10}, "for": "10m", "unit": "/s",
        "summary": "{{ $labels.device }} on {{ $labels.instance }} has {{ printf \"%.1f\" $values.A.Value }} errors a second",
        "action": "Check the NIC and switch port. Errors here show up as slow pages and broken calls.",
    },
    {
        "id": "host-rebooted", "module": "host", "title": "Server restarted",
        "expr": "(time() - node_boot_time_seconds) / 60",
        "direction": "below", "tiers": {"info": 15}, "for": "0s", "unit": " min",
        "summary": "{{ $labels.instance }} booted {{ printf \"%.0f\" $values.A.Value }} minutes ago",
        "action": "Expected if it was patched. If not, find out why with `journalctl -b -1 -n 200`.",
    },
    {
        "id": "systemd-unit-failed", "module": "host", "title": "systemd unit failed",
        "expr": 'node_systemd_unit_state{state="failed"}',
        "direction": "above", "tiers": {"warning": 0}, "for": "5m", "unit": "",
        "summary": "{{ $labels.name }} is in a failed state on {{ $labels.instance }}",
        "action": "Run `systemctl status {{ $labels.name }}`. Clear it with `systemctl reset-failed` once fixed.",
    },

    # ------------------------------------------------------------------ users' view of the site
    {
        "id": "site-down", "module": "site", "title": "Mattermost site unreachable",
        "expr": 'probe_success{job="blackbox_http"}',
        "direction": "below", "tiers": {"critical": 1}, "for": "2m", "unit": "",
        "summary": "{{ $labels.instance }} is failing its health check from the monitoring server",
        "action": "Users can't reach Mattermost. Check Nginx, then Mattermost, then the database, in that order.",
    },
    {
        "id": "site-slow", "module": "site", "title": "Mattermost site slow to respond",
        "expr": 'probe_duration_seconds{job="blackbox_http"}',
        "direction": "above", "tiers": {"info": 1, "warning": 2, "critical": 5}, "for": "10m", "unit": " s",
        "summary": "The health check for {{ $labels.instance }} takes {{ printf \"%.2f\" $values.A.Value }} s",
        "action": "Check database latency and app server CPU. Slow health checks come before user complaints.",
    },
    {
        "id": "tls-expiry", "module": "site", "title": "TLS certificate expiring",
        "expr": '(probe_ssl_earliest_cert_expiry{job="blackbox_http"} - time()) / 86400',
        "direction": "below", "tiers": {"info": 30, "warning": 14, "critical": 7}, "for": "1h", "unit": " days",
        "summary": "The certificate for {{ $labels.instance }} expires in {{ printf \"%.0f\" $values.A.Value }} days",
        "action": "Renew it, put it where tls_cert_file points and re-run install.sh on the app servers.",
    },
    {
        "id": "calls-port-unreachable", "module": "rtcd", "title": "Calls media port unreachable",
        "expr": 'probe_success{job="blackbox_tcp_calls"}',
        "direction": "below", "tiers": {"critical": 1}, "for": "3m", "unit": "",
        "summary": "Nobody can connect to Calls media on {{ $labels.instance }}",
        "action": "Calls will connect but carry no audio. Check rtcd is running and the firewall allows the media port.",
    },

    # ------------------------------------------------------------------ mattermost
    {
        "id": "mm-api-errors", "module": "mattermost", "title": "Mattermost API errors",
        "expr": '100 * sum by (instance) (rate(mattermost_api_time_count{status_code=~"5.."}[5m])) '
                '/ clamp_min(sum by (instance) (rate(mattermost_api_time_count[5m])), 0.001)',
        "direction": "above", "tiers": {"info": 1, "warning": 5, "critical": 10}, "for": "10m", "unit": "%",
        "summary": "{{ printf \"%.1f\" $values.A.Value }}% of API calls on {{ $labels.instance }} are failing",
        "action": "Look for the error in /var/log/mattermost/mattermost.log. Database and file store problems are the usual cause.",
        "quiet": True,
    },
    {
        "id": "mm-api-latency", "module": "mattermost", "title": "Mattermost API latency (p95)",
        "expr": 'histogram_quantile(0.95, sum by (le, instance) (rate(mattermost_api_time_bucket{handler!~"connectWebSocket|.*[Ll]ongPoll.*"}[5m])))',
        "direction": "above", "tiers": {"info": 1, "warning": 2, "critical": 5}, "for": "10m", "unit": " s",
        "summary": "95% of API calls on {{ $labels.instance }} finish within {{ printf \"%.2f\" $values.A.Value }} s",
        "action": "Check database CPU and slow queries (pg_stat_statements), then app server CPU.",
        "quiet": True,
    },
    {
        "id": "mm-db-connections", "module": "mattermost", "title": "Mattermost database connection pool",
        "expr": "100 * mattermost_db_master_connections_total / %(mm_max_conns)s",
        "direction": "above", "tiers": {"info": 70, "warning": 85, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of its database connections",
        "action": "Requests will queue when the pool is full. Look for slow queries before raising SqlSettings.MaxOpenConns.",
        "quiet": True,
    },
    {
        "id": "mm-login-failures", "module": "mattermost", "title": "Failed logins",
        "expr": "increase(mattermost_login_logins_fail_total[15m])",
        "direction": "above", "tiers": {"info": 25, "warning": 100, "critical": 500}, "for": "0s", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} failed logins in 15 minutes on {{ $labels.instance }}",
        "action": "A spike can be password spraying. Check the audit log for source addresses and consider rate limiting.",
        "quiet": True,
    },
    {
        "id": "mm-push-errors", "module": "mattermost", "title": "Push notification errors",
        "expr": "sum by (instance) (increase(mattermost_notifications_error[15m]))",
        "direction": "above", "tiers": {"warning": 10, "critical": 100}, "for": "0s", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} notifications failed in 15 minutes on {{ $labels.instance }}",
        "action": "Check the push proxy settings and that the server can reach push.mattermost.com (or your own proxy).",
        "quiet": True,
    },
    {
        "id": "mm-logger-errors", "module": "mattermost", "title": "Mattermost can't write its logs",
        "expr": "increase(mattermost_logging_logger_error_total[15m])",
        "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.instance }} reported {{ printf \"%.0f\" $values.A.Value }} logging errors",
        "action": "Usually a full or read-only log directory. Check /var/log/mattermost.",
        "quiet": True,
    },
    {
        "id": "mm-restarted", "module": "mattermost", "title": "Mattermost restarted",
        "expr": "changes(mattermost_system_server_start_time[15m])",
        "direction": "above", "tiers": {"info": 0}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.instance }} restarted",
        "action": "Expected after an upgrade or config change. If not, check `journalctl -u mattermost` for a crash.",
        "quiet": True,
    },

    # ------------------------------------------------------------------ postgresql
    {
        "id": "pg-down", "module": "postgresql", "title": "PostgreSQL down",
        "expr": "pg_up",
        "direction": "below", "tiers": {"critical": 1}, "for": "1m", "unit": "",
        "summary": "postgres_exporter on {{ $labels.instance }} can't reach PostgreSQL",
        "action": "Mattermost stops working without its database. Check `systemctl status postgresql` and the log in the data directory.",
    },
    {
        "id": "pg-connections", "module": "postgresql", "title": "PostgreSQL connections",
        "expr": "100 * sum by (instance) (pg_stat_activity_count) / on (instance) max by (instance) (pg_settings_max_connections)",
        "direction": "above", "tiers": {"info": 70, "warning": 80, "critical": 90}, "for": "10m", "unit": "%",
        "summary": "{{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of max_connections",
        "action": "New connections will be refused when this hits 100%. Look for idle sessions and app servers with oversized pools.",
    },
    {
        "id": "pg-long-transaction", "module": "postgresql", "title": "Long-running PostgreSQL transaction",
        "expr": 'max by (instance) (pg_stat_activity_max_tx_duration{datname="%(db_name)s"})',
        "direction": "above", "tiers": {"info": 300, "warning": 900, "critical": 3600}, "for": "2m", "unit": " s",
        "summary": "A transaction on {{ $labels.instance }} has been open for {{ printf \"%.0f\" $values.A.Value }} s",
        "action": "Long transactions block vacuum and can lock tables. Find it in pg_stat_activity and decide whether to cancel it.",
    },
    {
        "id": "pg-deadlocks", "module": "postgresql", "title": "PostgreSQL deadlocks",
        "expr": 'increase(pg_stat_database_deadlocks{datname="%(db_name)s"}[15m])',
        "direction": "above", "tiers": {"warning": 0, "critical": 10}, "for": "0s", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} deadlocks in 15 minutes on {{ $labels.instance }}",
        "action": "Check the PostgreSQL log for the statements involved. Report repeat offenders to Mattermost support.",
    },
    {
        "id": "pg-cache-hit", "module": "postgresql", "title": "PostgreSQL cache hit ratio",
        "expr": '100 * rate(pg_stat_database_blks_hit{datname="%(db_name)s"}[30m]) '
                '/ clamp_min(rate(pg_stat_database_blks_hit{datname="%(db_name)s"}[30m]) + rate(pg_stat_database_blks_read{datname="%(db_name)s"}[30m]), 1)',
        "direction": "below", "tiers": {"info": 99, "warning": 95, "critical": 90}, "for": "30m", "unit": "%",
        "summary": "Only {{ printf \"%.1f\" $values.A.Value }}% of reads on {{ $labels.instance }} come from memory",
        "action": "The working set no longer fits in shared_buffers. Add memory or move PostgreSQL to its own server.",
    },
    {
        "id": "pg-rollbacks", "module": "postgresql", "title": "PostgreSQL rollback rate",
        "expr": '100 * rate(pg_stat_database_xact_rollback{datname="%(db_name)s"}[15m]) '
                '/ clamp_min(rate(pg_stat_database_xact_rollback{datname="%(db_name)s"}[15m]) + rate(pg_stat_database_xact_commit{datname="%(db_name)s"}[15m]), 0.001)',
        "direction": "above", "tiers": {"info": 5, "warning": 15}, "for": "15m", "unit": "%",
        "summary": "{{ printf \"%.1f\" $values.A.Value }}% of transactions on {{ $labels.instance }} are rolling back",
        "action": "Some rollbacks are normal. A rise usually matches errors in the Mattermost log.",
    },
    {
        "id": "pg-idle-in-transaction", "module": "postgresql", "title": "Sessions idle in transaction",
        "expr": 'sum by (instance) (pg_stat_activity_count{state="idle in transaction"})',
        "direction": "above", "tiers": {"warning": 10, "critical": 50}, "for": "5m", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} sessions are idle in a transaction on {{ $labels.instance }}",
        "action": "These hold locks and connections. idle_in_transaction_session_timeout should clear them; find the client if it doesn't.",
    },
    # Replication. These series only exist where a primary feeds a read replica.
    {
        "id": "pg-replication-lag", "module": "postgresql", "title": "PostgreSQL replica lag",
        # 0 on a primary, and on a replica that has replayed everything it received.
        "expr": "pg_replication_lag_seconds",
        "direction": "above", "tiers": {"info": 60, "warning": 300, "critical": 1800}, "for": "5m", "unit": " s",
        "summary": "The replica {{ $labels.instance }} is {{ printf \"%.0f\" $values.A.Value }} s behind its primary",
        "action": "Reads Mattermost sends to the replica are this far out of date. Check disk I/O on the replica and the "
                  "network to the primary, and run `SELECT * FROM pg_stat_wal_receiver` on the replica.",
        "quiet": True,
    },
    {
        "id": "pg-replica-disconnected", "module": "postgresql", "title": "PostgreSQL replica disconnected",
        "expr": 'pg_replication_slots_slot_is_active{slot_type="physical"}',
        "direction": "below", "tiers": {"warning": 1}, "for": "5m", "unit": "",
        "summary": "The replica using slot {{ $labels.slot_name }} is not connected to {{ $labels.instance }}",
        "action": "The replica is down or can't reach the primary. Until it reconnects the primary keeps WAL for it, "
                  "up to max_slot_wal_keep_size. Check `systemctl status postgresql` and the log on the replica.",
        "quiet": True,
    },
    {
        "id": "pg-slot-wal-held", "module": "postgresql", "title": "WAL held for a PostgreSQL replica",
        # Share of max_slot_wal_keep_size the slot has used. Over 100 means the slot is past saving.
        "expr": "100 * (1 - pg_replication_slots_safe_wal_size_bytes"
                " / on (instance) group_left pg_settings_max_slot_wal_keep_size_bytes)",
        "direction": "above", "tiers": {"info": 25, "warning": 50, "critical": 80}, "for": "5m", "unit": "%",
        "summary": "Slot {{ $labels.slot_name }} on {{ $labels.instance }} holds {{ printf \"%.0f\" $values.A.Value }}% of the WAL it may keep",
        "action": "The replica is down or far behind. At 100% PostgreSQL gives the slot up to save the primary's disk, "
                  "and the replica has to be rebuilt. Bring the replica back, or if it's gone for good run "
                  "`SELECT pg_drop_replication_slot('<slot>')` on the primary to free the WAL now.",
        "quiet": True,
    },
    {
        "id": "pg-slot-lost", "module": "postgresql", "title": "PostgreSQL replica can't catch up",
        "expr": 'pg_replication_slots_wal_status{wal_status="lost"}',
        "direction": "above", "tiers": {"critical": 0}, "for": "1m", "unit": "",
        "summary": "Slot {{ $labels.slot_name }} on {{ $labels.instance }} has lost WAL its replica still needs",
        "action": "The replica can't catch up any more, so rebuild it. On the replica, stop postgresql, move the data "
                  "directory aside and run install.sh again. It drops the old slot and takes a fresh copy.",
        "quiet": True,
    },

    # ------------------------------------------------------------------ nginx
    {
        "id": "nginx-down", "module": "nginx", "title": "Nginx down",
        "expr": "nginx_up",
        "direction": "below", "tiers": {"critical": 1}, "for": "1m", "unit": "",
        "summary": "nginx_exporter on {{ $labels.instance }} can't reach Nginx",
        "action": "Check `systemctl status nginx` and `nginx -t`.",
    },
    {
        "id": "nginx-dropped", "module": "nginx", "title": "Nginx dropping connections",
        "expr": "rate(nginx_connections_accepted[5m]) - rate(nginx_connections_handled[5m])",
        "direction": "above", "tiers": {"warning": 0, "critical": 5}, "for": "5m", "unit": "/s",
        "summary": "Nginx on {{ $labels.instance }} is dropping {{ printf \"%.1f\" $values.A.Value }} connections a second",
        "action": "Nginx has run out of worker connections or file descriptors. Raise worker_connections and LimitNOFILE.",
    },

    # ------------------------------------------------------------------ opensearch
    {
        "id": "os-unreachable", "module": "opensearch", "title": "OpenSearch not answering",
        "expr": "elasticsearch_clusterinfo_up",
        "direction": "below", "tiers": {"critical": 1}, "for": "2m", "unit": "",
        "summary": "The exporter on {{ $labels.instance }} can't get cluster health from OpenSearch",
        "action": "Search falls back to the database or fails. Check `systemctl status opensearch` and its log.",
    },
    {
        "id": "os-red", "module": "opensearch", "title": "OpenSearch cluster red",
        "expr": 'elasticsearch_cluster_health_status{color="red"}',
        "direction": "above", "tiers": {"critical": 0}, "for": "2m", "unit": "",
        "summary": "The OpenSearch cluster on {{ $labels.instance }} is red",
        "action": "Some primary shards are missing, so search results are incomplete. Check `_cluster/allocation/explain`.",
    },
    {
        "id": "os-yellow", "module": "opensearch", "title": "OpenSearch cluster yellow",
        "expr": 'elasticsearch_cluster_health_status{color="yellow"}',
        "direction": "above", "tiers": {"info": 0}, "for": "30m", "unit": "",
        "summary": "The OpenSearch cluster on {{ $labels.instance }} has been yellow for 30 minutes",
        "action": "Replica shards have nowhere to go. Normal on a single node; on a cluster it means a node is missing.",
    },
    {
        "id": "os-heap", "module": "opensearch", "title": "OpenSearch JVM heap",
        "expr": '100 * elasticsearch_jvm_memory_used_bytes{area="heap"} / elasticsearch_jvm_memory_max_bytes{area="heap"}',
        "direction": "above", "tiers": {"info": 75, "warning": 85, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "OpenSearch on {{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of its heap",
        "action": "Sustained high heap means long GC pauses and then crashes. Raise opensearch_heap_mb if the host has the RAM.",
    },
    {
        "id": "os-disk", "module": "opensearch", "title": "OpenSearch data disk",
        "expr": "100 * (1 - elasticsearch_filesystem_data_available_bytes / elasticsearch_filesystem_data_size_bytes)",
        "direction": "above", "tiers": {"info": 75, "warning": 85, "critical": 90}, "for": "10m", "unit": "%",
        "summary": "OpenSearch data on {{ $labels.instance }} is {{ printf \"%.0f\" $values.A.Value }}% full",
        "action": "At 85% OpenSearch stops placing replicas, at 90% it moves shards away and at 95% indices go read-only.",
    },
    {
        "id": "os-rejections", "module": "opensearch", "title": "OpenSearch rejecting work",
        "expr": 'sum by (instance) (rate(elasticsearch_thread_pool_rejected_count{type=~"search|write"}[5m]))',
        "direction": "above", "tiers": {"warning": 0, "critical": 5}, "for": "5m", "unit": "/s",
        "summary": "OpenSearch on {{ $labels.instance }} is rejecting {{ printf \"%.1f\" $values.A.Value }} requests a second",
        "action": "Searches or index updates are being dropped. The node is overloaded; add CPU or heap.",
    },

    # ------------------------------------------------------------------ calls (rtcd)
    {
        "id": "rtcd-cpu", "module": "rtcd", "title": "rtcd CPU usage",
        "expr": '100 * rate(rtcd_process_cpu_seconds_total[5m]) '
                '/ on (instance) count by (instance) (node_cpu_seconds_total{mode="idle"})',
        "direction": "above", "tiers": {"info": 60, "warning": 80, "critical": 95}, "for": "10m", "unit": "%",
        "summary": "rtcd on {{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of the host's CPU",
        "action": "Calls will start to stutter. Add CPU or another rtcd server.",
    },
    {
        "id": "rtcd-fds", "module": "rtcd", "title": "rtcd open file descriptors",
        "expr": "100 * rtcd_process_open_fds / rtcd_process_max_fds",
        "direction": "above", "tiers": {"info": 70, "warning": 80, "critical": 90}, "for": "10m", "unit": "%",
        "summary": "rtcd on {{ $labels.instance }} is using {{ printf \"%.0f\" $values.A.Value }}% of its file descriptors",
        "action": "rtcd opens a socket per connection. Raise LimitNOFILE in the rtcd unit, or add another rtcd server.",
    },
    {
        "id": "udp-buffer-errors", "module": "rtcd", "title": "UDP receive buffer overflows",
        "expr": "rate(node_netstat_Udp_RcvbufErrors[5m])",
        "direction": "above", "tiers": {"warning": 0, "critical": 50}, "for": "5m", "unit": "/s",
        "summary": "{{ $labels.instance }} is dropping {{ printf \"%.1f\" $values.A.Value }} UDP packets a second",
        "action": "Media packets are being lost before rtcd reads them. Check net.core.rmem_max and CPU on the host.",
    },
    {
        "id": "rtcd-errors", "module": "rtcd", "title": "rtcd media errors",
        "expr": "sum by (instance) (increase(rtcd_rtc_errors_total[15m]))",
        "direction": "above", "tiers": {"info": 10, "warning": 50, "critical": 200}, "for": "0s", "unit": "",
        "summary": "rtcd on {{ $labels.instance }} logged {{ printf \"%.0f\" $values.A.Value }} media errors in 15 minutes",
        "action": "Check `journalctl -u rtcd` for the error types. Network paths and ICE settings are the usual cause.",
        "quiet": True,
    },
    {
        "id": "rtcd-packet-loss", "module": "rtcd", "title": "Calls packet loss reported by clients",
        "expr": "100 * histogram_quantile(0.95, sum by (le, instance) (rate(rtcd_rtc_client_loss_rate_bucket[10m])))",
        "direction": "above", "tiers": {"info": 2, "warning": 5, "critical": 10}, "for": "10m", "unit": "%",
        "summary": "The worst 5% of calls on {{ $labels.instance }} lose {{ printf \"%.1f\" $values.A.Value }}% of packets",
        "action": "Users will hear choppy audio. Check bandwidth to the rtcd server and UDP buffer overflows.",
        "quiet": True,
    },

    # ------------------------------------------------------------------ the monitoring itself
    {
        "id": "prometheus-config", "module": "monitoring", "title": "Prometheus config reload failed",
        "expr": "prometheus_config_last_reload_successful",
        "direction": "below", "tiers": {"warning": 1}, "for": "5m", "unit": "",
        "summary": "Prometheus on {{ $labels.instance }} is running an old config",
        "action": "Run `/opt/prometheus/promtool check config /etc/prometheus/prometheus.yml`, then fix and reload.",
    },
    {
        "id": "prometheus-scrape-slow", "module": "monitoring", "title": "Scrapes taking too long",
        "expr": "scrape_duration_seconds",
        "direction": "above", "tiers": {"info": 5, "warning": 10}, "for": "15m", "unit": " s",
        "summary": "Scraping {{ $labels.job }} on {{ $labels.instance }} takes {{ printf \"%.1f\" $values.A.Value }} s",
        "action": "The exporter or the network path is struggling. Dashboards and alerts lag behind reality.",
    },
]

# ------------------------------------------------------------------ log pipeline (Prometheus)
CATALOGUE += [
    {
        "id": "log-ship-failing", "module": "monitoring", "title": "Logs failing to reach Loki",
        "requires": "logs",
        # Counter names gained a _total suffix in newer collector releases.
        "expr": 'sum by (instance) (rate({__name__=~"otelcol_exporter_send_failed_log_records(_total)?"}[5m]))',
        "direction": "above", "tiers": {"warning": 0}, "for": "10m", "unit": "/s",
        "summary": "The log shipper on {{ $labels.instance }} can't deliver to Loki",
        "action": "Lines queue on disk in /var/lib/otelcol for up to an hour, then they're lost. Check Loki is up and "
                  "that the monitoring server's firewall lets this server in on port 3100 (`journalctl -u otelcol -n 50`).",
        "quiet": True,
    },
    {
        "id": "loki-discarding", "module": "monitoring", "title": "Loki refusing log lines",
        "requires": "logs",
        "expr": "sum by (reason) (rate(loki_discarded_samples_total[10m]))",
        "direction": "above", "tiers": {"warning": 0}, "for": "10m", "unit": "/s",
        "summary": "Loki is discarding log lines ({{ $labels.reason }})",
        "action": "Log alerts can't see lines Loki throws away. rate_limited or stream_limit: raise the limits in "
                  "/etc/loki/config.yml or ship less. greater_than_max_sample_age: a collector is sending week-old lines; "
                  "check its clock.",
        "quiet": True,
    },
]

SEVERITIES = ("info", "warning", "critical")


# ------------------------------------------------------------------ log checks (Loki)
#
# Each entry is one failure mode, matched on what the logs actually say. There is no
# "any error" rule: Mattermost and PostgreSQL log harmless errors all day. "Errors went
# up" is judged against the same time last week instead (kind: baseline), and a brand
# new kind of error only raises an info alert (kind: new).
#
# Extra fields on top of the ones above:
#   source      service_name of the stream: mattermost | postgresql | nginx | opensearch |
#               kernel, or None for every stream (systemd's messages about any unit)
#   parser      "json" to parse each line (Mattermost and PostgreSQL jsonlog), or None
#   match       [(field, regex)] that must all match. field "line" is a substring search
#               of the raw line; any other field is a parsed JSON field and must match
#               whole. Regexes stay in the subset RE2 and Python agree on, so the tests can
#               run them on the examples.
#   exclude     [(field, regex)] that must not match
#   by          labels to group by on top of host_name (one alert per value)
#   window      how far back each evaluation counts
#   kind        count (default), baseline (now / a week ago), new (not seen in a week),
#               silent (servers that sent nothing; built from the estate)
#   examples / counter_examples
#               real lines this rule must and must not match. tests/ checks both, and the
#               e2e self-test pushes the examples into Loki and runs the rule against them.
#
# %(name)s in a regex is filled from the estate: db_port, data_dir (both regex-escaped).

def _mm(level, msg, caller, **fields):
    """A Mattermost JSON log line, in the field order Mattermost writes."""
    line = {"timestamp": "2026-11-09 10:14:03.512 Z", "level": level, "msg": msg, "caller": caller}
    line.update(fields)
    return json.dumps(line, separators=(",", ":"))


def _pg(severity, code, message, **fields):
    """A PostgreSQL jsonlog line (PG 15+), with the fields PostgreSQL writes for a client backend.
    PostgreSQL leaves out fields that don't apply; pass None to drop one."""
    line = {"timestamp": "2026-11-09 10:14:03.512 UTC", "user": "mmuser", "dbname": "mattermost", "pid": 4242,
            "remote_host": "10.10.1.12", "remote_port": 50412, "session_id": "672f2b1b.1092", "line_num": 3,
            "ps": "SELECT", "session_start": "2026-11-09 10:02:03 UTC", "vxid": "5/1203", "txid": 0,
            "error_severity": severity, "state_code": code, "message": message}
    line.update(fields)
    for key, value in (("application_name", ""), ("backend_type", "client backend"), ("query_id", 0)):
        line.setdefault(key, value)
    return json.dumps({k: v for k, v in line.items() if v is not None}, separators=(",", ":"))


MM_DB_ERRORS = (r"Failed to ping DB|pq: sorry, too many clients|pq: the database system is "
                r"(shutting down|starting up|in recovery mode)|driver: bad connection|"
                r"dial tcp [^ \"]+:%(db_port)s: connect: connection refused")
MM_STORAGE_ERRORS = (r"no space left on device|read-only file system|%(data_dir)s[^\"]*: permission denied|"
                     r"Access Denied\.|The specified bucket does not exist|"
                     r"The AWS Access Key Id you provided does not exist|The request signature we calculated does not match")
PG_ERROR = ("error_severity", "ERROR|FATAL|PANIC")

LOG_CATALOGUE = [
    # ------------------------------------------------------------------ mattermost
    {
        "id": "mm-log-fatal", "module": "mattermost", "title": "Mattermost logged a fatal error",
        "source": "mattermost", "parser": "json", "match": [("level", "fatal|panic|critical")],
        "window": "5m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.host_name }} logged {{ printf \"%.0f\" $values.A.Value }} fatal or critical entries",
        "action": "Mattermost stops, or loses a part of itself, after one of these. Read the lines, then "
                  "`journalctl -u mattermost -n 100` on the server.",
        "examples": [_mm("fatal", "Error starting server", "commands/server.go:120",
                         error="failed to initialize platform: cannot create store: dial tcp 10.10.1.11:5432: connect: connection refused")],
        "counter_examples": [_mm("error", "Failed to ping DB", "sql/sql_utils.go:55", error="context deadline exceeded")],
    },
    {
        "id": "mm-go-panic", "module": "mattermost", "title": "Mattermost crashed (Go panic)",
        # Only on stderr, so only in the journal: mattermost.log never sees it.
        "source": "mattermost", "parser": None, "match": [("line", r"^(panic: |fatal error: )")],
        "window": "10m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.host_name }} crashed with a Go panic",
        "action": "systemd restarts it, but the crash will come back. Save the stack trace "
                  "(`journalctl -u mattermost --since -15m`) and send it to Mattermost support with the version.",
        "examples": ["panic: runtime error: invalid memory address or nil pointer dereference",
                     "fatal error: concurrent map writes"],
        "counter_examples": ["goroutine 1 [running]:", _mm("error", "panic: recovered in handler", "web/handlers.go:150")],
    },
    {
        "id": "mm-log-db", "module": "mattermost", "title": "Mattermost can't use its database",
        "source": "mattermost", "parser": "json",
        "match": [("line", MM_DB_ERRORS), ("level", "error|critical|fatal")],
        "window": "10m", "direction": "above", "tiers": {"warning": 10, "critical": 100}, "for": "2m", "unit": "",
        "summary": "Mattermost on {{ $labels.host_name }} logged {{ printf \"%.0f\" $values.A.Value }} database connection errors in 10 minutes",
        "action": "Check PostgreSQL is up and accepting connections from this server, and max_connections. "
                  "This is the only database alert when the database is outside the monitored estate.",
        "examples": [
            _mm("error", "Failed to ping DB", "sql/sql_utils.go:55", database="master", **{"retrying in seconds": 10},
                error="dial tcp 10.10.1.11:5432: connect: connection refused"),
            _mm("error", "Unable to get the channel", "app/channel.go:2101",
                error="failed to find Channel: pq: sorry, too many clients already"),
        ],
        "counter_examples": [_mm("info", "Pinging SQL", "sql/sql_utils.go:46", database="master", attempt=1),
                             _mm("error", "Failed to send", "app/x.go:1", error="dial tcp 10.0.0.25:587: connect: connection refused")],
    },
    {
        "id": "mm-log-storage", "module": "mattermost", "title": "Mattermost can't write uploaded files",
        "source": "mattermost", "parser": "json",
        "match": [("line", MM_STORAGE_ERRORS), ("level", "warn|error|critical|fatal")],
        "window": "10m", "direction": "above", "tiers": {"warning": 0, "critical": 20}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.host_name }} failed to write to its file store {{ printf \"%.0f\" $values.A.Value }} times",
        "action": "Uploads are failing. For local or NFS storage check free space, mounts and ownership of the data "
                  "directory. For S3 check the bucket, the keys and the endpoint.",
        "examples": [_mm("error", "Unable to upload file(s).", "web/context.go:131", path="/api/v4/files", err_where="WriteFile",
                         http_code=500, error="WriteFile: Encountered an error writing to file storage., open "
                         "/var/opt/mattermost/data/20261109/teams/noteam/a.png: no space left on device")],
        "counter_examples": [_mm("debug", "You do not have the appropriate permissions.", "web/context.go:126", http_code=403)],
    },
    {
        "id": "mm-log-plugin-crash", "module": "mattermost", "title": "Mattermost plugin crashing",
        "source": "mattermost", "parser": "json", "by": ["id"],
        "match": [("msg", "Health check failed for plugin|Failed to restart plugin")],
        "window": "1h", "direction": "above", "tiers": {"warning": 0, "critical": 2}, "for": "0s", "unit": "",
        "summary": "Plugin {{ $labels.id }} on {{ $labels.host_name }} failed {{ printf \"%.0f\" $values.A.Value }} health checks in an hour",
        "action": "Mattermost restarts a crashed plugin, and disables it after repeated crashes. Check the plugin's "
                  "own log lines just before, and its version against the server version.",
        "examples": [_mm("warn", "Health check failed for plugin", "plugin/health_check.go:59", id="com.mattermost.calls",
                         error="connection is shut down")],
        "counter_examples": [_mm("debug", "Plugin health check job starting.", "plugin/health_check.go:31")],
    },
    {
        "id": "mm-log-smtp", "module": "mattermost", "title": "Mattermost can't send email",
        "source": "mattermost", "parser": "json",
        "match": [("line", "unable to connect to the SMTP server|unable to send hello message|Error while sending the email"),
                  ("level", "warn|error")],
        "window": "30m", "direction": "above", "tiers": {"warning": 5}, "for": "0s", "unit": "",
        "summary": "Mattermost on {{ $labels.host_name }} failed to send {{ printf \"%.0f\" $values.A.Value }} emails in 30 minutes",
        "action": "Notifications, invites and password resets aren't going out. Check the SMTP settings in the System "
                  "Console and that this server can reach the mail server.",
        "examples": [_mm("error", "Error while sending the email", "email/email_batching.go:271",
                         error="unable to connect to the SMTP server: dial tcp 10.0.0.25:587: i/o timeout")],
        "counter_examples": [_mm("warn", "Unable to convert html body to text", "mail/mail.go:320")],
    },
    {
        "id": "mm-log-search-index", "module": "mattermost", "title": "Search indexing failing",
        "source": "mattermost", "parser": "json",
        "match": [("msg", "Worker: Failed to index batch for job|Worker: Failed to setup bulk processor")],
        "window": "15m", "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "Search indexing failed on {{ $labels.host_name }} ({{ printf \"%.0f\" $values.A.Value }} batches)",
        "action": "New messages won't show in search results. Check OpenSearch is green and has disk space, then run a "
                  "bulk index from System Console > Environment > Elasticsearch.",
        "examples": [_mm("error", "Worker: Failed to index batch for job", "common/indexing_job.go:296",
                         error="request failed: 429 Too Many Requests")],
        "counter_examples": [_mm("warn", "Worker: Error occurred while trying to claim job", "common/indexing_job.go:232")],
    },
    {
        "id": "mm-log-jobs", "module": "mattermost", "title": "Mattermost background job failed",
        "source": "mattermost", "parser": "json", "match": [("msg", ".*job execution error")],
        "window": "1h", "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "A background job failed on {{ $labels.host_name }}",
        "action": "Data retention, compliance exports, LDAP sync and the like run as jobs. Find which one failed in "
                  "System Console > Jobs or in the lines around this one.",
        "examples": [_mm("error", "SimpleWorker: job execution error", "jobs/base_workers.go:86", worker="ExportDelete",
                         error="failed to delete export files")],
        "counter_examples": [_mm("warn", "SimpleWorker experienced an error while trying to claim job", "jobs/base_workers.go:78")],
    },
    {
        "id": "mm-log-error-spike", "module": "mattermost", "title": "Mattermost errors well above last week",
        "source": "mattermost", "parser": "json", "match": [("level", "error")], "kind": "baseline",
        "window": "30m", "direction": "above", "tiers": {"info": 3, "warning": 5, "critical": 10}, "for": "15m", "unit": "x",
        "summary": "Mattermost on {{ $labels.host_name }} is logging {{ printf \"%.1f\" $values.A.Value }}x the errors of this time last week",
        "action": "Something changed. Look at the top error messages now (Explore, or the Mattermost logs dashboard) and "
                  "compare with last week. Add known, accepted errors to logging_alert_ignore.",
        "examples": [_mm("error", "Unable to get the channel", "app/channel.go:2101", error="not found")],
        "counter_examples": [_mm("warn", "Unable to get the channel", "app/channel.go:2101")],
    },
    {
        "id": "mm-log-new-error", "module": "mattermost", "title": "New kind of Mattermost error",
        "source": "mattermost", "parser": "json", "match": [("level", "error")], "kind": "new", "by": ["msg"],
        "window": "1h", "direction": "above", "tiers": {"info": 0}, "for": "0s", "unit": "",
        "summary": "Mattermost logged an error not seen in the last week: {{ $labels.msg }}",
        "action": "Usually the first sign of a config change, an upgrade or a failing integration. Harmless ones can go "
                  "in logging_alert_ignore. This rule stays quiet for the first week after Loki is installed.",
        "examples": [_mm("error", "Failed to post webhook", "app/webhook.go:120", error="context deadline exceeded")],
        "counter_examples": [_mm("info", "Server is started", "app/server.go:1201")],
    },

    # ------------------------------------------------------------------ postgresql (jsonlog)
    {
        "id": "pg-log-corruption", "module": "postgresql", "title": "PostgreSQL data corruption or PANIC",
        "source": "postgresql", "parser": "json",
        "match": [("line", r'"state_code":\s*"XX00[12]"|"error_severity":\s*"PANIC"')],
        "window": "10m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "PostgreSQL on {{ $labels.host_name }} reported corruption or a PANIC",
        "action": "Stop and think before restarting anything. Check the storage (`dmesg`), take a copy of the data "
                  "directory, and plan a restore from backup for the affected table or index.",
        "examples": [_pg("ERROR", "XX001", "invalid page in block 4123 of relation base/16384/16521"),
                     _pg("PANIC", "XX000", "could not locate a valid checkpoint record")],
        "counter_examples": [_pg("ERROR", "23505", "duplicate key value violates unique constraint \"idx_posts\"")],
    },
    {
        "id": "pg-log-io", "module": "postgresql", "title": "PostgreSQL I/O or disk full errors",
        "source": "postgresql", "parser": "json", "match": [("state_code", "58...|53100")],
        "window": "10m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "PostgreSQL on {{ $labels.host_name }} can't read or write its files",
        "action": "Free space on the data volume first (WAL in pg_wal is the usual culprit when a replica is down), "
                  "then check the storage for errors.",
        "examples": [_pg("ERROR", "53100", "could not extend file \"base/16384/16521\": No space left on device"),
                     _pg("ERROR", "58030", "could not read block 12 in file \"base/16384/16521\": Input/output error")],
        "counter_examples": [_pg("ERROR", "57014", "canceling statement due to statement timeout")],
    },
    {
        "id": "pg-log-conn-refused", "module": "postgresql", "title": "PostgreSQL refusing connections",
        "source": "postgresql", "parser": "json", "match": [("state_code", "53300")],
        "window": "5m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "PostgreSQL on {{ $labels.host_name }} turned away {{ printf \"%.0f\" $values.A.Value }} connections: too many clients",
        "action": "Requests are failing now. Look for idle sessions in pg_stat_activity and app servers with oversized "
                  "pools. pg-connections is the early warning for this.",
        "examples": [_pg("FATAL", "53300", "sorry, too many clients already"),
                     _pg("FATAL", "53300", "remaining connection slots are reserved for roles with the SUPERUSER attribute")],
        "counter_examples": [_pg("FATAL", "28P01", "password authentication failed for user \"mmuser\"")],
    },
    {
        "id": "pg-log-auth-failures", "module": "postgresql", "title": "PostgreSQL login failures",
        "source": "postgresql", "parser": "json", "match": [("state_code", "28P01|28000")], "by": ["remote_host"],
        "window": "10m", "direction": "above", "tiers": {"warning": 5, "critical": 50}, "for": "0s", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} failed logins to PostgreSQL on {{ $labels.host_name }} from {{ $labels.remote_host }}",
        "action": "An app server with an old password after a rotation, or someone guessing. Known app servers: fix "
                  "mattermost_db_password there. Unknown addresses: tighten postgresql_allowed_clients.",
        "examples": [_pg("FATAL", "28P01", "password authentication failed for user \"mmuser\""),
                     _pg("FATAL", "28000", "no pg_hba.conf entry for host \"10.10.9.9\", user \"mmuser\", database \"mattermost\", SSL encryption")],
        "counter_examples": [_pg("LOG", "00000", "connection authorized: user=mmuser database=mattermost")],
    },
    {
        "id": "pg-log-crash-recovery", "module": "postgresql", "title": "PostgreSQL recovered from a crash",
        "source": "postgresql", "parser": "json",
        "match": [("line", "database system was (not properly shut down|interrupted)")],
        "window": "30m", "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "PostgreSQL on {{ $labels.host_name }} started after a crash or power loss",
        "action": "It replayed WAL and carried on, so data should be intact. Find out why it went down: "
                  "`journalctl -u postgresql -b -1`, the OOM killer, or a host reboot.",
        "examples": [_pg("LOG", "00000", "database system was not properly shut down; automatic recovery in progress",
                         user=None, dbname=None, remote_host=None, remote_port=None, ps=None, application_name=None,
                         backend_type="startup")],
        "counter_examples": [_pg("LOG", "00000", "database system is ready to accept connections")],
    },
    {
        "id": "pg-log-checkpoints", "module": "postgresql", "title": "PostgreSQL checkpoints too frequent",
        "source": "postgresql", "parser": "json", "match": [("line", "checkpoints are occurring too frequently")],
        "window": "1h", "direction": "above", "tiers": {"info": 0}, "for": "0s", "unit": "",
        "summary": "PostgreSQL on {{ $labels.host_name }} is checkpointing more often than it should",
        "action": "Write-heavy periods (imports, bulk deletes) fill WAL faster than max_wal_size allows. Raise max_wal_size "
                  "if this keeps happening.",
        "examples": [_pg("LOG", "00000", "checkpoints are occurring too frequently (12 seconds apart)",
                         hint="Consider increasing the configuration parameter \"max_wal_size\".")],
        "counter_examples": [_pg("LOG", "00000", "checkpoint complete: wrote 1203 buffers (7.3%)")],
    },
    {
        "id": "pg-log-error-spike", "module": "postgresql", "title": "PostgreSQL errors well above last week",
        "source": "postgresql", "parser": "json", "match": [PG_ERROR], "kind": "baseline",
        # Unique violations are normal for Mattermost; deadlocks and logins have their own checks;
        # admin shutdowns happen on every restart.
        "exclude": [("state_code", "23...|40P01|28...|57P0[1-3]")],
        "window": "30m", "direction": "above", "tiers": {"info": 3, "warning": 5, "critical": 10}, "for": "15m", "unit": "x",
        "summary": "PostgreSQL on {{ $labels.host_name }} is logging {{ printf \"%.1f\" $values.A.Value }}x the errors of this time last week",
        "action": "Group the errors by state_code in Explore to see what changed. A new Mattermost version or a "
                  "failing integration are the usual causes.",
        "examples": [_pg("ERROR", "57014", "canceling statement due to statement timeout")],
        "counter_examples": [_pg("ERROR", "23505", "duplicate key value violates unique constraint \"idx_posts\"")],
    },

    # ------------------------------------------------------------------ every server (journal)
    {
        "id": "unit-crash-loop", "module": "host", "title": "Service restarting over and over",
        "source": None, "parser": None, "by": ["unit"],
        "match": [("line", "Scheduled restart job, restart counter is at")],
        "window": "15m", "direction": "above", "tiers": {"critical": 2}, "for": "0s", "unit": "",
        "summary": "{{ $labels.unit }} on {{ $labels.host_name }} restarted {{ printf \"%.0f\" $values.A.Value }} times in 15 minutes",
        "action": "systemd keeps restarting it, so it may look up between checks. Read why it exits: "
                  "`journalctl -u {{ $labels.unit }} -n 200`.",
        "examples": ["mattermost.service: Scheduled restart job, restart counter is at 3."],
        "counter_examples": ["Started mattermost.service - Mattermost."],
    },
    {
        "id": "unit-start-limit", "module": "host", "title": "systemd gave up restarting a service",
        "source": None, "parser": None, "by": ["unit"],
        "match": [("line", "Start request repeated too quickly")],
        "window": "15m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "systemd stopped trying to start {{ $labels.unit }} on {{ $labels.host_name }}",
        "action": "It stays down until someone acts. Fix the cause (`journalctl -u {{ $labels.unit }} -n 200`), then "
                  "`systemctl reset-failed {{ $labels.unit }}` and start it.",
        "examples": ["rtcd.service: Start request repeated too quickly."],
        "counter_examples": ["rtcd.service: Scheduled restart job, restart counter is at 1."],
    },
    {
        "id": "unit-core-dump", "module": "host", "title": "Service crashed with a core dump",
        "source": None, "parser": None, "by": ["unit"],
        "match": [("line", "Main process exited, code=dumped")],
        "window": "15m", "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "{{ $labels.unit }} on {{ $labels.host_name }} crashed (core dumped)",
        "action": "A segfault or abort, not a clean exit. `coredumpctl list` shows the dump; keep it for the vendor.",
        "examples": ["rtcd.service: Main process exited, code=dumped, status=11/SEGV"],
        "counter_examples": ["rtcd.service: Main process exited, code=exited, status=0/SUCCESS"],
    },
    {
        "id": "kernel-io-errors", "module": "host", "title": "Kernel disk or filesystem errors",
        "source": "kernel", "parser": None,
        "match": [("line", r"I/O error|EXT4-fs error|XFS \([^)]+\):.*([Ee]rror|[Cc]orruption)|"
                           r"Remounting filesystem read-only|Aborting journal")],
        "window": "15m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "The kernel on {{ $labels.host_name }} reported disk or filesystem errors",
        "action": "Storage is failing or a filesystem is damaged. Read `dmesg -T | tail -50` and call the storage team "
                  "before PostgreSQL or Mattermost write anything else there.",
        "examples": ["blk_update_request: I/O error, dev sdb, sector 2048 op 0x1:(WRITE) flags 0x800 phys_seg 1 prio class 0",
                     "EXT4-fs error (device dm-2): ext4_find_entry:1658: inode #2: comm mattermost: reading directory lblock 0",
                     "XFS (dm-0): Metadata corruption detected at xfs_buf_ioend+0x51/0x1c0"],
        "counter_examples": ["EXT4-fs (dm-2): mounted filesystem with ordered data mode. Quota mode: none."],
    },

    # ------------------------------------------------------------------ nginx (error.log)
    {
        "id": "nginx-log-upstream-down", "module": "nginx", "title": "Nginx can't reach Mattermost",
        "source": "nginx", "parser": None,
        "match": [("line", r"connect\(\) failed \(111: Connection refused\) while connecting to upstream|"
                           r"no live upstreams while connecting to upstream")],
        "window": "5m", "direction": "above", "tiers": {"warning": 10, "critical": 100}, "for": "2m", "unit": "",
        "summary": "Nginx on {{ $labels.host_name }} got {{ printf \"%.0f\" $values.A.Value }} refused connections from Mattermost in 5 minutes",
        "action": "Users are getting 502s. Check Mattermost is running on the upstream servers and listening on 8065.",
        "examples": ['2026/11/09 10:14:03 [error] 1234#1234: *5678 connect() failed (111: Connection refused) while '
                     'connecting to upstream, client: 10.0.0.5, server: chat.example.com, request: "GET /api/v4/users/me '
                     'HTTP/1.1", upstream: "http://127.0.0.1:8065/api/v4/users/me", host: "chat.example.com"'],
        "counter_examples": ['2026/11/09 10:14:03 [info] 1234#1234: *5678 client closed connection while waiting for request'],
    },
    {
        "id": "nginx-log-upstream-slow", "module": "nginx", "title": "Mattermost too slow to answer Nginx",
        "source": "nginx", "parser": None,
        "match": [("line", r"upstream timed out \(110: Connection timed out\) while reading response header from upstream")],
        "window": "10m", "direction": "above", "tiers": {"warning": 10}, "for": "0s", "unit": "",
        "summary": "{{ printf \"%.0f\" $values.A.Value }} requests timed out waiting for Mattermost on {{ $labels.host_name }}",
        "action": "Users see 504s. Usually slow database queries; check PostgreSQL CPU and long-running queries.",
        "examples": ['2026/11/09 10:14:03 [error] 1234#1234: *5678 upstream timed out (110: Connection timed out) while '
                     'reading response header from upstream, client: 10.0.0.5, server: chat.example.com'],
        "counter_examples": ['2026/11/09 10:14:03 [error] 1234#1234: *5678 upstream timed out (110: Connection timed out) '
                             'while reading upstream, client: 10.0.0.5'],
    },
    {
        "id": "nginx-log-limits", "module": "nginx", "title": "Nginx out of connections or files",
        "source": "nginx", "parser": None, "match": [("line", "worker_connections are not enough|Too many open files")],
        "window": "10m", "direction": "above", "tiers": {"warning": 0}, "for": "0s", "unit": "",
        "summary": "Nginx on {{ $labels.host_name }} ran out of worker connections or file descriptors",
        "action": "This is why connections get dropped. Raise worker_connections and LimitNOFILE for Nginx.",
        "examples": ["2026/11/09 10:14:03 [alert] 1234#1234: 1024 worker_connections are not enough"],
        "counter_examples": ["2026/11/09 10:14:03 [notice] 1234#1234: signal process started"],
    },

    # ------------------------------------------------------------------ opensearch
    {
        "id": "os-log-oom", "module": "opensearch", "title": "OpenSearch ran out of heap",
        "source": "opensearch", "parser": None, "match": [("line", r"java\.lang\.OutOfMemoryError")],
        "window": "15m", "direction": "above", "tiers": {"critical": 0}, "for": "0s", "unit": "",
        "summary": "OpenSearch on {{ $labels.host_name }} hit an OutOfMemoryError",
        "action": "The JVM is unstable after this; restart OpenSearch, then raise opensearch_heap_mb or add memory.",
        "examples": ["[2026-11-09T10:14:03,512][ERROR][o.o.b.OpenSearchUncaughtExceptionHandler] [search01] fatal error "
                     "in thread [opensearch[search01][write][T#2]], exiting java.lang.OutOfMemoryError: Java heap space"],
        "counter_examples": ["[2026-11-09T10:14:03,512][WARN ][o.o.m.j.JvmGcMonitorService] [search01] [gc][1203] overhead, "
                             "spent [1.2s] collecting in the last [1.5s]"],
    },
    {
        "id": "os-log-circuit-breaker", "module": "opensearch", "title": "OpenSearch circuit breaker tripping",
        "source": "opensearch", "parser": None, "match": [("line", "CircuitBreakingException")],
        "window": "10m", "direction": "above", "tiers": {"warning": 5}, "for": "0s", "unit": "",
        "summary": "OpenSearch on {{ $labels.host_name }} refused {{ printf \"%.0f\" $values.A.Value }} requests to protect its heap",
        "action": "Searches or indexing are being refused. The heap is too small for the load; raise opensearch_heap_mb.",
        "examples": ["[2026-11-09T10:14:03,512][WARN ][r.suppressed] [search01] path: /posts/_search, params: {} "
                     "org.opensearch.core.common.breaker.CircuitBreakingException: [parent] Data too large"],
        "counter_examples": ["[2026-11-09T10:14:03,512][WARN ][o.o.c.r.a.DiskThresholdMonitor] [search01] high disk watermark exceeded"],
    },

    # ------------------------------------------------------------------ the log pipeline itself
    {
        "id": "log-host-silent", "module": "monitoring", "title": "Server stopped sending logs",
        "source": None, "parser": None, "match": [], "kind": "silent",
        "window": "15m", "direction": "below", "tiers": {"warning": 1}, "for": "15m", "unit": "",
        "summary": "No logs from {{ $labels.host_name }} for 15 minutes",
        "action": "The log alerts are blind for this server. Check `systemctl status otelcol` on it, that "
                  "logging_loki_url points here, and that otel_collector_host_name matches its name in monitoring_estate.",
        "examples": [], "counter_examples": [],
    },
]
