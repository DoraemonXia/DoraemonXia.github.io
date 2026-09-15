# Publication updates

GitHub Actions runs daily at 04:00 UTC (12:00 Beijing time), or manually via
**Actions → Update Publications → Run workflow**. GitHub may delay scheduled runs.

- Public works from ORCID `0009-0000-7567-6978` establish ownership. Add a new
  work to this ORCID record and make it public to include it in the next update.
- Semantic Scholar author `2296580567` supplies citation counts for matching
  records. Unmatched Scholar records are not automatically published. Matching
  uses DOI/arXiv identifiers; exact normalized titles are a fallback only when
  one record lacks stable identifiers. Email/name similarity alone is not used.
- `papers.json` retains manually confirmed publications, formatting and explicit
  preprint-to-publication aliases. Add an alias here when versions have different
  identifiers and titles; they cannot always be merged automatically.
- `publications-cache.json` stores confirmed bibliographic records and citation
  counts (no email fields or credentials). Previously confirmed works remain
  even if absent from a later ORCID response. To deliberately remove a work,
  remove it from ORCID, this cache, and the curated list if applicable.
- An empty, incomplete or unavailable ORCID response fails the update without
  changing the page/cache. Scholar failures produce a warning and use cached
  citations. Both sources use bounded retries and honor Retry-After.

The optional repository secret `SEMANTIC_SCHOLAR_API_KEY` enables authenticated
Scholar requests. Public ORCID reads currently require no secret.

Run `python -m unittest discover -s scripts -p 'test_*.py'` for regression tests.
Run `python scripts/update_papers.py --check` to fetch and validate without
writing files, or omit `--check` to regenerate the page and cache.
