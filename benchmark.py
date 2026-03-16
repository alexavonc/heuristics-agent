"""
Competitive benchmarking module — Playwright-free edition.

Pipeline
────────
1. identify_workflows(report_text)
     → Claude extracts product type, industry, region, workflow, competitor hints

2. _search_ddg(query)
     → DuckDuckGo HTML endpoint via httpx — no browser needed

3. filter_competitors(raw_results, context, hints)
     → Claude selects up to 5 genuine competitors with type + URL

4. _identify_workflow_tasks(product_context, workflow)
     → Claude identifies 2–4 specific pages to compare per competitor

5. _capture_competitor(url, name, workflow_tasks, _prog)
     → httpx fetches HTML, BeautifulSoup extracts links, Claude picks
       which link matches each task, thum.io renders screenshots

6. generate_benchmark_html(context, competitors, workflow)
     → Full interactive HTML report

7. run_benchmark(report_text, api_url, progress_cb)
     → Orchestrates 1–6, returns {"html", "product_context", "competitors"}
"""

import base64
import io
import json
import os
import re
import time
from urllib.parse import quote_plus, unquote, urljoin

import httpx
from anthropic import Anthropic
from bs4 import BeautifulSoup
from PIL import Image

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _client() -> Anthropic:
    return Anthropic(api_key=API_KEY, http_client=httpx.Client(verify=False))


def _to_jpeg_b64(img_bytes: bytes, max_dim: int = 900, quality: int = 70) -> str:
    img = Image.open(io.BytesIO(img_bytes))
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _to_png_b64(img_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(img_bytes))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.standard_b64encode(buf.getvalue()).decode()
    except Exception:
        return base64.standard_b64encode(img_bytes).decode()


def _strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ── Step 1: workflow / product identification ─────────────────────────────────

def identify_workflows(report_text: str) -> dict:
    """Extract structured metadata from a heuristic report."""
    prompt = f"""You are analysing a UX heuristic evaluation report for competitive benchmarking.

Return a single JSON object with exactly these fields:
{{
  "product_type": "short phrase (e.g. 'mobile banking app', 'property agent portal')",
  "industry": "industry vertical (e.g. fintech, proptech, healthtech, e-commerce)",
  "region": "country or region if detectable, else 'global'",
  "primary_workflow": {{
    "name": "Human-readable workflow name (e.g. 'Agent login and listing creation')",
    "description": "One sentence: what the user is trying to accomplish",
    "competitor_search": "DuckDuckGo query to find competitors (e.g. 'property agent portal Singapore competitors')"
  }},
  "competitors_hints": ["Name1", "Name2", "Name3"]
}}

Return ONLY valid JSON. No markdown fences.

Report (first 6000 chars):
---
{report_text[:6000]}
---"""

    try:
        resp = _client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=768,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json(resp.content[0].text))
    except Exception as e:
        print(f"  identify_workflows error: {e}")
        return {
            "product_type": "digital product",
            "industry": "technology",
            "region": "global",
            "primary_workflow": {
                "name": "Main flow",
                "description": "Core user workflow",
                "competitor_search": "competitor apps",
            },
            "competitors_hints": [],
        }


# ── Step 2: DuckDuckGo search via httpx ──────────────────────────────────────

def _search_ddg(query: str, max_results: int = 12) -> list[dict]:
    """Search DuckDuckGo HTML endpoint and return [{title, url, snippet}]."""
    results = []
    try:
        resp = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=_HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select(".result")[:max_results]:
            a = el.select_one(".result__title a")
            snip = el.select_one(".result__snippet")
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            url = unquote(m.group(1)) if m else href
            results.append({
                "title":   a.get_text(strip=True),
                "url":     url,
                "snippet": snip.get_text(strip=True) if snip else "",
            })
    except Exception as e:
        print(f"  DDG search error: {e}")
    return results


# ── Step 3: competitor filtering ──────────────────────────────────────────────

def filter_competitors(
    search_results: list[dict],
    context: dict,
    hints: list[str],
) -> list[dict]:
    """Use Claude to pick genuine competitors from search results."""
    results_text = "\n".join(
        f"- {r['title']}: {r['url']} — {r['snippet']}"
        for r in search_results[:20]
    )
    hints_text = ", ".join(hints) if hints else "(none)"
    region = context.get("region", "global")

    prompt = f"""Pick the top 5 genuine COMPETITOR PRODUCTS for benchmarking.
Include a mix: 3 from the same region ({region}) and 2 global/international.

Product being benchmarked:
  Type: {context.get('product_type')}
  Industry: {context.get('industry')}
  Region: {region}

Known competitor hints: {hints_text}

Search results:
{results_text}

Return a JSON array. Each object:
  {{"name": "Company", "url": "https://...", "type": "direct|regional|global", "rationale": "one sentence"}}

Rules:
- Prefer real product websites over review/blog sites
- First 3 should be regional (same country/region), last 2 global
- Skip news/review sites (techcrunch, g2, capterra, etc.)
- ONLY JSON array, no markdown"""

    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=768,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json(resp.content[0].text))
    except Exception as e:
        print(f"  filter_competitors error: {e}")
        return [{"name": h, "url": "", "type": "direct", "rationale": ""} for h in hints[:5]]


# ── Step 4: workflow task identification ──────────────────────────────────────

def _identify_workflow_tasks(product_context: dict, workflow: dict) -> list[dict]:
    """Ask Claude what specific pages to capture for this workflow on competitors."""
    wf_name = workflow.get("name", "main workflow")
    wf_desc = workflow.get("description", "")
    product_type = product_context.get("product_type", "product")

    prompt = f"""For a UX competitive benchmark, we are studying the "{wf_name}" workflow in a {product_type}.
Workflow: {wf_desc}

What are 2–3 specific pages or UI states that are most valuable to screenshot on competitor websites to compare how they implement this workflow?

Return a JSON array (2–3 items):
[
  {{"label": "Short page name", "link_hint": "keyword likely in the link text on their website"}},
  ...
]

Examples for "checkout flow": [{{"label": "Cart", "link_hint": "cart"}}, {{"label": "Payment", "link_hint": "checkout"}}]
Examples for "agent login & listing": [{{"label": "Login page", "link_hint": "login"}}, {{"label": "Agent portal", "link_hint": "agent"}}, {{"label": "New listing", "link_hint": "listing"}}]

ONLY JSON array, no markdown."""

    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        tasks = json.loads(_strip_json(resp.content[0].text))
        return tasks[:3]
    except Exception:
        return [{"label": "Main page", "link_hint": ""}]


# ── Step 5a: screenshot via thum.io ──────────────────────────────────────────

def _screenshot_url(url: str) -> bytes | None:
    """Get a 1440-wide screenshot via thum.io (free, no API key needed)."""
    try:
        api = f"https://image.thum.io/get/width/1440/noanimate/{url}"
        resp = httpx.get(api, timeout=30, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; UXBenchmark/1.0)",
        })
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "image" in ct or len(resp.content) > 1000:
                return resp.content
    except Exception as e:
        print(f"  thum.io failed for {url}: {e}")
    return None


# ── Step 5b: agentic competitor capture ──────────────────────────────────────

def _extract_links(html: str, base_url: str) -> list[dict]:
    """Extract navigation links from HTML, resolved to absolute URLs."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or not href or href.startswith("#") or href.startswith("javascript"):
            continue
        if href.startswith("/"):
            href = urljoin(base_url, href)
        if not href.startswith("http"):
            continue
        if href not in seen and len(links) < 60:
            seen.add(href)
            links.append({"text": text[:80], "url": href})
    return links


def _pick_link_for_task(links: list[dict], task: dict, comp_name: str, workflow_name: str) -> str | None:
    """Ask Claude to pick the best link for a given task from available links."""
    label = task.get("label", "relevant page")
    hint = task.get("link_hint", "")

    if not links:
        return None

    links_text = "\n".join(f"- {l['text']}: {l['url']}" for l in links[:40])

    prompt = f"""You are benchmarking {comp_name}'s "{workflow_name}" flow.
Find their "{label}" page{f' (look for links containing: {hint})' if hint else ''}.

Available links:
{links_text}

Return ONLY the single best matching URL from the list above.
If nothing matches, return: SKIP"""

    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        url = resp.content[0].text.strip()
        if url == "SKIP" or not url.startswith("http"):
            return None
        return url
    except Exception:
        return None


def _capture_competitor(
    url: str,
    name: str,
    workflow_tasks: list[dict],
    workflow_name: str,
    _prog,
) -> list[dict]:
    """
    Navigate competitor site using httpx + Claude link selection,
    capture screenshots via thum.io at each step.
    """
    if not url or not url.startswith("http"):
        return []

    steps: list[dict] = []

    # Step 0 — entry page screenshot
    _prog(f"  → {name}: screenshotting entry page…")
    screenshot = _screenshot_url(url)
    steps.append({
        "step_num":        0,
        "label":           "Entry page",
        "screenshot_bytes": screenshot,
        "url":             url,
    })

    if not workflow_tasks:
        return steps

    # Fetch HTML for link extraction
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        links = _extract_links(resp.text, str(resp.url))
    except Exception as e:
        _prog(f"  → {name}: could not fetch page links ({e})")
        return steps

    # For each task, pick best link and screenshot it
    visited: set[str] = {url}
    for i, task in enumerate(workflow_tasks, 1):
        task_label = task.get("label", f"Step {i}")
        _prog(f"  → {name}: finding {task_label}…")
        chosen_url = _pick_link_for_task(links, task, name, workflow_name)

        if not chosen_url or chosen_url in visited:
            _prog(f"  → {name}: no match for {task_label}, skipping")
            continue

        visited.add(chosen_url)
        _prog(f"  → {name}: screenshotting {task_label}…")
        screenshot = _screenshot_url(chosen_url)
        steps.append({
            "step_num":        i,
            "label":           task_label,
            "screenshot_bytes": screenshot,
            "url":             chosen_url,
        })

    return steps


# ── Step 6: benchmark HTML ────────────────────────────────────────────────────

def _step_card(step: dict) -> str:
    ss = step.get("screenshot_bytes")
    label = step.get("label", f"Step {step.get('step_num', '')}")
    step_url = step.get("url", "")

    url_chip = ""
    if step_url:
        short = step_url[:60] + ("…" if len(step_url) > 60 else "")
        url_chip = (
            f'<a href="{step_url}" target="_blank" class="step-url" title="{step_url}">'
            f"{short}</a>"
        )

    if ss:
        try:
            b64 = _to_png_b64(ss)
            img_html = (
                f'<img src="data:image/png;base64,{b64}" loading="lazy" '
                f'     onclick="openZoom(this.src)" />'
            )
        except Exception:
            img_html = '<p class="empty-msg">Screenshot could not be rendered.</p>'
    else:
        img_html = '<p class="empty-msg">Screenshot could not be captured.</p>'

    return (
        f'<div class="step-card">'
        f'  <div class="step-label">{label}</div>'
        f"  {url_chip}"
        f"  {img_html}"
        f"</div>"
    )


def generate_benchmark_html(
    context: dict,
    competitors: list[dict],
    workflow: dict,
    api_url: str = "",
) -> str:
    product_type = context.get("product_type", "")
    industry     = context.get("industry", "")
    region       = context.get("region", "")
    wf_name      = workflow.get("name", "Workflow")

    # ── competitor sections ──
    comp_html = ""
    for comp in competitors:
        name      = comp.get("name", "Competitor")
        comp_url  = comp.get("url", "")
        comp_type = comp.get("type", "global")
        steps     = comp.get("steps", [])
        rationale = comp.get("rationale", "")

        badge_cls = {
            "direct":   "badge-direct",
            "regional": "badge-regional",
            "global":   "badge-global",
        }.get(comp_type, "badge-global")

        cards_html = (
            "".join(_step_card(s) for s in steps)
            if steps
            else '<p class="empty-msg">Screenshots could not be captured.</p>'
        )

        analyze_btn = ""
        if api_url and comp_url:
            safe_url = comp_url.replace("'", "\\'")
            analyze_btn = (
                f'<button class="analyze-btn" '
                f"onclick=\"analyzeCompetitor('{safe_url}')\">"
                f"&#128269; Analyse this competitor</button>"
            )

        comp_html += f"""
<section class="comp-block">
  <div class="comp-header">
    <h2>{name}</h2>
    <span class="comp-badge {badge_cls}">{comp_type}</span>
    {f'<a href="{comp_url}" target="_blank" class="comp-url">{comp_url}</a>' if comp_url else ''}
    <span class="rationale">{rationale}</span>
    {analyze_btn}
  </div>
  <div class="steps-row">{cards_html}</div>
</section>"""

    analyze_js = ""
    if api_url:
        analyze_js = f"""
  function analyzeCompetitor(url) {{
    if (!confirm('Open a full heuristic analysis for ' + url + '?')) return;
    window.open('{api_url}/?url=' + encodeURIComponent(url), '_blank');
  }}"""

    n_comps = len(competitors)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Competitive Benchmark: {wf_name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #f1f5f9; color: #1e293b; line-height: 1.6; }}
    header {{ background: #0f172a; color: #f8fafc; padding: 1.25rem 2rem; }}
    header h1 {{ font-size: 1.4rem; font-weight: 700; }}
    header p  {{ font-size: .85rem; color: #94a3b8; margin-top: .25rem; }}
    #top-bar  {{ position: sticky; top: 0; z-index: 100; background: #1e293b;
                 padding: .55rem 2rem; display: flex; align-items: center;
                 gap: .75rem; box-shadow: 0 2px 8px rgba(0,0,0,.25); }}
    .tb-stat  {{ font-size: .82rem; color: #94a3b8; font-weight: 600; }}
    .tb-right {{ margin-left: auto; display: flex; align-items: center; gap: .5rem; }}
    .tb-btn   {{ display: flex; align-items: center; gap: .4rem; padding: .45rem 1rem;
                 background: transparent; color: #e2e8f0; border: 1.5px solid #475569;
                 border-radius: 8px; font-size: .85rem; font-weight: 600; cursor: pointer;
                 text-decoration: none; white-space: nowrap; }}
    .tb-btn:hover {{ border-color: #94a3b8; color: #f8fafc; }}
    main      {{ max-width: 1500px; margin: 0 auto; padding: 2rem; }}
    .ctx-bar  {{ background: white; border-radius: 12px;
                 box-shadow: 0 4px 16px rgba(0,0,0,.08);
                 padding: 1rem 1.5rem; margin-bottom: 1.75rem;
                 display: flex; flex-wrap: wrap; gap: 1.5rem; }}
    .ctx-item {{ font-size: .84rem; }}
    .ctx-item strong {{ color: #0f172a; margin-right: .25rem; }}
    .ctx-item span   {{ color: #64748b; }}
    .comp-block  {{ background: white; border-radius: 12px;
                    box-shadow: 0 4px 16px rgba(0,0,0,.08);
                    padding: 1.5rem; margin-bottom: 2rem; }}
    .comp-header {{ display: flex; align-items: center; flex-wrap: wrap;
                    gap: .65rem; margin-bottom: 1.25rem; }}
    .comp-header h2 {{ font-size: 1.1rem; font-weight: 700; color: #0f172a; }}
    .comp-url  {{ font-size: .8rem; color: #6366f1; text-decoration: none; }}
    .comp-url:hover {{ text-decoration: underline; }}
    .rationale {{ font-size: .78rem; color: #64748b; font-style: italic; }}
    .comp-badge {{ padding: .2rem .6rem; border-radius: 999px;
                   font-size: .7rem; font-weight: 700;
                   text-transform: uppercase; letter-spacing: .05em; }}
    .badge-direct   {{ background: #fee2e2; color: #b91c1c; }}
    .badge-regional {{ background: #d1fae5; color: #065f46; }}
    .badge-global   {{ background: #dbeafe; color: #1e40af; }}
    .analyze-btn {{ margin-left: auto; padding: .3rem .75rem;
                    background: #f1f5f9; border: 1.5px solid #cbd5e1;
                    border-radius: 6px; font-size: .78rem; font-weight: 600;
                    color: #1e293b; cursor: pointer; white-space: nowrap; }}
    .analyze-btn:hover {{ background: #e2e8f0; }}
    .steps-row {{ display: flex; flex-wrap: nowrap; gap: 1rem;
                  overflow-x: auto; padding-bottom: .5rem; }}
    .step-card  {{ flex: 0 0 220px; }}
    .step-label {{ font-size: .76rem; font-weight: 600; color: #64748b; margin-bottom: .3rem; }}
    .step-url   {{ display: block; font-size: .7rem; color: #94a3b8; margin-bottom: .35rem;
                   text-decoration: none; white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; max-width: 220px; }}
    .step-url:hover {{ color: #6366f1; }}
    .step-card img {{ width: 100%; border-radius: 6px; border: 1px solid #e2e8f0;
                      cursor: zoom-in; display: block; }}
    .empty-msg {{ color: #94a3b8; font-size: .85rem; font-style: italic; padding: .5rem 0; }}
    #zoom-modal {{ display: none; position: fixed; inset: 0;
                   background: rgba(0,0,0,.88); z-index: 9999;
                   align-items: center; justify-content: center; cursor: zoom-out; }}
    #zoom-modal.open {{ display: flex; }}
    #zoom-img {{ max-width: 94vw; max-height: 94vh; border-radius: 8px; object-fit: contain; }}
  </style>
</head>
<body>
  <header>
    <h1>&#128270; Competitive Benchmark: {wf_name}</h1>
    <p>{product_type} &middot; {industry} &middot; {region}</p>
  </header>
  <div id="top-bar">
    <span class="tb-stat">{n_comps} competitor{'' if n_comps == 1 else 's'}</span>
    <div class="tb-right">
      <a href="/" class="tb-btn">&#10227; New analysis</a>
    </div>
  </div>
  <main>
    <div class="ctx-bar">
      <div class="ctx-item"><strong>Product type:</strong><span>{product_type}</span></div>
      <div class="ctx-item"><strong>Industry:</strong><span>{industry}</span></div>
      <div class="ctx-item"><strong>Region:</strong><span>{region}</span></div>
      <div class="ctx-item"><strong>Workflow:</strong><span>{wf_name}</span></div>
      <div class="ctx-item"><strong>Description:</strong><span>{workflow.get('description', '')}</span></div>
    </div>
    {comp_html}
  </main>
  <div id="zoom-modal" onclick="closeZoom()">
    <img id="zoom-img" src="" alt="" />
  </div>
  <script>
  function openZoom(src) {{
    document.getElementById('zoom-img').src = src;
    document.getElementById('zoom-modal').classList.add('open');
  }}
  function closeZoom() {{
    document.getElementById('zoom-modal').classList.remove('open');
  }}
  document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeZoom(); }});
  {analyze_js}
  </script>
</body>
</html>"""


# ── Step 7: orchestrator ──────────────────────────────────────────────────────

def run_benchmark(
    report_text: str,
    api_url: str = "",
    progress_cb=None,
) -> dict:
    """
    Full pipeline. progress_cb(msg: str) receives live status updates.
    Returns {"html": str, "product_context": dict, "competitors": list}.
    """
    def _p(msg: str):
        print(f"  [benchmark] {msg}")
        if progress_cb:
            progress_cb(msg)

    # 1 ── identify product context & workflow
    _p("Analysing your report to identify the product and workflow…")
    context  = identify_workflows(report_text)
    workflow = context.get("primary_workflow") or {
        "name": "Main flow",
        "description": "Core user workflow",
        "competitor_search": f"{context.get('product_type', 'app')} competitors",
    }
    _p(
        f"Identified: {context.get('product_type')} · "
        f"{context.get('industry')} · {context.get('region')}"
    )
    _p(f"Primary workflow: {workflow.get('name')}")

    # 2 ── identify what pages to compare per competitor
    _p("Identifying which workflow pages to compare across competitors…")
    workflow_tasks = _identify_workflow_tasks(context, workflow)
    task_names = [t.get("label", "?") for t in workflow_tasks]
    _p(f"Will capture {len(task_names)} page(s) per competitor: {', '.join(task_names)}")

    # 3 ── search for competitors
    ddg_query = workflow.get("competitor_search") or (
        f"{context.get('product_type')} {context.get('region')} competitors"
    )
    _p(f'Searching for competitors: "{ddg_query}"…')
    raw = _search_ddg(ddg_query, max_results=15)
    _p(f"Found {len(raw)} search results")

    # 4 ── filter to genuine competitors
    _p("Selecting the best competitors to benchmark against…")
    competitors = filter_competitors(raw, context, context.get("competitors_hints", []))
    names = [c.get("name", "?") for c in competitors]
    _p(f"Selected {len(competitors)} competitors: {', '.join(names)}")

    # 5 ── capture each competitor
    wf_name = workflow.get("name", "workflow")
    for i, comp in enumerate(competitors, 1):
        name     = comp.get("name", "unknown")
        comp_url = comp.get("url", "")
        if not comp_url:
            _p(f"Skipping {name} — no URL found")
            comp["steps"] = []
            continue
        _p(f"Capturing {name} [{i}/{len(competitors)}]…")
        comp["steps"] = _capture_competitor(comp_url, name, workflow_tasks, wf_name, _p)
        n_shots = sum(1 for s in comp["steps"] if s.get("screenshot_bytes"))
        _p(f"  {name}: {n_shots} screenshot(s) captured")

    # 6 ── build HTML
    _p("Building benchmark report…")
    html = generate_benchmark_html(context, competitors, workflow, api_url)

    clean_competitors = [
        {k: v for k, v in c.items() if k != "steps"}
        for c in competitors
    ]

    return {
        "html":            html,
        "product_context": context,
        "competitors":     clean_competitors,
    }
