"""
Competitive benchmarking module.

Pipeline
────────
1. identify_workflows(report_text)
     → Claude extracts product type, industry, region, workflows, competitor hints

2. _search_ddg(query)
     → DuckDuckGo HTML search returns [{title, url, snippet}]

3. filter_competitors(raw_results, context, hints)
     → Claude selects up to 5 genuine competitors with type + URL

4. _search_mobbin(query)
     → Playwright navigates mobbin.com, returns screenshot of search-results grid

5. _capture_competitor(url, name, workflow)
     → Playwright + Claude (haiku) navigates up to 5 steps of the relevant flow

6. generate_benchmark_html(context, target_steps, competitors, mobbin, workflow)
     → Full interactive HTML report

7. run_benchmark(report_text, api_url, progress_cb)
     → Orchestrates 1-6, returns {"html", "product_context", "competitors"}
"""

import base64
import io
import json
import os
import re
import time
from urllib.parse import quote_plus, unquote

import httpx
from anthropic import Anthropic
from PIL import Image
from playwright.sync_api import sync_playwright

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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
    """Ask Claude to extract structured metadata from a heuristic report."""
    prompt = f"""You are analysing a UX heuristic evaluation report to extract metadata for competitive benchmarking.

From the report below return a single JSON object with exactly these fields:
{{
  "product_type": "short phrase describing the product (e.g. 'mobile banking app', 'e-commerce checkout')",
  "industry": "industry vertical (e.g. fintech, e-commerce, healthtech, ride-hailing, edtech)",
  "region": "country or region if detectable from the report, else 'global'",
  "workflows": [
    {{
      "name": "Human-readable workflow name (e.g. 'Account onboarding', 'Checkout flow')",
      "description": "One sentence: what the user is trying to accomplish",
      "mobbin_query": "2-4 word query to search on Mobbin.com (e.g. 'banking onboarding', 'checkout payment')",
      "competitor_search": "DuckDuckGo query to find competitors offering this workflow (e.g. 'mobile banking app South Africa competitors')"
    }}
  ],
  "competitors_hints": ["Name1", "Name2", "Name3"],
  "duckduckgo_query": "best overall query to find regional and global direct competitors"
}}

Return ONLY valid JSON. No markdown fences. No explanation.

Report (first 8 000 chars):
---
{report_text[:8000]}
---"""

    try:
        resp = _client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json(resp.content[0].text))
    except Exception as e:
        print(f"  identify_workflows error: {e}")
        return {
            "product_type": "digital product",
            "industry": "technology",
            "region": "global",
            "workflows": [{"name": "Main flow", "description": "",
                           "mobbin_query": "app onboarding",
                           "competitor_search": "competitor apps"}],
            "competitors_hints": [],
            "duckduckgo_query": "competitor apps",
        }


# ── Step 2: web search ────────────────────────────────────────────────────────

def _search_ddg(query: str, max_results: int = 12) -> list[dict]:
    """Search DuckDuckGo (HTML endpoint) and return [{title, url, snippet}]."""
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
            encoded = quote_plus(query)
            page.goto(
                f"https://html.duckduckgo.com/html/?q={encoded}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            for el in page.query_selector_all(".result")[:max_results]:
                a = el.query_selector(".result__title a")
                snip = el.query_selector(".result__snippet")
                if not a:
                    continue
                raw_href = a.get_attribute("href") or ""
                # DDG HTML wraps real URLs in a redirect; unwrap
                m = re.search(r"uddg=([^&]+)", raw_href)
                url = unquote(m.group(1)) if m else raw_href
                results.append({
                    "title":   a.inner_text().strip(),
                    "url":     url,
                    "snippet": snip.inner_text().strip() if snip else "",
                })
            browser.close()
    except Exception as e:
        print(f"  DDG search error: {e}")
    return results


# ── Step 3: competitor filtering ──────────────────────────────────────────────

def filter_competitors(
    search_results: list[dict],
    context: dict,
    hints: list[str],
) -> list[dict]:
    """Use Claude (haiku) to pick genuine competitors from search results."""
    results_text = "\n".join(
        f"- {r['title']}: {r['url']} — {r['snippet']}"
        for r in search_results[:20]
    )
    hints_text = ", ".join(hints) if hints else "(none)"
    prompt = f"""Pick the top 5 genuine COMPETITOR PRODUCTS for benchmarking.

Product being benchmarked:
  Type: {context.get('product_type')}
  Industry: {context.get('industry')}
  Region: {context.get('region')}

Known competitor hints: {hints_text}

Search results:
{results_text}

Return a JSON array. Each object:
  {{"name": "Company", "url": "https://...", "type": "direct|analogous|regional|global", "rationale": "one sentence"}}

Rules:
- Prefer real product websites, not blogs/review sites/aggregators
- Mix of regional and global where possible
- type = "direct" if same product category and region; "regional" if same region different category;
  "global" if global well-known equivalent; "analogous" if different region but similar product
- ONLY JSON array, no markdown, no explanation
- If a URL looks like a news/review site (techcrunch, forbes, g2, capterra), skip it"""

    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json(resp.content[0].text))
    except Exception as e:
        print(f"  filter_competitors error: {e}")
        return [{"name": h, "url": "", "type": "unknown", "rationale": ""} for h in hints[:5]]


# ── Step 4: Mobbin ────────────────────────────────────────────────────────────

def _search_mobbin(query: str) -> dict | None:
    """Navigate Mobbin.com, screenshot the visible flow results grid."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            encoded = quote_plus(query)
            page.goto(
                f"https://mobbin.com/browse/ios/flows?sort=popularity&term={encoded}",
                wait_until="networkidle",
                timeout=30_000,
            )
            time.sleep(2)  # let JS render thumbnails
            screenshot = page.screenshot()
            # Try to scrape visible app names
            apps = []
            for sel in ['[class*="app"]', 'h3', '[aria-label]']:
                for el in page.query_selector_all(sel)[:30]:
                    t = el.inner_text().strip()
                    if t and len(t) < 60 and t not in apps:
                        apps.append(t)
                if apps:
                    break
            browser.close()
            return {"query": query, "url": page.url, "screenshot": screenshot, "apps": apps[:12]}
    except Exception as e:
        print(f"  Mobbin search error: {e}")
        return None


# ── Step 5: competitor flow capture (agentic Playwright) ──────────────────────

def _next_nav_action(img_b64: str, steps_done: list[str], workflow: dict, comp_name: str) -> dict:
    """Ask Claude haiku what to click/navigate next on this competitor page."""
    wf_name = workflow.get("name", "main workflow")
    wf_desc = workflow.get("description", "")
    prompt = (
        f"You are navigating {comp_name}'s website to find and capture their "
        f'"{wf_name}" flow ({wf_desc}).\n'
        f"Steps already captured: {steps_done}\n\n"
        "Look at this screenshot. Choose the NEXT navigation step to get closer to the "
        f"{wf_name} flow.\n\n"
        "Return JSON with one of:\n"
        '{"action":"click","label":"what this step shows","text":"visible button/link text"}\n'
        '{"action":"navigate","label":"what this step shows","url":"absolute URL"}\n'
        '{"action":"stop","reason":"already captured enough OR flow not findable"}\n\n'
        "Rules:\n"
        "- Click CTAs like Sign up, Register, Get started, Open account, etc.\n"
        f"- After {len(steps_done)} steps, return stop if the flow is clearly captured\n"
        "- Never fill in real personal data\n"
        "- ONLY JSON, no explanation"
    )
    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/jpeg",
                                                  "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return json.loads(_strip_json(resp.content[0].text))
    except Exception:
        return {"action": "stop"}


def _execute_action(page, action: dict):
    act = action.get("action")
    if act == "navigate":
        page.goto(action.get("url", ""), wait_until="domcontentloaded", timeout=20_000)
    elif act == "click":
        text = action.get("text", "")
        selector = action.get("selector", "")
        clicked = False
        if selector:
            try:
                page.click(selector, timeout=5_000)
                clicked = True
            except Exception:
                pass
        if not clicked and text:
            for attempt in [
                lambda: page.get_by_text(text, exact=False).first.click(timeout=5_000),
                lambda: page.click(f"text={text}", timeout=5_000),
            ]:
                try:
                    attempt()
                    clicked = True
                    break
                except Exception:
                    pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass


def _capture_competitor(
    url: str,
    name: str,
    workflow: dict,
    viewport_width: int = 1440,
) -> list[dict]:
    """Navigate competitor site for up to 5 steps, return [{step_num, label, screenshot_bytes, url}]."""
    if not url or not url.startswith("http"):
        return []
    steps: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": viewport_width, "height": 900},
                ignore_https_errors=True,
            )
            page = ctx.new_page()
            # Step 0 — homepage
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                except Exception as e:
                    print(f"    Could not load {url}: {e}")
                    browser.close()
                    return steps

            steps.append({
                "step_num": 0,
                "label": "Homepage",
                "screenshot_bytes": page.screenshot(),
                "url": page.url,
            })

            # Steps 1-4 — Claude-guided navigation
            for i in range(1, 5):
                img_b64 = _to_jpeg_b64(steps[-1]["screenshot_bytes"])
                done_labels = [s["label"] for s in steps]
                action = _next_nav_action(img_b64, done_labels, workflow, name)
                if action.get("action") == "stop":
                    break
                try:
                    _execute_action(page, action)
                    time.sleep(1.5)
                    steps.append({
                        "step_num": i,
                        "label": action.get("label", f"Step {i}"),
                        "screenshot_bytes": page.screenshot(),
                        "url": page.url,
                    })
                except Exception as e:
                    print(f"    Action failed on {name} step {i}: {e}")
                    break

            browser.close()
    except Exception as e:
        print(f"  Competitor capture error ({name}): {e}")
    return steps


# ── Step 6: benchmark HTML ────────────────────────────────────────────────────

def _step_card(step: dict, css_class: str = "") -> str:
    b64 = _to_png_b64(step["screenshot_bytes"])
    label = step.get("label", f"Step {step.get('step_num', '')}")
    step_url = step.get("url", "")
    url_chip = (
        f'<a href="{step_url}" target="_blank" class="step-url" title="{step_url}">'
        f'{step_url[:55]}{"…" if len(step_url) > 55 else ""}</a>'
    ) if step_url else ""
    return (
        f'<div class="step-card {css_class}">'
        f'  <div class="step-label">{label}</div>'
        f'  {url_chip}'
        f'  <img src="data:image/png;base64,{b64}" loading="lazy" '
        f'       onclick="openZoom(this.src)" />'
        f'</div>'
    )


def generate_benchmark_html(
    context: dict,
    target_steps: list[dict],
    competitors: list[dict],
    mobbin_results: list[dict],
    workflow: dict,
    api_url: str = None,
) -> str:
    product_type = context.get("product_type", "")
    industry     = context.get("industry", "")
    region       = context.get("region", "")
    wf_name      = workflow.get("name", "Workflow")

    # ── target section ──
    if target_steps:
        target_cards = "".join(_step_card(s) for s in target_steps)
        target_section = f"""
<section class="comp-block target-block">
  <div class="comp-header">
    <h2>Your product <span class="chip chip-baseline">baseline</span></h2>
    <span class="comp-badge badge-target">baseline</span>
  </div>
  <div class="steps-row">{target_cards}</div>
</section>"""
    else:
        target_section = ""

    # ── competitor sections ──
    comp_html = ""
    for comp in competitors:
        name      = comp.get("name", "Competitor")
        comp_url  = comp.get("url", "")
        comp_type = comp.get("type", "global")
        steps     = comp.get("steps", [])
        rationale = comp.get("rationale", "")
        badge_cls = {
            "direct":    "badge-direct",
            "regional":  "badge-regional",
            "global":    "badge-global",
            "analogous": "badge-analogous",
        }.get(comp_type, "badge-global")

        if steps:
            cards_html = "".join(_step_card(s) for s in steps)
        else:
            cards_html = '<p class="empty-msg">Screenshots could not be captured.</p>'

        analyze_btn = ""
        if api_url and comp_url:
            analyze_btn = (
                f'<button class="analyze-btn" '
                f'onclick="analyzeCompetitor(\'{comp_url}\')">'
                f'&#128269; Analyse this competitor</button>'
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

    # ── Mobbin section ──
    mobbin_html = ""
    for mb in mobbin_results:
        if not mb:
            continue
        b64     = _to_png_b64(mb["screenshot"])
        apps    = ", ".join(mb.get("apps", [])) or "various apps"
        mb_url  = mb.get("url", "https://mobbin.com")
        mb_q    = mb.get("query", wf_name)
        mobbin_html += f"""
<section class="comp-block mobbin-block">
  <div class="comp-header">
    <h2>Mobbin — <em>"{mb_q}"</em></h2>
    <span class="comp-badge badge-reference">reference library</span>
    <a href="{mb_url}" target="_blank" class="comp-url">mobbin.com</a>
  </div>
  <p class="mobbin-note">Apps visible in results: {apps}</p>
  <div class="steps-row">
    <div class="step-card step-wide">
      <div class="step-label">Search results overview</div>
      <img src="data:image/png;base64,{b64}" loading="lazy" onclick="openZoom(this.src)" />
    </div>
  </div>
</section>"""

    # ── analyze competitor JS (if api_url present) ──
    analyze_js = ""
    if api_url:
        analyze_js = f"""
  function analyzeCompetitor(url) {{
    if (!confirm('Open a full heuristic analysis for ' + url + ' in the main tool?')) return;
    window.open('{api_url}/?url=' + encodeURIComponent(url), '_blank');
  }}"""

    n_comps   = len(competitors)
    n_steps   = len(target_steps)
    n_mobbin  = len(mobbin_results)

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
                 border-radius: 8px; font-size: .85rem; font-weight: 600;
                 cursor: pointer; text-decoration: none; white-space: nowrap;
                 transition: border-color .15s, color .15s; }}
    .tb-btn:hover {{ border-color: #94a3b8; color: #f8fafc; }}
    main      {{ max-width: 1500px; margin: 0 auto; padding: 2rem; }}
    .ctx-bar  {{ background: white; border-radius: 12px;
                 box-shadow: 0 4px 16px rgba(0,0,0,.08);
                 padding: 1rem 1.5rem; margin-bottom: 1.75rem;
                 display: flex; flex-wrap: wrap; gap: 1.5rem; }}
    .ctx-item {{ font-size: .84rem; }}
    .ctx-item strong {{ color: #0f172a; margin-right: .25rem; }}
    .ctx-item span   {{ color: #64748b; }}
    .comp-block {{ background: white; border-radius: 12px;
                   box-shadow: 0 4px 16px rgba(0,0,0,.08);
                   padding: 1.5rem; margin-bottom: 2rem; }}
    .target-block {{ border: 2px solid #6366f1; }}
    .mobbin-block {{ border-top: 3px solid #8b5cf6; }}
    .comp-header  {{ display: flex; align-items: center; flex-wrap: wrap;
                     gap: .65rem; margin-bottom: 1.25rem; }}
    .comp-header h2 {{ font-size: 1.1rem; font-weight: 700; color: #0f172a; }}
    .comp-url  {{ font-size: .8rem; color: #6366f1; text-decoration: none; margin-left: .25rem; }}
    .comp-url:hover {{ text-decoration: underline; }}
    .rationale {{ font-size: .78rem; color: #64748b; font-style: italic; }}
    .comp-badge {{ padding: .2rem .6rem; border-radius: 999px;
                   font-size: .7rem; font-weight: 700;
                   text-transform: uppercase; letter-spacing: .05em; }}
    .badge-direct    {{ background: #fee2e2; color: #b91c1c; }}
    .badge-analogous {{ background: #fef3c7; color: #92400e; }}
    .badge-regional  {{ background: #d1fae5; color: #065f46; }}
    .badge-global    {{ background: #dbeafe; color: #1e40af; }}
    .badge-reference {{ background: #e0e7ff; color: #3730a3; }}
    .badge-target    {{ background: #f3e8ff; color: #6b21a8; }}
    .chip {{ background: #6366f1; color: white; border-radius: 4px;
             padding: .1rem .4rem; font-size: .68rem; font-weight: 700;
             vertical-align: middle; margin-left: .3rem; }}
    .steps-row  {{ display: flex; flex-wrap: nowrap; gap: 1rem;
                   overflow-x: auto; padding-bottom: .5rem; }}
    .step-card  {{ flex: 0 0 220px; }}
    .step-wide  {{ flex: 0 0 min(100%, 900px); }}
    .step-label {{ font-size: .76rem; font-weight: 600; color: #64748b; margin-bottom: .3rem; }}
    .step-url   {{ display: block; font-size: .7rem; color: #94a3b8; margin-bottom: .35rem;
                   text-decoration: none; white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; max-width: 220px; }}
    .step-url:hover {{ color: #6366f1; }}
    .step-card img  {{ width: 100%; border-radius: 6px; border: 1px solid #e2e8f0;
                       cursor: zoom-in; transition: box-shadow .15s; display: block; }}
    .step-card img:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.15); }}
    .empty-msg  {{ color: #94a3b8; font-size: .85rem; font-style: italic; padding: .5rem 0; }}
    .mobbin-note {{ font-size: .82rem; color: #64748b; margin-bottom: .75rem; }}
    .analyze-btn {{ margin-left: auto; padding: .3rem .75rem;
                    background: #f1f5f9; border: 1.5px solid #cbd5e1;
                    border-radius: 6px; font-size: .78rem; font-weight: 600;
                    color: #1e293b; cursor: pointer; white-space: nowrap;
                    transition: background .15s; }}
    .analyze-btn:hover {{ background: #e2e8f0; }}
    /* zoom modal */
    #zoom-modal {{ display: none; position: fixed; inset: 0;
                   background: rgba(0,0,0,.88); z-index: 9999;
                   align-items: center; justify-content: center; cursor: zoom-out; }}
    #zoom-modal.open {{ display: flex; }}
    #zoom-img {{ max-width: 94vw; max-height: 94vh; border-radius: 8px; object-fit: contain; }}
  </style>
</head>
<body>
  <header>
    <h1>Competitive Benchmark: {wf_name}</h1>
    <p>{product_type} &middot; {industry} &middot; {region}</p>
  </header>
  <div id="top-bar">
    <span class="tb-stat">{n_comps} competitor{'' if n_comps == 1 else 's'}</span>
    <span class="tb-stat">&middot;</span>
    <span class="tb-stat">{n_steps} baseline step{'' if n_steps == 1 else 's'}</span>
    {f'<span class="tb-stat">&middot;</span><span class="tb-stat">{n_mobbin} Mobbin result{chr(115) if n_mobbin != 1 else ""}</span>' if n_mobbin else ''}
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
    {target_section}
    {comp_html}
    {mobbin_html}
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
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeZoom();
  }});
  {analyze_js}
  </script>
</body>
</html>"""


# ── Step 7: orchestrator ──────────────────────────────────────────────────────

def run_benchmark(
    report_text: str,
    api_url: str = None,
    progress_cb=None,
) -> dict:
    """
    Full pipeline.
    progress_cb(msg: str) is called with human-readable status updates.
    Returns {"html": str, "product_context": dict, "competitors": list}.
    """
    def _p(msg: str):
        print(f"  [benchmark] {msg}")
        if progress_cb:
            progress_cb(msg)

    # 1 ── identify
    _p("Identifying product type, industry, and workflows…")
    context   = identify_workflows(report_text)
    workflows = context.get("workflows") or [
        {"name": "Main flow", "description": "", "mobbin_query": "",
         "competitor_search": f"{context.get('product_type')} competitors"}
    ]
    primary   = workflows[0]
    _p(f"→ {context.get('product_type')} | {context.get('industry')} | {context.get('region')}")
    _p(f"→ Primary workflow: {primary.get('name')}")

    # 2 ── search
    ddg_query = context.get("duckduckgo_query") or (
        f"{context.get('product_type')} {context.get('region')} competitors"
    )
    _p(f'Searching for competitors: "{ddg_query}"…')
    raw = _search_ddg(ddg_query, max_results=15)
    _p(f"→ {len(raw)} search results")

    # 3 ── filter
    _p("Filtering for genuine competitors…")
    competitors = filter_competitors(raw, context, context.get("competitors_hints", []))
    _p(f"→ {len(competitors)} competitors: {[c.get('name') for c in competitors]}")

    # 4 ── Mobbin
    mobbin_results = []
    if primary.get("mobbin_query"):
        _p(f"Searching Mobbin for \"{primary['mobbin_query']}\"…")
        mb = _search_mobbin(primary["mobbin_query"])
        if mb:
            mobbin_results = [mb]
            _p(f"→ Mobbin results captured ({len(mb.get('apps', []))} apps visible)")

    # 5 ── capture each competitor (max 4)
    for comp in competitors[:4]:
        name = comp.get("name", "unknown")
        url  = comp.get("url", "")
        if not url:
            _p(f"Skipping {name} (no URL found)")
            comp["steps"] = []
            continue
        _p(f"Capturing {name} ({url})…")
        comp["steps"] = _capture_competitor(url, name, primary)
        _p(f"→ {len(comp['steps'])} steps captured from {name}")

    # 6 ── build report
    _p("Building benchmark report…")
    html = generate_benchmark_html(
        context       = context,
        target_steps  = [],          # originals not stored; user has their own report
        competitors   = competitors[:4],
        mobbin_results= mobbin_results,
        workflow      = primary,
        api_url       = api_url,
    )
    _p("Done.")
    return {"html": html, "product_context": context, "competitors": competitors}
