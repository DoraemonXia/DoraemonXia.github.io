"""
Auto-update ORCID-confirmed publications; Semantic Scholar enriches citations.
Runs via GitHub Actions daily at noon (Beijing time).

Semantic Scholar Author ID: 2296580567
API docs: https://api.semanticscholar.org/api-docs/graph
"""

import json
from html import escape
import os
import re
import random
import sys
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ORCID_ID = "0009-0000-7567-6978"
ORCID_API = f"https://pub.orcid.org/v3.0/{ORCID_ID}"
CACHE_PATH = os.path.join(os.path.dirname(__file__), "publications-cache.json")

AUTHOR_ID = "2296580567"
API_URL = f"https://api.semanticscholar.org/graph/v1/author/{AUTHOR_ID}/papers"
FIELDS = "title,authors,year,venue,journal,externalIds,url,publicationDate,citationCount"
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "index.html")
PAPERS_JSON = os.path.join(os.path.dirname(__file__), "papers.json")

START_MARKER = "<!-- PAPERS_START -->"
END_MARKER = "<!-- PAPERS_END -->"


def retry_delay(attempt, retry_after=None):
    """Use exponential backoff and respect the server's Retry-After header."""
    delay = min(30 * 2 ** attempt, 300) + random.uniform(0, 5)
    if retry_after:
        try:
            server_delay = float(retry_after)
        except ValueError:
            try:
                server_delay = (
                    parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                server_delay = 0
        delay = max(delay, server_delay)
    return delay


def fetch_papers():
    """Retry transient API failures without modifying the site on failure."""
    url = f"{API_URL}?fields={FIELDS}&limit=100"
    headers = {"User-Agent": "AcademicWebsite/1.0"}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    attempts = 6
    for attempt in range(attempts):
        retry_after = None
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise ValueError("API response is missing the papers list")
                return data["data"]
        except HTTPError as e:
            print(f"Attempt {attempt + 1} failed: HTTP {e.code}", flush=True)
            if e.code not in (408, 429, 500, 502, 503, 504):
                break
            retry_after = e.headers.get("Retry-After") if e.headers else None
        except (URLError, TimeoutError, ConnectionError) as e:
            print(f"Attempt {attempt + 1} failed: {e}", flush=True)
        except ValueError as e:
            print(f"Invalid API response: {e}", flush=True)
            break
        if attempt < attempts - 1:
            delay = retry_delay(attempt, retry_after)
            print(f"Retrying in {delay:.1f} seconds...", flush=True)
            time.sleep(delay)
    print("Failed to fetch papers; existing publications will be preserved.")
    return None


def fetch_orcid_json(path):
    for attempt in range(3):
        try:
            req = Request(f"{ORCID_API}/{path}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except (URLError, TimeoutError, ConnectionError, ValueError) as error:
            print(f"ORCID request failed: {error}", flush=True)
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                break
            if attempt < 2:
                retry_after = error.headers.get("Retry-After") if isinstance(error, HTTPError) and error.headers else None
                time.sleep(retry_delay(attempt, retry_after))
    return None


def parse_orcid_work(work):
    title = ((work.get("title") or {}).get("title") or {}).get("value")
    if not title:
        raise ValueError("ORCID work is missing its title")
    ids = {}
    for item in (work.get("external-ids") or {}).get("external-id", []):
        if item.get("external-id-relationship") != "self":
            continue
        kind = {"doi": "DOI", "arxiv": "ArXiv"}.get(item.get("external-id-type"))
        value = item.get("external-id-value")
        if kind and value:
            value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
            ids[kind] = value
    date = work.get("publication-date") or {}
    year = (date.get("year") or {}).get("value")
    parts = [(date.get(key) or {}).get("value") for key in ("year", "month", "day")]
    authors = [{"name": (c.get("credit-name") or {}).get("value")} for c in
               (work.get("contributors") or {}).get("contributor", [])
               if (c.get("credit-name") or {}).get("value")]
    return {"title": title, "externalIds": ids, "year": int(year) if year else None,
            "publicationDate": "-".join(v for v in parts if v),
            "venue": (work.get("journal-title") or {}).get("value") or "",
            "url": f"https://doi.org/{ids['DOI']}" if ids.get("DOI") else f"https://orcid.org/{ORCID_ID}",
            "authors": authors, "_orcid": ORCID_ID}


def fetch_orcid_papers():
    data = fetch_orcid_json("works")
    if not isinstance(data, dict) or not isinstance(data.get("group"), list) or not data["group"]:
        return None
    papers = []
    try:
        for group in data["group"]:
            summaries = group["work-summary"]
            summary = max(summaries, key=lambda w: int(w.get("display-index") or 0))
            detail = fetch_orcid_json(f"work/{summary['put-code']}")
            if detail is None:
                return None
            papers.append(parse_orcid_work(detail))
    except (KeyError, TypeError, ValueError) as error:
        print(f"Invalid ORCID response: {error}")
        return None
    return papers


def same_work(left, right):
    # If both records have stable identifiers, a matching title alone is insufficient.
    left_ids = {i for i in paper_identities(left) if i.startswith(("doi:", "arxiv:"))}
    right_ids = {i for i in paper_identities(right) if i.startswith(("doi:", "arxiv:"))}
    if left_ids and right_ids:
        return bool(left_ids & right_ids)
    return bool(normalize_title(left.get("title") or "")) and normalize_title(left["title"]) == normalize_title(right.get("title") or "")


def enrich_confirmed_papers(orcid_papers, scholar_papers, cached):
    confirmed = []
    for paper in orcid_papers:
        enriched = dict(paper)
        previous = next((p for p in cached if same_work(paper, p)), {})
        match = next((p for p in scholar_papers if same_work(paper, p)), {})
        citation_count = match.get("citationCount", previous.get("citationCount"))
        if citation_count is not None:
            enriched["citationCount"] = citation_count
        if not enriched.get("authors"):
            enriched["authors"] = previous.get("authors") or match.get("authors") or []
        confirmed.append(enriched)
    for paper in cached:
        if not any(same_work(paper, current) for current in confirmed):
            confirmed.append(paper)
            print(f"Retaining previously confirmed publication: {paper['title']}")
    excluded = sum(not any(same_work(p, c) for c in confirmed) for p in scholar_papers)
    print(f"Excluded {excluded} Semantic Scholar records without confirmed ownership.")
    return confirmed


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return []
    with open(CACHE_PATH, encoding="utf-8") as source:
        data = json.load(source)
    if data.get("orcid") != ORCID_ID:
        raise ValueError("Publication cache belongs to a different ORCID")
    return data["papers"]


def load_known_papers():
    """Load manually curated paper config."""
    if os.path.exists(PAPERS_JSON):
        with open(PAPERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def normalize_title(title):
    return "".join(c for c in unicodedata.normalize("NFKC", title).casefold() if c.isalnum())


def get_paper_key(paper):
    """Use the complete title when a DOI is unavailable."""
    doi = (paper.get("externalIds") or {}).get("DOI")
    if doi:
        return f"doi:{doi.strip().lower()}"
    return f"title:{normalize_title(paper.get('title') or '')}"


def normalize_key(key):
    kind, value = key.split(":", 1)
    return f"{kind}:{normalize_title(value) if kind == 'title' else value.strip().lower()}"


def paper_identities(paper):
    ids = set()
    title = normalize_title(paper.get("title") or "")
    if title:
        ids.add(f"title:{title}")
    for kind, value in (paper.get("externalIds") or {}).items():
        if value and kind in ("DOI", "ArXiv"):
            ids.add(f"{kind.lower()}:{str(value).strip().lower()}")
    if paper.get("paperId"):
        ids.add(f"s2:{paper['paperId']}")
    return ids


def prepare_papers(papers, known):
    """Merge versions by identifiers/title and retain curated publications."""
    normalized = {normalize_key(key): key for key in known}

    def canonical(key):
        seen = set()
        while "_alias_for" in known[key]:
            if key in seen:
                raise ValueError(f"Cyclic paper alias: {key}")
            seen.add(key)
            key = normalized[normalize_key(known[key]["_alias_for"])]
        return key

    identity_map = {}
    for key, info in known.items():
        target = canonical(key)
        for identity in {normalize_key(key)} | paper_identities(info):
            identity_map[identity] = target

    candidates = []
    for paper in papers:
        paper = dict(paper)
        identities = paper_identities(paper)
        key = identity_map.get(get_paper_key(paper))
        if key is None:
            key = next((identity_map[i] for i in sorted(identities) if i in identity_map), None)
        if key is not None:
            paper["_known_key"] = key
            identities.add(f"known:{key}")
        candidates.append((paper, identities))

    for key, info in known.items():
        if "_alias_for" not in info:
            paper = {**info, "_known_key": key, "_curated_only": True}
            candidates.append((paper, paper_identities(info) | {f"known:{key}"}))

    groups = []
    for paper, identities in candidates:
        members = [paper]
        remaining = []
        for group_ids, group_members in groups:
            if identities & group_ids:
                identities |= group_ids
                members.extend(group_members)
            else:
                remaining.append((group_ids, group_members))
        groups = remaining + [(identities, members)]

    result = []
    for _, members in groups:
        def preference(paper):
            key = paper.get("_known_key")
            return (
                not paper.get("_curated_only", False),
                key is not None and get_paper_key(paper) == normalize_key(key),
                paper.get("citationCount") or 0,
                paper.get("paperId") or "",
            )
        selected = dict(max(members, key=preference))
        key = next((p["_known_key"] for p in members if "_known_key" in p), None)
        if key is not None:
            selected["_known_key"] = key
            selected["year"] = known[key].get("year") or selected.get("year")
        result.append(selected)
    return sorted(result, key=lambda p: (-(p.get("year") or 0), normalize_title(p.get("title") or "")))


def generate_paper_html(paper, known):
    """Generate HTML block for a single paper using known config if available."""
    key = paper.get("_known_key", get_paper_key(paper))
    info = known.get(key, {})

    # Follow alias chain (for preprint → published mappings)
    if "_alias_for" in info:
        info = known.get(info["_alias_for"], {})

    title = escape(info.get("title") or paper.get("title", "Unknown Title"))
    authors_raw = info.get("authors") or ""
    venue_text = info.get("venue") or ""
    year = paper.get("year", "")
    tags = " ".join(info.get("tags", []))
    image = info.get("image", "")
    links = info.get("links", [])

    # Build author HTML
    if authors_raw:
        authors_html = authors_raw
    else:
        author_names = [a.get("name", "") for a in paper.get("authors", [])]
        authors_html = ", ".join(f"<strong>{escape(n)}</strong>" if n == "Yunpeng Xia" else escape(n) for n in author_names)

    # Build venue line
    if not venue_text:
        journal = paper.get("journal") or {}
        journal_name = journal.get("name", "") if isinstance(journal, dict) else str(journal)
        venue_info = paper.get("venue", "")
        pub_date = paper.get("publicationDate", "")
        if pub_date:
            pub_date = pub_date[:7]  # YYYY-MM
        parts = [p for p in [journal_name, venue_info, pub_date] if p]
        venue_text = escape(" · ".join(parts))
        if year:
            venue_text += f" ({year})"

    # Image
    thumb_html = ""
    if image:
        thumb_html = f"""<div class="paper-thumb">
      <img src="{image}" alt="thumbnail">
    </div>"""

    # Links
    links_html = ""
    for link in links:
        cls = link.get("class", "link-paper")
        icon = link.get("icon", "file-lines")
        label = link.get("label", "Link")
        url = link.get("url", "#")
        # github icon uses 'fab' prefix, others use 'fas'
        prefix = "fab" if icon == "github" else "fas"
        links_html += f"""
        <a href="{url}" class="{cls}">
          <i class="{prefix} fa-{icon}"></i> {label}
        </a>"""

    # External links from API (if no manual links)
    if not links:
        ext = paper.get("externalIds", {}) or {}
        paper_url = escape(paper.get("url", ""), quote=True)
        if paper_url:
            links_html += f"""
        <a href="{paper_url}" class="link-paper">
          <i class="fas fa-file-lines"></i> Paper
        </a>"""
        doi = escape(ext.get("DOI", ""), quote=True)
        if doi and paper_url != f"https://doi.org/{doi}":
            links_html += f"""
        <a href="https://doi.org/{doi}" class="link-code">
          <i class="fas fa-link"></i> DOI
        </a>"""

    citation_count = paper.get("citationCount", 0)
    cite_str = f" · Cited {citation_count} times" if citation_count else ""

    return f"""  <div class="paper-item" data-tags="{tags}">
    {thumb_html}
    <div class="paper-body">
      <h3>{title}</h3>
      <div class="paper-authors">
        {authors_html}
      </div>
      <div class="paper-venue">
        {venue_text}{cite_str}
      </div>
      <div class="paper-links">{links_html}
      </div>
    </div>
  </div>"""


def update_index(papers_html):
    """Replace papers section in index.html."""
    if not os.path.exists(INDEX_PATH):
        print(f"ERROR: {INDEX_PATH} not found")
        return False

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(
        f"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        re.DOTALL,
    )
    replacement = f"{START_MARKER}\n\n{papers_html}\n\n  {END_MARKER}"

    if not pattern.search(content):
        print(f"ERROR: Markers not found in {INDEX_PATH}")
        return False

    new_content = pattern.sub(lambda match: replacement, content)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def main():
    print(f"Fetching confirmed publications from ORCID {ORCID_ID}...", flush=True)
    orcid_papers = fetch_orcid_papers()
    if orcid_papers is None:
        print("ERROR: ORCID unavailable or incomplete; existing page and cache preserved.")
        sys.exit(1)
    print(f"Found {len(orcid_papers)} ORCID records.", flush=True)
    scholar_papers = fetch_papers()
    if scholar_papers is None:
        print("::warning::Semantic Scholar unavailable; using ORCID and cached citation counts.")
    confirmed = enrich_confirmed_papers(orcid_papers, scholar_papers or [], load_cache())
    papers = confirmed
    known = load_known_papers()

    papers = prepare_papers(papers, known)
    print(f"Displaying {len(papers)} unique publications after merging curated records.")
    html_blocks = [generate_paper_html(paper, known) for paper in papers]
    new_papers = [paper.get("title", "Untitled") for paper in papers if "_known_key" not in paper]

    papers_html = "\n".join(line.rstrip() for line in "\n\n".join(html_blocks).splitlines())

    if new_papers:
        print(f"\n--- NEW PAPERS DETECTED ({len(new_papers)}) ---")
        for t in new_papers:
            print(f"  * {t}")
        print("Add them to scripts/papers.json for full details.\n")

    # Dry run check
    if "--check" in sys.argv:
        print("\nDry run mode. No changes made.")
        print("Run without --check to update index.html.")
        return

    if update_index(papers_html):
        with open(CACHE_PATH, "w", encoding="utf-8") as target:
            json.dump({"orcid": ORCID_ID, "papers": confirmed}, target, ensure_ascii=False, indent=2)
            target.write("\n")
        print(f"Updated {INDEX_PATH} successfully.")
        print(f"Timestamp: {datetime.now().isoformat()}")
    else:
        print("Failed to update index.html")
        sys.exit(1)


if __name__ == "__main__":
    main()
