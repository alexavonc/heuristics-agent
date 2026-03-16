"""
benchmark.py – Competitive benchmarking module for the Heuristics Agent.

Identifies 5 competitors (3 regional + 2 global), captures screenshots via
Playwright, and generates a styled HTML comparison report.
"""

import os
import io
import json
import base64
import httpx
import anthropic
from playwright.sync_api import sync_playwright

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Prompts ───────────────────────────────────────────────────────

_CONTEXT_EXTRACTION_PROMPT = """\
From the heuristic evaluation report below, extract the product context.
Return ONLY a valid JSON object with these keys (no markdown, no prose):
  product_type  – short noun phrase for the product (e.g. "e-commerce checkout flow")
  industry      – industry/sector (e.g. "fintech", "proptech", "healthcare")
  region        – country or region the product targets (e.g. "Singapore", "Southeast Asia", "United States")
  workflow      – the specific workflow evaluated (e.g. "user registration", "checkout", "agent login")
  description   – one sentence describing the product and the workflow evaluated

Report:
{report_text}
"""

_COMPETITOR_PROMPT = """\
You are a competitive intelligence analyst. Based on the product context below, \
identify exactly 5 competitors:
  • 3 that are from the SAME country/region as the product ("{region}")
  • 2 that are GLOBAL / INTERNATIONAL competitors (well-known worldwide)

Product context:
{product_context}

Return ONLY a valid JSON object (no markdown, no prose) with this exact structure:
{{
  "competitors": [
    {{
      "name": "Competitor Name",
      "url": "https://exact-login-or-relevant-page-url.com",
      "description": "One sentence – what it is and why it competes",
      "relationship": "DIRECT" or "INDIRECT",
      "scope": "regional" or "global"
    }}
  ]
}}

Rules:
- First 3 entries MUST have scope "regional" (same country/region as the product).
- Last 2 entries MUST have scope "global".
- Use real, publicly accessible URLs. Prefer the specific page most relevant to the workflow.
- Do NOT include the product itself.
- Only return JSON.
"""

# ── Screenshot capture ────────────────────────────────────────────

def _capture_screenshot(url: str) -> bytes | None:
    """Capture a 1440×900 above-the-fold screenshot. Returns PNG bytes or None."""
    import shutil
    try:
        with sync_playwright() as p:
            # Use system chromium if Playwright's own browser isn't downloaded (e.g. Railway)
            system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
            launch_kwargs: dict = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            }
            if system_chromium:
                launch_kwargs["executable_path"] = system_chromium
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            screenshot = page.screenshot(full_page=False)
            browser.close()
            return screenshot
    except Exception as exc:
        print(f"  [benchmark] Screenshot failed for {url}: {exc}")
        return None

# ── JSON parsing helper ───────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first and last fence lines
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)

# ── Main entry point ──────────────────────────────────────────────

def run_benchmark(
    report_text: str,
    api_url: str = "",
    progress_cb=None,
) -> dict:
    """
    Run a competitive benchmark from a heuristics report.

    Args:
        report_text:  The plain-text heuristics report from the prior analysis.
        api_url:      Base URL of this API service (for "Analyse" buttons in HTML).
        progress_cb:  Optional callable(str) for progress messages.

    Returns:
        dict with keys: html, product_context, competitors
    """

    def _prog(msg: str):
        print(f"  [benchmark] {msg}")
        if progress_cb:
            progress_cb(msg)

    client = anthropic.Anthropic(
        api_key=API_KEY,
        http_client=httpx.Client(verify=False),
    )

    # ── Step 1: Extract product context ──────────────────────────
    _prog("Extracting product context from report…")
    ctx_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": _CONTEXT_EXTRACTION_PROMPT.format(
                report_text=report_text[:4000]
            ),
        }],
    )
    try:
        product_context: dict = _parse_json(ctx_resp.content[0].text)
    except Exception as exc:
        print(f"  [benchmark] Context parse failed: {exc} — using fallback")
        product_context = {
            "product_type": "web application",
            "industry": "technology",
            "region": "global",
            "workflow": "main workflow",
            "description": "A web application.",
        }

    region = product_context.get("region", "global")
    _prog(
        f"Product: {product_context.get('product_type')} | "
        f"Region: {region} | "
        f"Industry: {product_context.get('industry')}"
    )

    # ── Step 2: Identify competitors (3 regional + 2 global) ─────
    _prog(f"Identifying 3 regional ({region}) + 2 global competitors…")
    comp_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": _COMPETITOR_PROMPT.format(
                region=region,
                product_context=json.dumps(product_context, indent=2),
            ),
        }],
    )
    try:
        comp_data = _parse_json(comp_resp.content[0].text)
        competitors: list[dict] = comp_data.get("competitors", [])
    except Exception as exc:
        print(f"  [benchmark] Competitor parse failed: {exc}")
        competitors = []

    # Ensure correct ordering: regional first, then global
    regional = [c for c in competitors if c.get("scope") == "regional"][:3]
    global_  = [c for c in competitors if c.get("scope") == "global"][:2]
    competitors = regional + global_

    _prog(f"Found {len(regional)} regional + {len(global_)} global competitors.")

    # ── Step 3: Capture screenshots ───────────────────────────────
    for i, comp in enumerate(competitors, 1):
        label = f"[{comp.get('scope','?')[0].upper()}] {comp['name']}"
        _prog(f"Screenshotting {i}/{len(competitors)}: {label} ({comp['url']})…")
        comp["screenshot_bytes"] = _capture_screenshot(comp["url"])

    # ── Step 4: Build HTML report ─────────────────────────────────
    _prog("Building HTML report…")
    html = _build_html(product_context, competitors, api_url)

    # Strip screenshot bytes from return value (not serialisable)
    clean_competitors = [
        {k: v for k, v in c.items() if k != "screenshot_bytes"}
        for c in competitors
    ]

    return {
        "html": html,
        "product_context": product_context,
        "competitors": clean_competitors,
    }

# ── HTML generation ───────────────────────────────────────────────

def _scope_badge(scope: str) -> str:
    if scope == "global":
        return (
            '<span style="display:inline-block;padding:.18rem .55rem;'
            'background:#dbeafe;color:#1e40af;border-radius:9999px;'
            'font-size:.7rem;font-weight:700;letter-spacing:.04em;'
            'text-transform:uppercase;margin-left:.5rem;">GLOBAL</span>'
        )
    return (
        '<span style="display:inline-block;padding:.18rem .55rem;'
        'background:#fce7f3;color:#9d174d;border-radius:9999px;'
        'font-size:.7rem;font-weight:700;letter-spacing:.04em;'
        'text-transform:uppercase;margin-left:.5rem;">REGIONAL</span>'
    )

def _rel_badge(rel: str) -> str:
    color = "#fee2e2" if rel == "DIRECT" else "#fef9c3"
    text_color = "#991b1b" if rel == "DIRECT" else "#854d0e"
    return (
        f'<span style="display:inline-block;padding:.18rem .55rem;'
        f'background:{color};color:{text_color};border-radius:9999px;'
        f'font-size:.7rem;font-weight:700;letter-spacing:.04em;'
        f'text-transform:uppercase;">{rel}</span>'
    )

def _competitor_card(comp: dict, api_url: str) -> str:
    name        = comp.get("name", "Unknown")
    url         = comp.get("url", "#")
    description = comp.get("description", "")
    rel         = comp.get("relationship", "DIRECT")
    scope       = comp.get("scope", "regional")
    ss_bytes    = comp.get("screenshot_bytes")

    if ss_bytes:
        img_b64 = base64.b64encode(ss_bytes).decode()
        screenshot_html = (
            f'<img src="data:image/png;base64,{img_b64}" '
            f'alt="Screenshot of {name}" '
            f'style="width:100%;border:1px solid #e2e8f0;border-radius:8px;'
            f'display:block;margin-top:1rem;" />'
        )
    else:
        screenshot_html = (
            '<p style="color:#94a3b8;font-style:italic;margin-top:1rem;">'
            "Screenshots could not be captured.</p>"
        )

    # "Analyse this competitor" button posts to the parent window's analyze flow
    analyse_btn = ""
    if api_url:
        safe_url = url.replace('"', "&quot;")
        analyse_btn = f"""
        <button
          onclick="(function(){{
            var u='{safe_url}';
            if(window.parent&&window.parent.startAnalysis){{
              window.parent.startAnalysis(u);
            }} else {{
              window.open('{api_url}?analyse='+encodeURIComponent(u),'_blank');
            }}
          }})()"
          style="padding:.45rem 1rem;background:#0f172a;color:#fff;border:none;
                 border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600;
                 white-space:nowrap;display:flex;align-items:center;gap:.35rem;">
          &#128269; Analyse this competitor
        </button>"""

    scope_section = (
        '<span style="font-size:.75rem;color:#64748b;font-style:italic;margin-left:.75rem;">'
        + ("Same region" if scope == "regional" else "Global competitor")
        + "</span>"
    )

    return f"""
<div style="background:#fff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.08);
            padding:1.5rem;margin-bottom:1.25rem;">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.75rem;">
    <div style="display:flex;align-items:center;flex-wrap:wrap;gap:.25rem;">
      <span style="font-size:1.05rem;font-weight:700;color:#0f172a;">{name}</span>
      {_rel_badge(rel)}
      {_scope_badge(scope)}
      {scope_section}
    </div>
    {analyse_btn}
  </div>
  <div style="margin-top:.6rem;display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap;">
    <a href="{url}" target="_blank" rel="noopener"
       style="color:#3b82f6;font-size:.85rem;text-decoration:none;word-break:break-all;">{url}</a>
  </div>
  <p style="margin-top:.5rem;color:#475569;font-size:.88rem;line-height:1.5;">{description}</p>
  {screenshot_html}
</div>"""

def _build_html(product_context: dict, competitors: list[dict], api_url: str) -> str:
    regional_count = sum(1 for c in competitors if c.get("scope") == "regional")
    global_count   = sum(1 for c in competitors if c.get("scope") == "global")
    total          = len(competitors)

    # Context card
    ctx_items = [
        ("Product type", product_context.get("product_type", "—")),
        ("Industry",     product_context.get("industry", "—")),
        ("Region",       product_context.get("region", "—")),
        ("Workflow",     product_context.get("workflow", "—")),
    ]
    ctx_pills = "".join(
        f'<span style="margin-right:1.5rem;"><strong style="color:#0f172a;">{k}:</strong>'
        f'&nbsp;<span style="color:#475569;">{v}</span></span>'
        for k, v in ctx_items
    )
    description = product_context.get("description", "")
    context_card = f"""
<div style="background:#fff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.08);
            padding:1.5rem;margin-bottom:1.5rem;font-size:.9rem;">
  <div style="display:flex;flex-wrap:wrap;gap:.5rem 0;">{ctx_pills}</div>
  {"<p style='margin-top:.75rem;color:#475569;font-size:.88rem;'><strong>Description:</strong> "+description+"</p>" if description else ""}
</div>"""

    # Section headers
    regional_competitors = [c for c in competitors if c.get("scope") == "regional"]
    global_competitors   = [c for c in competitors if c.get("scope") == "global"]

    regional_section = ""
    if regional_competitors:
        region_label = product_context.get("region", "Region")
        regional_section = f"""
<h2 style="font-size:1rem;font-weight:700;color:#475569;margin:1.5rem 0 .75rem;
           display:flex;align-items:center;gap:.5rem;">
  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f9a8d4;"></span>
  Regional competitors &mdash; {region_label}
  <span style="font-size:.8rem;font-weight:400;color:#94a3b8;">({len(regional_competitors)} of 3)</span>
</h2>
{"".join(_competitor_card(c, api_url) for c in regional_competitors)}"""

    global_section = ""
    if global_competitors:
        global_section = f"""
<h2 style="font-size:1rem;font-weight:700;color:#475569;margin:1.5rem 0 .75rem;
           display:flex;align-items:center;gap:.5rem;">
  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#93c5fd;"></span>
  Global competitors
  <span style="font-size:.8rem;font-weight:400;color:#94a3b8;">({len(global_competitors)} of 2)</span>
</h2>
{"".join(_competitor_card(c, api_url) for c in global_competitors)}"""

    body = context_card + regional_section + global_section

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Competitive Benchmark</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f1f5f9; color: #1e293b; line-height: 1.6; }}
    header {{ background: #0f172a; color: #f8fafc; padding: 1.25rem 2rem;
      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .75rem; }}
    header h1 {{ font-size: 1.25rem; font-weight: 700; }}
    header p  {{ font-size: 0.82rem; color: #94a3b8; margin-top: .15rem; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 2rem; }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>&#128270; Competitive Benchmark</h1>
    <p>{total} competitors &nbsp;&middot;&nbsp; {regional_count} regional &nbsp;&middot;&nbsp; {global_count} global</p>
  </div>
</header>
<main>{body}</main>
</body>
</html>"""
