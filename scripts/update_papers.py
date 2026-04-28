"""
Auto-update publications section from Semantic Scholar API.
Runs via GitHub Actions daily at midnight (Beijing time).

Semantic Scholar Author ID: 2296580567
API docs: https://api.semanticscholar.org/api-docs/graph
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError

AUTHOR_ID = "2296580567"
API_URL = f"https://api.semanticscholar.org/graph/v1/author/{AUTHOR_ID}/papers"
FIELDS = "title,authors,year,venue,journal,externalIds,url,publicationDate,citationCount"
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "index.html")
PAPERS_JSON = os.path.join(os.path.dirname(__file__), "papers.json")

START_MARKER = "<!-- PAPERS_START -->"
END_MARKER = "<!-- PAPERS_END -->"


def fetch_papers():
    """Fetch papers from Semantic Scholar API with retry."""
    url = f"{API_URL}?fields={FIELDS}&limit=100"
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "AcademicWebsite/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                return data.get("data", [])
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(5)
    print("Failed to fetch papers after 3 attempts.")
    return None


def load_known_papers():
    """Load manually curated paper config."""
    if os.path.exists(PAPERS_JSON):
        with open(PAPERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_paper_key(paper):
    """Derive a stable key from Semantic Scholar paper data."""
    title = paper.get("title", "").strip().lower()
    # Use DOI or first 50 chars of title as key
    ext = paper.get("externalIds", {}) or {}
    doi = ext.get("DOI", "")
    if doi:
        return f"doi:{doi}"
    return f"title:{title[:60]}"


def generate_paper_html(paper, known):
    """Generate HTML block for a single paper using known config if available."""
    key = get_paper_key(paper)
    info = known.get(key, {})

    # Follow alias chain (for preprint → published mappings)
    if "_alias_for" in info:
        info = known.get(info["_alias_for"], {})

    title = info.get("title") or paper.get("title", "Unknown Title")
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
        authors_html = ", ".join(author_names) if author_names else ""

    # Build venue line
    if not venue_text:
        journal = paper.get("journal") or {}
        journal_name = journal.get("name", "") if isinstance(journal, dict) else str(journal)
        venue_info = paper.get("venue", "")
        pub_date = paper.get("publicationDate", "")
        if pub_date:
            pub_date = pub_date[:7]  # YYYY-MM
        parts = [p for p in [journal_name, venue_info, pub_date] if p]
        venue_text = " · ".join(parts)
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
        paper_url = paper.get("url", "")
        if paper_url:
            links_html += f"""
        <a href="{paper_url}" class="link-paper">
          <i class="fas fa-file-lines"></i> Paper
        </a>"""
        doi = ext.get("DOI", "")
        if doi:
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
        <span class="venue-tag">Updated via Semantic Scholar</span>
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

    new_content = pattern.sub(replacement, content)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def main():
    print(f"Fetching papers for author {AUTHOR_ID}...")
    papers = fetch_papers()

    if papers is None:
        print("ERROR: Could not fetch papers.")
        sys.exit(1)

    print(f"Found {len(papers)} papers from Semantic Scholar.")
    known = load_known_papers()

    # Sort by year descending
    papers.sort(key=lambda p: p.get("year", 0) or 0, reverse=True)

    # Generate HTML; skip aliased duplicates
    html_blocks = []
    new_papers = []
    seen_keys = set()
    for paper in papers:
        key = get_paper_key(paper)
        info = known.get(key, {})
        # Skip preprint if it's an alias for a published version
        if "_alias_for" in info:
            alias_target = info["_alias_for"]
            if alias_target in seen_keys:
                continue
            seen_keys.add(key)
            html_blocks.append(generate_paper_html(paper, known))
            continue
        if key not in known:
            new_papers.append(paper.get("title", "Untitled"))
        seen_keys.add(key)
        html_blocks.append(generate_paper_html(paper, known))

    papers_html = "\n\n".join(html_blocks)

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
        print(f"Updated {INDEX_PATH} successfully.")
        print(f"Timestamp: {datetime.now().isoformat()}")
    else:
        print("Failed to update index.html")
        sys.exit(1)


if __name__ == "__main__":
    main()
