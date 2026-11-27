# Daily Paper Index

Every day, GitHub Actions collects everything a set of open scholarly sources listed for the previous UTC day (preprints, papers, reports and white papers, theses) and publishes a searchable index page.

- `collect.py`: the collector (Python standard library only). `python collect.py --date 2026-11-25`, or `--days-ago 1,4,8`, `--reindex`, or `--only arxiv,hal` to test sources.
- `docs/index.html`: the index page. It reads `docs/data/index.json` and `docs/data/<date>.json`.
- `.github/workflows/daily.yml`: runs at 06:23 UTC. It indexes yesterday and re-collects 4 and 8 days ago (arXiv lists submissions only once announced; Crossref/DataCite/OpenAlex deposits arrive late), commits the data and deploys GitHub Pages.

Sources: arXiv, bioRxiv, medRxiv, PhilArchive, PhilSci-Archive, IACR ePrint, HAL, Zenodo, Crossref (posted content, reports, dissertations), DataCite, OpenAlex (dissertations, reports).

Optional repository secret: `OPENALEX_API_KEY`.
