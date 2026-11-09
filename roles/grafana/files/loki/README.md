`mattermost-loki-logs.json` is the logs dashboard from the Mattermost docs guide
"Deploy Grafana Loki for centralized logging" (`source/samples/grafana-dashboards/` in
mattermost/docs). It's kept in the repo so offline installs get it too. At install time
the role swaps `${DS_LOKI}` for the provisioned Loki datasource UID. To refresh it:

    gh api repos/mattermost/docs/contents/source/samples/grafana-dashboards/mattermost-loki-logs.json \
      -H "Accept: application/vnd.github.raw" > mattermost-loki-logs.json
