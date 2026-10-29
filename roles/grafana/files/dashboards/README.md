These are Mattermost's published Grafana dashboards, taken from grafana.com:

- 15582 Mattermost Performance Monitoring v2
- 15581 Collapsed Reply Threads Performance

They are kept in the repo so offline installs get them too. At install time the
role swaps `${DS_PROMETHEUS}` for the provisioned datasource UID. To refresh them:

    curl -sL https://grafana.com/api/dashboards/15582/revisions/latest/download -o mattermost-15582.json
