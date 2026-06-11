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

DOCUMENT_ID = "FDA-2025-N-6895-0001"
DOCUMENT_OBJECT_ID = "09000064b92865ab"
API_KEY = os.environ.get("REGULATIONS_GOV_API_KEY", "DEMO_KEY")
# Get a free API key at https://api.data.gov/signup/ and set REGULATIONS_GOV_API_KEY env var
# DEMO_KEY is limited to ~40 req/min and will be slow for large dockets
CACHE_FILE = Path("comments_cache.json")

CATEGORIES = [
    "Patient Access",
    "Safety & Quality",
    "Regulatory Process",
    "Drug Substance Support",
    "Drug Substance Opposition",
    "Compounding Pharmacy",
    "Healthcare Provider",
    "Scientific Evidence",
    "Economic Impact",
    "Other / General",
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
    """Categorize comments using keyword matching — no API key needed."""
    print("Categorizing comments...", flush=True)

    keyword_map = [
        ("Patient Access & Medical Need", [
            "patient", "access", "need", "condition", "disease", "treatment",
            "therapy", "doctor", "prescription", "medical", "health", "suffer",
            "pain", "chronic", "life", "quality of life", "affordable"
        ]),
        ("Safety & Quality Concerns", [
            "safe", "safety", "risk", "harm", "danger", "adverse", "side effect",
            "contamination", "sterile", "quality", "standard", "concern", "protect"
        ]),
        ("Specific Drug Substance Support", [
            "support", "include", "approve", "add", "list", "allow", "should be",
            "necessary", "effective", "beneficial", "important", "critical"
        ]),
        ("Specific Drug Substance Opposition", [
            "oppose", "remove", "exclude", "ban", "prohibit", "dangerous",
            "unnecessary", "should not", "reject", "deny", "against"
        ]),
        ("Scientific / Clinical Evidence", [
            "study", "research", "evidence", "clinical", "trial", "data", "literature",
            "published", "science", "scientific", "pharmacology", "efficacy", "result"
        ]),
        ("Regulatory Process & FDA Authority", [
            "fda", "regulation", "regulatory", "authority", "law", "statute",
            "fdca", "503a", "503b", "rule", "guidance", "policy", "process",
            "docket", "comment", "federal", "congress"
        ]),
        ("Compounding Pharmacy Industry", [
            "compound", "compounding", "pharmacy", "pharmacist", "compounder",
            "503a", "503b", "bulk", "outsourcing", "facility", "pcab"
        ]),
        ("Healthcare Provider Perspective", [
            "physician", "prescriber", "provider", "practitioner", "nurse",
            "veterinarian", "vet", "clinic", "hospital", "practice", "patient care"
        ]),
        ("Economic & Market Impact", [
            "cost", "price", "market", "commercial", "manufacturer", "industry",
            "business", "economic", "afford", "insurance", "supply", "shortage"
        ]),
    ]

    oppose_words = ["oppose", "against", "reject", "ban", "prohibit", "dangerous",
                    "should not", "unnecessary", "remove", "exclude", "deny"]
    support_words = ["support", "approve", "include", "allow", "important", "necessary",
                     "beneficial", "effective", "critical", "need", "access"]

    PEPTIDE_KEYWORDS = {
        "TB-500": ["tb-500", "tb500"],
        "BPC-157": ["bpc-157", "bpc157", "bpc 157"],
    }

    results = {}
    for c in comments:
        text = (c.get("comment") or c.get("title") or "").lower()

        # Score each category
        scores = {}
        for cat, keywords in keyword_map:
            scores[cat] = sum(1 for kw in keywords if kw in text)

        # Pick top category
        best_cat = max(scores, key=scores.get)
        if scores[best_cat] == 0:
            best_cat = "Other / General Comment"

        # Pick top 2 tags (next highest scoring categories)
        sorted_cats = sorted(scores, key=scores.get, reverse=True)
        tags = [c for c in sorted_cats[1:3] if scores[c] > 0]

        # Flag peptide mentions (stored separately, not mixed into category tags)
        peptides_mentioned = [
            peptide for peptide, kws in PEPTIDE_KEYWORDS.items()
            if any(kw in text for kw in kws)
        ]

        # Sentiment
        opp_score = sum(1 for w in oppose_words if w in text)
        sup_score = sum(1 for w in support_words if w in text)
        if opp_score > sup_score:
            sentiment = "oppose"
        elif sup_score > opp_score:
            sentiment = "support"
        elif opp_score > 0 and sup_score > 0:
            sentiment = "mixed"
        else:
            sentiment = "neutral"

        # Summary — first 120 chars of comment
        raw = (c.get("comment") or c.get("title") or "No comment text available.")
        summary = raw[:120].strip()
        if len(raw) > 120:
            summary += "..."

        results[c["id"]] = {
            "id": c["id"],
            "category": best_cat,
            "tags": tags[:3],
            "summary": summary,
            "sentiment": sentiment,
            "peptides": peptides_mentioned,
        }

    print(f"Categorized {len(results)} comments.", flush=True)
    return results


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
            full_comment = (item.get("comment") or "No comment text available.")
            comment_preview = full_comment[:300]
            is_long = len(full_comment) > 300
            reg_url = f"https://www.regulations.gov/comment/{item['id']}"
            cards_html += f"""
            <div class="comment-card" onclick="toggleComment(this)">
              <div class="card-meta">
                <span class="comment-id">{item['id']}</span>
                {org_html}{state_html}
                <span class="date">{item.get('posted_date','')}</span>
                {sentiment_badge(item.get('sentiment','neutral'))}
                <span class="expand-hint">▼ click to expand</span>
              </div>
              <p class="summary"><strong>{item.get('summary','')}</strong></p>
              <p class="comment-text preview-text">{comment_preview}{'<span class="ellipsis">...</span>' if is_long else ''}</p>
              {'<p class="comment-text full-text" style="display:none">' + full_comment + '</p>' if is_long else ''}
              <div class="card-footer">
                <div class="tags">{tags_html}</div>
                <a href="{reg_url}" target="_blank" onclick="event.stopPropagation()" class="reg-link">View on regulations.gov ↗</a>
              </div>
            </div>"""
        cards_html += "</div>"

    # Build peptide spotlight — summary only, no duplicate cards
    tb_count = len([e for e in enriched if "TB-500" in (e.get("peptides") or [])])
    bpc_count = len([e for e in enriched if "BPC-157" in (e.get("peptides") or [])])
    peptide_spotlight_html = f"""
    <div class="peptide-section">
      <h3>🔬 Peptide Mentions in This Docket</h3>
      <p style="font-size:0.9rem;color:#636e72;margin-bottom:0.75rem">Comments referencing specific peptides under review:</p>
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap">
        <div style="background:white;border-radius:8px;padding:0.75rem 1.25rem;border:1px solid #ffcc80">
          <span style="font-size:1.5rem;font-weight:700;color:#e65100">{tb_count}</span>
          <span style="font-size:0.9rem;color:#636e72;margin-left:0.5rem">comments mention <strong>TB-500</strong></span>
        </div>
        <div style="background:white;border-radius:8px;padding:0.75rem 1.25rem;border:1px solid #ffcc80">
          <span style="font-size:1.5rem;font-weight:700;color:#e65100">{bpc_count}</span>
          <span style="font-size:0.9rem;color:#636e72;margin-left:0.5rem">comments mention <strong>BPC-157</strong></span>
        </div>
      </div>
      <p style="font-size:0.8rem;color:#b2bec3;margin-top:0.75rem">Search for "tb500" or "bpc-157" below to filter these comments.</p>
    </div>"""

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
  .chart-wrapper {{ position: relative; height: 320px; }}
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
  .peptide-tag {{ font-size: 0.72rem; background: #fff3e0; color: #e65100; padding: 2px 8px; border-radius: 12px; border: 1px solid #ffcc80; font-weight: 700; }}
  .peptide-section {{ background: #fff8e1; border: 2px solid #ffcc02; border-radius: 12px; padding: 1.25rem 1.5rem; margin-bottom: 2rem; }}
  .peptide-section h3 {{ color: #e65100; margin-bottom: 0.75rem; font-size: 1.1rem; }}
  .hidden {{ display: none !important; }}
  .comment-card {{ cursor: pointer; transition: box-shadow 0.15s; }}
  .comment-card:hover {{ box-shadow: 0 4px 14px rgba(0,0,0,0.12); }}
  .expand-hint {{ font-size: 0.72rem; color: #b2bec3; margin-left: auto; }}
  .comment-card.expanded .expand-hint {{ content: "▲ collapse"; }}
  .card-footer {{ display: flex; justify-content: space-between; align-items: center; margin-top: 0.5rem; flex-wrap: wrap; gap: 0.5rem; }}
  .reg-link {{ font-size: 0.78rem; color: #1a237e; text-decoration: none; white-space: nowrap; }}
  .reg-link:hover {{ text-decoration: underline; }}
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

  {peptide_spotlight_html}
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

function toggleComment(card) {{
  const preview = card.querySelector('.preview-text');
  const full = card.querySelector('.full-text');
  const hint = card.querySelector('.expand-hint');
  if (!full) return;
  if (full.style.display === 'none') {{
    full.style.display = 'block';
    if (preview) preview.style.display = 'none';
    if (hint) hint.textContent = '▲ collapse';
    card.classList.add('expanded');
  }} else {{
    full.style.display = 'none';
    if (preview) preview.style.display = 'block';
    if (hint) hint.textContent = '▼ click to expand';
    card.classList.remove('expanded');
  }}
}}

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
