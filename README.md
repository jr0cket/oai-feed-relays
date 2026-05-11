# OAI feed relays

Scheduled GitHub Actions that fetch TSML meeting feeds and commit them to this repo, so ottawaaa.org can consume them from `raw.githubusercontent.com`.

This exists because some upstream AA hosts block SiteGround's outbound IP at the firewall layer, preventing direct server-to-server fetches.

## How it works

- `.github/workflows/fetch-feeds.yml` runs every 6 hours
- For each matrix entry, it fetches the URL, validates the JSON (list of meetings, each with a `slug`), and writes to `<name>.json`
- If the file changed, the workflow commits and pushes

## Adding a new feed

Add an entry to the matrix in `fetch-feeds.yml`:

```yaml
- name: my-source
  url: https://example.org/wp-admin/admin-ajax.php?action=meetings
```

Then in OAI's TSML data source, use:

```
https://raw.githubusercontent.com/<owner>/oai-feed-relays/main/my-source.json
```

## Current relays

| Name | Upstream | Reason |
|---|---|---|
| `lanark-leeds` | lanarkleedsaa.org | SiteGround IP blocked at the firewall |
