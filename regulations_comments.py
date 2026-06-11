#!/usr/bin/env python3
"""
Fetch, categorize, and visualize public comments for FDA-2025-N-6895-0001
(Pharmacy Compounding Advisory Committee / Section 503A Bulk Drug Substances).
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
import anthropic

DOCUMENT_ID = "FDA-2025-N-6895-0001"
DOCUMENT_OBJECT_ID = "09000064b92865ab"
API_KEY = os.environ.get("REGULATIONS_GOV_API_KEY", "DEMO_KEY")
# Get a free API key at https://api.data.gov/signup/ and set REGULATIONS_GOV_API_KEY env var
# DEMO_KEY is limited to ~40 req/min and will be slow for large dockets
CACHE_FILE = Path("comments_cache.json")

CATEGORIES = [
    "Patient Access & Medical Need",
    "Safety & Quality Concerns",
    "Regulatory Process & FDA Authority",
    "Specific Drug Substance Support",
    "Specific Drug Substance Opposition",
    "Compounding Pharmacy Industry",
    "Healthcare Provider Perspective",
    "Scientific / Clinical Evidence",
    "Economic & Market Impact",
    "Other / General Comment",
]


def _api_get(url, label="request"):
    """GET with exponential backoff for rate limiting. Returns parsed JSON."""
    delay = 70
    for attempt in range(10):
        resp = requests.get(url, timeout=30)
        data = resp.json() if resp.ok else None
        if resp.status_code == 429 or (data and "error" in data):
            wait = delay * (2 ** min(attempt, 3))
            print(f"  Rate limited on {label}, waiting {wait}s...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return data
    raise RuntimeError(f"Failed after retries: {label}")


def fetch_all_comment_ids():
    """Fetch all comment IDs for the docket (handles pagination)."""
    ids = []
    page = 1
    while True:
        url = (
            f"https://api.regulations.gov/v4/comments"
            f"?filter[commentOnId]={DOCUMENT_OBJECT_ID}"
            f"&page[size]=25&page[number]={page}&api_key={API_KEY}"
        )
        data = _api_get(url, f"page {page}")
        batch = data.get("data", [])
        ids.extend(item["id"] for item in batch)
        meta = data.get("meta", {})
        print(f"  Page {page}: {len(batch)} comments (total so far: {len(ids)})", flush=True)
        if not meta.get("hasNextPage"):
            break
        page += 1
        time.sleep(4)
    return ids


def fetch_comment_detail(comment_id):
    """Fetch full text of a single comment."""
    url = f"https://api.regulations.gov/v4/comments/{comment_id}?api_key={API_KEY}&include=attachments"
    return _api_get(url, comment_id)


def load_or_fetch_comments():
    """Return list of comment dicts, using cache if available."""
    if CACHE_FILE.exists():
        print(f"Loading cached comments from {CACHE_FILE}", flush=True)
        with open(CACHE_FILE) as f:
            return json.load(f)

    print("Fetching comment IDs...", flush=True)
    ids = fetch_all_comment_ids()
    print(f"Found {len(ids)} comments. Fetching full text...", flush=True)

    comments = []
    for i, cid in enumerate(ids, 1):
        print(f"  [{i}/{len(ids)}] {cid}", flush=True)
        detail = fetch_comment_detail(cid)
        attrs = detail.get("data", {}).get("attributes", {})
        comments.append({
            "id": cid,
            "title": attrs.get("title", ""),
            "posted_date": attrs.get("postedDate", "")[:10] if attrs.get("postedDate") else "",
            "comment": attrs.get("comment", "") or "",
            "organization": attrs.get("organization", "") or "",
            "submitter_type": attrs.get("submitterType", "") or "",
            "city": attrs.get("city", "") or "",
            "state": attrs.get("stateProvinceRegion", "") or "",
        })
        time.sleep(4 if API_KEY != "DEMO_KEY" else 8)

    with open(CACHE_FILE, "w") as f:
        json.dump(comments, f, indent=2)
    print(f"Cached to {CACHE_FILE}", flush=True)
    return comments


def categorize_comments(comments):
    """Use Claude to categorize all comments at once."""
    client = anthropic.Anthropic()

    # Build a compact representation of all comments for Claude
    comment_summaries = []
    for c in comments:
        text = c["comment"][:500] if c["comment"] else c["title"]
        comment_summaries.append(f'ID:{c["id"]} | Org:"{c["organization"]}" | {text}')

    comments_text = "\n\n".join(comment_summaries)

    categories_list = "\n".join(f"- {cat}" for cat in CATEGORIES)

    prompt = f"""You are analyzing public comments filed with the FDA on docket FDA-2025-N-6895
regarding the Pharmacy Compounding Advisory Committee's review of bulk drug substances
nominated for the Section 503A Bulk Drug Substances List.

For EACH comment below, provide:
1. One primary category (must be exactly one from the list)
2. Up to 2 additional tags (from the list or short descriptive tags)
3. A 1-sentence summary (max 20 words)
4. Sentiment: support / oppose / neutral / mixed

Categories:
{categories_list}

Comments:
{comments_text}

Respond as JSON array:
[
  {{
    "id": "FDA-2025-N-6895-XXXX",
    "category": "exact category name",
    "tags": ["tag1", "tag2"],
    "summary": "one sentence summary",
    "sentiment": "support|oppose|neutral|mixed"
  }},
  ...
]

Return ONLY the JSON array, no other text."""

    print("Calling Claude to categorize comments...", flush=True)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    categorized = json.load(open("/dev/stdin") if False else __import__("io").StringIO(raw))
    return {item["id"]: item for item in categorized}


def generate_html(comments, categorized):
    """Generate a self-contained HTML report with charts."""

    # Merge categorization into comments
    enriched = []
    for c in comments:
        cat_info = categorized.get(c["id"], {})
        enriched.append({**c, **cat_info})

    # Compute stats
    from collections import Counter
    category_counts = Counter(e.get("category", "Other / General Comment") for e in enriched)
    sentiment_counts = Counter(e.get("sentiment", "neutral") for e in enriched)
    date_counts = Counter(e.get("posted_date", "") for e in enriched if e.get("posted_date"))
    submitter_type_counts = Counter(e.get("submitter_type", "Individual") or "Individual" for e in enriched)

    # Sort by date for timeline
    dates_sorted = sorted(date_counts.items())

    # Color palette
    colors = [
        "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
        "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"
    ]
    sentiment_colors = {
        "support": "#59a14f",
        "oppose": "#e15759",
        "neutral": "#4e79a7",
        "mixed": "#f28e2b",
    }

    cat_labels = list(category_counts.keys())
    cat_values = list(category_counts.values())

    sent_labels = list(sentiment_counts.keys())
    sent_values = list(sentiment_counts.values())
    sent_colors = [sentiment_colors.get(s, "#bab0ac") for s in sent_labels]

    timeline_labels = [d for d, _ in dates_sorted]
    timeline_values = [v for _, v in dates_sorted]

    # Build comment cards grouped by category
    by_category = {}
    for e in enriched:
        cat = e.get("category", "Other / General Comment")
        by_category.setdefault(cat, []).append(e)

    def sentiment_badge(s):
        badge_colors = {
            "support": "#d4edda", "oppose": "#f8d7da",
            "neutral": "#d1ecf1", "mixed": "#fff3cd"
        }
        text_colors = {
            "support": "#155724", "oppose": "#721c24",
            "neutral": "#0c5460", "mixed": "#856404"
        }
        bg = badge_colors.get(s, "#e2e3e5")
        tc = text_colors.get(s, "#383d41")
        return f'<span style="background:{bg};color:{tc};padding:2px 8px;border-radius:12px;font-size:0.75rem;font-weight:600">{s.upper()}</span>'

    cards_html = ""
    for cat in CATEGORIES:
        items = by_category.get(cat, [])
        if not items:
            continue
        cards_html += f'<div class="category-section" id="cat-{CATEGORIES.index(cat)}">'
        cards_html += f'<h3 class="cat-header">{cat} <span class="count-badge">{len(items)}</span></h3>'
        for item in items:
            tags_html = " ".join(
                f'<span class="tag">{t}</span>'
                for t in (item.get("tags") or [])
                if t != cat
            )
            org = item.get("organization") or ""
            org_html = f'<span class="org">{org}</span>' if org else ""
            state = item.get("state") or ""
            state_html = f'<span class="state">{state}</span>' if state else ""
            comment_preview = (item.get("comment") or "")[:300]
            if len(item.get("comment", "")) > 300:
                comment_preview += "..."
            cards_html += f"""
            <div class="comment-card">
              <div class="card-meta">
                <span class="comment-id">{item['id']}</span>
                {org_html}{state_html}
                <span class="date">{item.get('posted_date','')}</span>
                {sentiment_badge(item.get('sentiment','neutral'))}
              </div>
              <p class="summary"><strong>{item.get('summary','')}</strong></p>
              <p class="comment-text">{comment_preview}</p>
              <div class="tags">{tags_html}</div>
            </div>"""
        cards_html += "</div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FDA-2025-N-6895 Public Comments Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f6fa; color: #2d3436; }}
  .header {{ background: linear-gradient(135deg, #1a237e 0%, #283593 100%); color: white; padding: 2rem; }}
  .header h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
  .header p {{ opacity: 0.85; font-size: 0.95rem; max-width: 800px; }}
  .header .meta {{ margin-top: 1rem; display: flex; gap: 1.5rem; flex-wrap: wrap; }}
  .header .meta-item {{ background: rgba(255,255,255,0.15); padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.85rem; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem 1rem; }}
  .charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }}
  .chart-card {{ background: white; border-radius: 12px; padding: 1.5rem; box-shadow: 0 2px 8px rgba(0,0,0,0.07); }}
  .chart-card h2 {{ font-size: 1rem; color: #636e72; margin-bottom: 1rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }}
  .chart-wrapper {{ position: relative; height: 300px; }}
  .timeline-wrapper {{ position: relative; height: 200px; }}
  .full-width {{ grid-column: 1 / -1; }}
  .section-title {{ font-size: 1.25rem; font-weight: 700; margin: 2rem 0 1rem; color: #2d3436; }}
  .search-bar {{ margin-bottom: 1.5rem; }}
  .search-bar input {{ width: 100%; padding: 0.75rem 1rem; border: 1px solid #dfe6e9; border-radius: 8px; font-size: 0.95rem; outline: none; }}
  .search-bar input:focus {{ border-color: #1a237e; box-shadow: 0 0 0 3px rgba(26,35,126,0.1); }}
  .category-section {{ margin-bottom: 2rem; }}
  .cat-header {{ font-size: 1.1rem; font-weight: 700; color: #1a237e; margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem; }}
  .count-badge {{ background: #1a237e; color: white; border-radius: 12px; padding: 2px 10px; font-size: 0.8rem; font-weight: 600; }}
  .comment-card {{ background: white; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 0.75rem; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border-left: 3px solid #1a237e; }}
  .card-meta {{ display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.5rem; }}
  .comment-id {{ font-size: 0.75rem; color: #636e72; font-family: monospace; }}
  .org {{ font-size: 0.78rem; background: #e8eaf6; color: #283593; padding: 1px 7px; border-radius: 4px; font-weight: 600; }}
  .state {{ font-size: 0.78rem; background: #f3f4f6; color: #4b5563; padding: 1px 7px; border-radius: 4px; }}
  .date {{ font-size: 0.75rem; color: #b2bec3; margin-left: auto; }}
  .summary {{ font-size: 0.9rem; color: #2d3436; margin-bottom: 0.4rem; }}
  .comment-text {{ font-size: 0.82rem; color: #636e72; line-height: 1.5; margin-bottom: 0.5rem; }}
  .tags {{ display: flex; gap: 0.4rem; flex-wrap: wrap; }}
  .tag {{ font-size: 0.72rem; background: #f0f4ff; color: #1a237e; padding: 2px 8px; border-radius: 12px; border: 1px solid #c5cae9; }}
  .hidden {{ display: none !important; }}
  @media (max-width: 600px) {{ .header h1 {{ font-size: 1.1rem; }} .charts-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>FDA-2025-N-6895 &mdash; Public Comments Analysis</h1>
  <p>Pharmacy Compounding Advisory Committee: Bulk Drug Substances Nominated for the Section 503A List</p>
  <div class="meta">
    <div class="meta-item">📄 {len(enriched)} Total Comments</div>
    <div class="meta-item">📅 Comment Period: Apr 16 – Jul 23, 2026</div>
    <div class="meta-item">🏛 Agency: FDA / CDER</div>
    <div class="meta-item">⚡ AI-Categorized with Claude</div>
  </div>
</div>

<div class="container">
  <div class="charts-grid">
    <div class="chart-card">
      <h2>Comments by Category</h2>
      <div class="chart-wrapper"><canvas id="catChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Sentiment Distribution</h2>
      <div class="chart-wrapper"><canvas id="sentChart"></canvas></div>
    </div>
    <div class="chart-card full-width">
      <h2>Filing Timeline</h2>
      <div class="timeline-wrapper"><canvas id="timelineChart"></canvas></div>
    </div>
  </div>

  <div class="section-title">All Comments by Category</div>
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search comments by keyword, organization, or ID..." oninput="filterComments()">
  </div>
  <div id="commentsContainer">
    {cards_html}
  </div>
</div>

<script>
// Category bar chart
new Chart(document.getElementById('catChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(cat_labels)},
    datasets: [{{
      data: {json.dumps(cat_values)},
      backgroundColor: {json.dumps(colors[:len(cat_labels)])},
      borderRadius: 5,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ color: '#636e72' }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ color: '#636e72', font: {{ size: 11 }} }} }}
    }},
    responsive: true,
    maintainAspectRatio: false,
  }}
}});

// Sentiment donut
new Chart(document.getElementById('sentChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(sent_labels)},
    datasets: [{{
      data: {json.dumps(sent_values)},
      backgroundColor: {json.dumps(sent_colors)},
      borderWidth: 2,
      borderColor: '#fff',
    }}]
  }},
  options: {{
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ color: '#636e72', font: {{ size: 12 }} }} }}
    }},
    responsive: true,
    maintainAspectRatio: false,
    cutout: '60%',
  }}
}});

// Timeline
new Chart(document.getElementById('timelineChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(timeline_labels)},
    datasets: [{{
      label: 'Comments Filed',
      data: {json.dumps(timeline_values)},
      borderColor: '#1a237e',
      backgroundColor: 'rgba(26,35,126,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 4,
      pointBackgroundColor: '#1a237e',
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ color: '#636e72', maxTicksLimit: 10 }} }},
      y: {{ grid: {{ color: '#f0f0f0' }}, ticks: {{ color: '#636e72', stepSize: 1 }} }}
    }},
    responsive: true,
    maintainAspectRatio: false,
  }}
}});

function filterComments() {{
  const q = document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('.comment-card').forEach(card => {{
    card.classList.toggle('hidden', q.length > 0 && !card.textContent.toLowerCase().includes(q));
  }});
  document.querySelectorAll('.category-section').forEach(sec => {{
    const visible = sec.querySelectorAll('.comment-card:not(.hidden)').length;
    sec.classList.toggle('hidden', visible === 0 && q.length > 0);
  }});
}}
</script>
</body>
</html>"""
    return html


def main():
    if API_KEY == "DEMO_KEY":
        print("NOTE: Using DEMO_KEY (rate-limited). For faster results, get a free key at")
        print("      https://api.data.gov/signup/  then: export REGULATIONS_GOV_API_KEY=your_key\n")

    output_file = Path("comments_report.html")

    # 1. Fetch or load comments
    comments = load_or_fetch_comments()
    if not comments:
        print("No comments found.", file=sys.stderr)
        sys.exit(1)
    print(f"Loaded {len(comments)} comments.", flush=True)

    # 2. Categorize with Claude
    cat_cache = Path("categorized_cache.json")
    if cat_cache.exists():
        print(f"Loading categorization cache from {cat_cache}", flush=True)
        with open(cat_cache) as f:
            categorized = json.load(f)
    else:
        categorized = categorize_comments(comments)
        with open(cat_cache, "w") as f:
            json.dump(categorized, f, indent=2)
        print(f"Categorization cached to {cat_cache}", flush=True)

    # 3. Generate HTML report
    html = generate_html(comments, categorized)
    output_file.write_text(html, encoding="utf-8")
    print(f"\nReport saved to {output_file}")
    print(f"Open with: open {output_file}")


if __name__ == "__main__":
    main()
