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
https://raw.githubusercontent.com/jr0cket/oai-feed-relays/main/my-source.json
```

## Current relays

| Name | Upstream | Reason |
|---|---|---|
| `lanark-leeds` | lanarkleedsaa.org | SiteGround IP blocked at the firewall |

---

## District site monitor

Three AA district websites have no TSML feed. OAI mirrors their meeting data
manually and needs a human to re-sync when anything changes.

`.github/workflows/check-district-sites.yml` runs daily at 08:30 UTC and:

1. Fetches each site (HTML or PDF as needed)
2. Strips markup / extracts PDF text and normalises whitespace
3. Diffs against `snapshots/<slug>.txt`
4. If changed: overwrites the snapshot and writes `reports/YYYY-MM-DD-<slug>.md`
5. Commits and pushes any changes
6. Creates a **private** WordPress post on ottawaaa.org so the webmaster sees it

### Monitored sites

| Slug | District | URL | Format |
|---|---|---|---|
| `cornwall` | D50 | cornwallaa.ca/meetings | HTML (Wix) |
| `renfrew` | D70 | aarenfrew.org/meetings-2/ | HTML (WordPress) |
| `madawaska` | — | aamadawaskavalley.org/meetings/ | PDF (linked from page) |

### First run

On the first run snapshots don't exist yet. The workflow writes baselines and
commits them with message `Initial snapshots: <slugs>`. No change report is
generated for the first run.

### GitHub Secrets required

Add these in **Settings → Secrets → Actions** on this repo:

| Secret | Value |
|---|---|
| `WP_USERNAME` | `jr0c@me.com` |
| `WP_APP_PASSWORD` | the WordPress application password (spaces included or stripped — both work) |

If the secrets are absent the WordPress notification step is skipped silently
(the site checks and commits still run).

### Running locally

```bash
# Requires: curl, python3, pdftotext (poppler-utils)
bash scripts/check-sites.sh
```

Reports are written to `reports/` and snapshots updated in `snapshots/`.
Commit manually if you run it outside Actions.
