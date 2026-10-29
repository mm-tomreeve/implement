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

Thresholds and "for" can be changed per site with monitoring_alert_overrides in the
answer file, and any rule switched off with monitoring_alerts_disabled.
"""

FS = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|nsfs|ramfs|autofs|fuse.*",mountpoint!~"/(run|dev|sys|proc)($|/.*)|/etc/.+"'
# Virtual and tunnel interfaces the kernel creates on its own; never interesting.
NETDEV = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*|gre.*|gretap.*|erspan.*|ip6.*|sit.*|tunl.*|ip_vti.*|ip6_vti.*|nbd.*"'
BLOCKDEV = 'device!~"dm-.*|loop.*|sr.*|nbd.*|ram.*|zram.*"'

CATALOGUE = [
    # ------------------------------------------------------------------ every server
    {
        "id": "target-down", "module": "host", "title": "Metrics target down",
        "expr": 'up{job!~"blackbox.*"}',
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

SEVERITIES = ("info", "warning", "critical")
