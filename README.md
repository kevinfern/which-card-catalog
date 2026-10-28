# which-card-catalog

The rewards catalog for the Which Card iOS app, kept up to date automatically.

- `base_catalog.json`: cards, categories and fixed rewards. Edit by hand when adding a card.
- `sources.json`: web pages the job reads for rotating rewards (currently Discover).
- `build_catalog.py`: downloads each page, has Claude extract the categories as structured
  data, checks the result, and merges it into the catalog.
- `.github/workflows/update-catalog.yml`: runs the script every Monday and can be run by hand
  from the Actions tab. It needs the `ANTHROPIC_API_KEY` repository secret.
- `docs/catalog.json`: the published file the app downloads (served with GitHub Pages).

If a check fails, nothing is published and the last good `docs/catalog.json` stays live.
GitHub emails the repo owner when a run fails.
