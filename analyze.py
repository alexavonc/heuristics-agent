import os
import urllib3
import httpx
import json
import re
import base64
import io
import html as _html_mod
from collections import defaultdict
urllib3.disable_warnings()
import anthropic
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
# ── Configuration ────────────────────────────────────────────────
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    raise RuntimeError("Set the ANTHROPIC_API_KEY environment variable.")
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT  = {"width": 390,  "height": 844}
SEVERITY_RGBA = {
    "Critical": (220, 38,  38,  220),
    "High":     (234, 88,  12,  220),
    "Medium":   (217, 119, 6,   220),
    "Low":      (37,  99,  235, 220),
}
SEVERITY_HEX = {
    "Critical": "#dc2626",
    "High":     "#ea580c",
    "Medium":   "#d97706",
    "Low":      "#2563eb",
}
HEURISTICS_PROMPT = """\
You are a senior UX researcher conducting a Nielsen heuristic evaluation.
Nielsen's 10 Heuristics for reference:
1. Visibility of system status
2. Match between system and the real world
3. User control and freedom
4. Consistency and standards
5. Error prevention
6. Recognition rather than recall
7. Flexibility and efficiency of use
8. Aesthetic and minimalist design
9. Help users recognize, diagnose, and recover from errors
10. Help and documentation
You have been given extracted content from a sales funnel page.
Evaluate it against these heuristics and produce a clear report.
For each issue found, provide:
- Which heuristic is violated (number + name)
- Where exactly on the page the issue occurs
- What the specific problem is
- A concrete recommendation to fix it
- Severity: Critical / High / Medium / Low
Also list any strengths you observe.
End with an overall score out of 10 and a one-paragraph summary.
Be specific. Reference the actual text, buttons, and labels you can see.
Avoid generic advice — tie everything back to the exact content provided.
IMPORTANT: After your full report, append a <LOCATIONS> block containing a JSON
array that maps each issue number to how to find it on the rendered page:
<LOCATIONS>
[
  {
    "issue_number": 1,
    "short_title": "4-6 word title",
    "severity": "High",
    "text_to_find": "exact short phrase visible on the page near the problem",
    "bbox_pct": {"x": 0.05, "y": 0.12, "w": 0.90, "h": 0.06}
  }
]
</LOCATIONS>
Rules for text_to_find:
- Use a short (3-8 word) string that is literally visible on the rendered page
- Pick text closest to the problematic element
- If the issue is about something MISSING (no CTA, no status bar), set to null
- Do not invent text — only use strings from the page content provided
Rules for bbox_pct (REQUIRED — always provide this):
- Estimate the bounding box of the problematic UI element as fractions of the image
- x, y = top-left corner (0.0 = left/top edge, 1.0 = right/bottom edge)
- w, h = width and height as fractions of the total image dimensions
- Be as precise as possible based on what you see
"""
JOURNEY_HEURISTICS_PROMPT = """\
You are a senior UX researcher conducting a Nielsen heuristic evaluation of a \
multi-step user journey. You will be given a series of screenshots and page \
content captured at each step of the journey, in order.
Nielsen's 10 Heuristics for reference:
1. Visibility of system status
2. Match between system and the real world
3. User control and freedom
4. Consistency and standards
5. Error prevention
6. Recognition rather than recall
7. Flexibility and efficiency of use
8. Aesthetic and minimalist design
9. Help users recognize, diagnose, and recover from errors
10. Help and documentation
Evaluate the ENTIRE FLOW end-to-end. For each issue found, provide:
- Which step(s) it occurs in (e.g. "Step 2 -> Step 3")
- Which heuristic is violated (number + name)
- What the specific problem is
- A concrete recommendation to fix it
- Severity: Critical / High / Medium / Low
Also evaluate:
- Flow continuity: does each screen logically follow from the previous?
- Progress visibility: does the user know where they are in the journey?
- Drop-off risks: where are users most likely to abandon?
- Cross-step consistency: do labels, tone, and design stay consistent?
List any strengths you observe.
End with an overall journey score out of 10 and a paragraph summary of the \
biggest friction points and quick wins.
Be specific. Reference actual text, buttons, and labels you can see in the \
screenshots. Tie everything back to the exact content provided.
IMPORTANT: After your full report, append a <LOCATIONS> block containing a JSON
array that maps each issue to the specific step and text where it can be found:
<LOCATIONS>
[
  {
    "issue_number": 1,
    "short_title": "4-6 word title",
    "severity": "High",
    "step_num": 0,
    "text_to_find": "exact short phrase visible on that step's page",
    "bbox_pct": {"x": 0.05, "y": 0.12, "w": 0.90, "h": 0.06}
  }
]
</LOCATIONS>
Rules for text_to_find:
- Use a short (3-8 word) string literally visible on that step's page
- step_num must match the step where the issue appears (use first step for cross-step issues)
- If the issue is about something MISSING, set text_to_find to null
- Do not invent text — only use strings from the page content provided
Rules for bbox_pct (REQUIRED — always provide this):
- Estimate the bounding box of the problematic UI element as fractions of the screenshot
- x, y = top-left corner (0.0 = left/top edge, 1.0 = right/bottom edge)
- w, h = width and height as fractions of total image dimensions
- Be as precise as possible based on what you see in the screenshot
"""
GENERAL_CHAT_SYSTEM_PROMPT = """\
You are a senior UX expert helping a designer or developer understand \
the findings in a heuristic evaluation report.
Be conversational, specific, and actionable. Explain the psychological \
principles behind heuristic violations, their real user impact, and \
concrete implementable solutions with examples. Connect findings across \
desktop and mobile viewports where relevant.
When the user quotes or highlights specific text from the report, focus \
on that but draw on the full report for broader context. Answer \
follow-up questions and engage with any UX topics raised.
Full evaluation report:
---
{report_text}
---
"""
# ── helpers ──────────────────────────────────────────────────────
def _safe_bytes(s: str) -> bytes:
    """Encode string to UTF-8, replacing any un-encodable / surrogate chars."""
    return s.encode("utf-8", errors="replace")
# ── Chat panel HTML/JS ────────────────────────────────────────────
def _chat_panel_html(api_url: str) -> str:
    return f"""  <div id="chat-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:998;"></div>
  <div id="chat-panel" style="display:none;position:fixed;right:0;top:0;width:440px;max-width:100vw;height:100vh;background:#fff;box-shadow:-4px 0 28px rgba(0,0,0,.18);z-index:999;flex-direction:column;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    <div style="background:#0f172a;color:#f8fafc;padding:1rem 1.25rem;display:flex;justify-content:space-between;align-items:flex-start;flex-shrink:0;">
      <div>
        <div style="font-weight:700;font-size:.95rem;">&#128172; Ask Claude</div>
        <div style="font-size:.72rem;color:#94a3b8;margin-top:.2rem;">Select any text on the page, then ask about it</div>
      </div>
      <button id="cp-close" style="flex-shrink:0;margin-left:.75rem;background:none;border:none;color:#94a3b8;font-size:1.5rem;line-height:1;cursor:pointer;padding:0;">&times;</button>
    </div>
    <div id="cp-msgs" style="flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:.75rem;"></div>
    <div style="padding:.75rem;border-top:1px solid #e2e8f0;display:flex;flex-direction:column;gap:.4rem;flex-shrink:0;background:#fff;">
      <div id="cp-ctx" style="display:none;background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:.45rem .6rem;font-size:.76rem;color:#1e40af;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.2rem;">
          <span style="font-weight:700;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#3b82f6;">Selected context</span>
          <button id="cp-ctx-clear" style="background:none;border:none;color:#93c5fd;cursor:pointer;font-size:1rem;line-height:1;padding:0;">&times;</button>
        </div>
        <div id="cp-ctx-text" style="overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;line-height:1.4;"></div>
      </div>
      <div style="display:flex;gap:.5rem;">
        <textarea id="cp-input" rows="2" placeholder="Ask about this report..."
          style="flex:1;padding:.5rem .75rem;border:1.5px solid #e2e8f0;border-radius:8px;font-size:.88rem;resize:none;font-family:inherit;outline:none;line-height:1.4;"></textarea>
        <button id="cp-send"
          style="align-self:flex-end;padding:.55rem 1rem;background:#0f172a;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:.88rem;">Send</button>
      </div>
    </div>
  </div>
  <script>
  var _chatApiUrl='{api_url}';
  var _chist=[],_cbusy=false,_selCtx='';
  function _gel(id){{return document.getElementById(id);}}
  function _cmsg(role,text){{
    var d=document.createElement('div');
    d.style.cssText=role==='user'
      ?'align-self:flex-end;background:#0f172a;color:#fff;padding:.45rem .75rem;border-radius:12px 12px 2px 12px;max-width:85%;font-size:.88rem;white-space:pre-wrap;word-wrap:break-word;'
      :'align-self:flex-start;background:#f1f5f9;color:#1e293b;padding:.45rem .75rem;border-radius:12px 12px 12px 2px;max-width:90%;font-size:.88rem;white-space:pre-wrap;word-wrap:break-word;line-height:1.6;';
    d.textContent=text;
    var m=_gel('cp-msgs');m.appendChild(d);m.scrollTop=m.scrollHeight;return d;
  }}
  function _setCtx(text){{
    _selCtx=text||'';
    var el=_gel('cp-ctx');
    if(_selCtx){{_gel('cp-ctx-text').textContent=_selCtx;el.style.display='block';}}
    else{{el.style.display='none';_gel('cp-ctx-text').textContent='';}}
  }}
  document.addEventListener('mouseup',function(e){{
    if(e.target.closest&&e.target.closest('#chat-panel'))return;
    var s=window.getSelection().toString().trim();
    if(s.length>10)_setCtx(s);
  }});
  document.addEventListener('keyup',function(e){{
    if(_gel('cp-input')&&e.target===_gel('cp-input'))return;
    if(e.shiftKey){{var s=window.getSelection().toString().trim();if(s.length>10)_setCtx(s);}}
  }});
  window.openChat=function(){{
    _gel('chat-overlay').style.display='block';
    _gel('chat-panel').style.display='flex';
    if(_chist.length===0){{
      _cmsg('assistant','Hi! Ask me anything about this evaluation. You can also select text anywhere on the page before sending to use it as context.');
    }}
    var inp=_gel('cp-input');if(inp)inp.focus();
  }};
  window.closeChat=function(){{
    _gel('chat-overlay').style.display='none';
    _gel('chat-panel').style.display='none';
  }};
  async function _cpSend(){{
    if(_cbusy)return;
    var inp=_gel('cp-input'),txt=inp.value.trim();
    if(!txt)return;
    var fullMsg=_selCtx?'Regarding this from the report:\\n"'+_selCtx+'"\\n\\n'+txt:txt;
    inp.value='';
    _setCtx('');
    _cmsg('user',fullMsg);
    _chist.push({{role:'user',content:fullMsg}});
    var bot=_cmsg('assistant','...');
    _cbusy=true;
    var sendBtn=_gel('cp-send');if(sendBtn)sendBtn.disabled=true;
    try{{
      var resp=await fetch(_chatApiUrl+'/api/chat',{{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{messages:_chist}})
      }});
      var reader=resp.body.getReader(),dec=new TextDecoder(),buf='',acc='';
      for(;;){{
        var r=await reader.read();if(r.done)break;
        buf+=dec.decode(r.value,{{stream:true}});
        var lines=buf.split('\\n');buf=lines.pop();
        for(var i=0;i<lines.length;i++){{
          var ln=lines[i];if(!ln.startsWith('data: '))continue;
          var chunk=ln.slice(6).trim();if(chunk==='[DONE]')break;
          try{{acc+=JSON.parse(chunk).text;bot.textContent=acc;_gel('cp-msgs').scrollTop=99999;}}catch(ex){{}}
        }}
      }}
      bot.textContent=acc||'(empty response)';
      _chist.push({{role:'assistant',content:acc}});
    }}catch(err){{
      bot.textContent='Error: could not reach chat server.';
      console.error('Chat fetch error:',err);
    }}
    _cbusy=false;if(sendBtn)sendBtn.disabled=false;
    var inp2=_gel('cp-input');if(inp2)inp2.focus();
  }}
  document.addEventListener('click',function(e){{
    var t=e.target;
    if(t===_gel('chat-overlay')){{window.closeChat();return;}}
    if(t.closest('#cp-close')){{window.closeChat();return;}}
    if(t.closest('#cp-ctx-clear')){{_setCtx('');return;}}
    if(t.closest('#cp-send')){{_cpSend();return;}}
    if(t.closest('#open-chat-btn')){{window.openChat();return;}}
  }});
  document.addEventListener('keydown',function(e){{
    if(_gel('cp-input')&&e.target===_gel('cp-input')&&e.key==='Enter'&&!e.shiftKey){{
      e.preventDefault();_cpSend();
    }}
  }});
  </script>
"""
# ── Step 1: Render page with Playwright ─────────────────────────
def _parse_html(url: str, html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "meta", "link", "noscript"]):
        tag.decompose()
    return {
        "url": url,
        "page_title": soup.title.get_text(strip=True) if soup.title else "No title found",
        "h1_headings": [h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)],
        "h2_headings": [h.get_text(strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)],
        "h3_headings": [h.get_text(strip=True) for h in soup.find_all("h3") if h.get_text(strip=True)],
        "buttons_and_ctas": list({
            el.get_text(strip=True)
            for el in soup.find_all(["button", "a"])
            if el.get_text(strip=True) and len(el.get_text(strip=True)) < 80
        })[:25],
        "form_fields": [
            {
                "label": label.get_text(strip=True),
                "input_type": (label.find_next("input") or {}).get("type", "text"),
                "placeholder": (label.find_next("input") or {}).get("placeholder", ""),
            }
            for label in soup.find_all("label")
            if label.get_text(strip=True)
        ],
        "images": [
            {"alt_text": img.get("alt", "MISSING ALT TEXT")}
            for img in soup.find_all("img")
        ][:10],
        "visible_text_sample": " ".join(soup.get_text(separator=" ", strip=True).split())[:4000],
    }
def playwright_scrape_and_screenshot(url: str, viewport: dict = None) -> tuple[dict, bytes]:
    vp = viewport or DESKTOP_VIEWPORT
    print(f"\n  Launching browser for {url} ({vp['width']}x{vp['height']}) ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=vp)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        html_content = page.content()
        screenshot_bytes = page.screenshot(full_page=True)
        browser.close()
    return _parse_html(url, html_content), screenshot_bytes
# ── Journey: execute steps and capture state ─────────────────────
def _capture_state(page, label: str, step_num: int) -> dict:
    html_content = page.content()
    screenshot_bytes = page.screenshot(full_page=True)
    content = _parse_html(page.url, html_content)
    return {
        "step_num": step_num,
        "label": label,
        "url": page.url,
        "content": content,
        "screenshot_bytes": screenshot_bytes,
    }
def playwright_journey_scrape(url: str, steps: list[dict], viewport: dict = None) -> list[dict]:
    vp = viewport or DESKTOP_VIEWPORT
    print(f"\n  Launching browser for journey starting at {url} ({vp['width']}x{vp['height']}) ...")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=vp)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        results.append(_capture_state(page, "Initial Page", 0))
        print(f"  Step 0: Initial Page captured")
        for i, step in enumerate(steps, 1):
            action = step.get("action", "")
            label = step.get("label", f"Step {i}: {action}")
            try:
                if action == "click_text":
                    page.get_by_text(step["value"], exact=False).first.click()
                elif action == "click_selector":
                    page.click(step["value"])
                elif action == "fill":
                    page.fill(step["selector"], step.get("value", ""))
                elif action == "fill_label":
                    page.get_by_label(step["label"], exact=False).fill(step.get("value", ""))
                elif action == "navigate":
                    page.goto(step["url"], wait_until="networkidle", timeout=60000)
                elif action == "wait":
                    page.wait_for_timeout(step.get("ms", 2000))
                elif action == "scroll":
                    page.mouse.wheel(0, step.get("amount", 500))
                elif action == "hover":
                    page.hover(step["value"])
                elif action == "press":
                    page.keyboard.press(step.get("key", "Enter"))
                else:
                    print(f"  Step {i}: Unknown action '{action}', skipping")
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                results.append(_capture_state(page, label, i))
                print(f"  Step {i}: '{label}' captured ({page.url})")
            except Exception as e:
                print(f"  Step {i}: '{label}' FAILED — {e}")
                results.append(_capture_state(page, f"{label} [FAILED]", i))
        browser.close()
    return results
# ── Step 2: Format content for Claude ───────────────────────────
def format_for_prompt(content: dict) -> str:
    lines = [
        f"URL: {content['url']}",
        f"Page Title: {content['page_title']}",
        "",
        "── Headings ──",
        f"H1: {content['h1_headings'] or 'None found'}",
        f"H2: {content['h2_headings'] or 'None found'}",
        f"H3: {content['h3_headings'] or 'None found'}",
        "",
        "── Buttons & CTAs ──",
    ]
    for cta in content["buttons_and_ctas"]:
        lines.append(f"  • {cta}")
    lines += ["", "── Form Fields ──"]
    if content["form_fields"]:
        for field in content["form_fields"]:
            lines.append(
                f"  • Label: '{field['label']}' | Type: {field['input_type']} | Placeholder: '{field['placeholder']}'"
            )
    else:
        lines.append("  No form fields detected")
    lines += ["", "── Images (alt text) ──"]
    for img in content["images"]:
        lines.append(f"  • {img['alt_text']}")
    lines += ["", "── Visible Page Text (excerpt) ──", content["visible_text_sample"]]
    return "\n".join(lines)
def _resize_screenshot(screenshot_bytes: bytes, max_dim: int = 7900) -> bytes:
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
# ── Step 3a: Single page evaluation ─────────────────────────────
def call_claude(formatted: str, viewport_label: str = "desktop") -> str:
    print(f"  Sending {viewport_label} view to Claude for heuristic evaluation ...")
    client = anthropic.Anthropic(
        api_key=API_KEY,
        http_client=httpx.Client(verify=False),
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=HEURISTICS_PROMPT,
        messages=[{"role": "user", "content": f"Please evaluate this {viewport_label} page:\n\n{formatted}"}],
    )
    return response.content[0].text
# ── Step 3b: Journey evaluation (with screenshots) ───────────────
def call_claude_journey(steps_data: list[dict], viewport_label: str = "desktop") -> str:
    print(f"  Sending {len(steps_data)}-step {viewport_label} journey to Claude for evaluation ...")
    client = anthropic.Anthropic(
        api_key=API_KEY,
        http_client=httpx.Client(verify=False),
    )
    content_blocks: list = [
        {
            "type": "text",
            "text": (
                f"I am providing a {len(steps_data)}-step user journey ({viewport_label} viewport) "
                f"for heuristic evaluation. Each step includes a screenshot and page content summary. "
                f"Please evaluate the entire flow end-to-end.\n\n"
            ),
        }
    ]
    for step in steps_data:
        content_blocks.append({
            "type": "text",
            "text": (
                f"--- STEP {step['step_num']}: {step['label']} ---\n"
                f"URL: {step['url']}\n"
                f"{format_for_prompt(step['content'])}\n\n"
                f"Screenshot for Step {step['step_num']}:"
            ),
        })
        img = Image.open(io.BytesIO(step["screenshot_bytes"])).convert("RGB")
        img.thumbnail((900, 900), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img_b64,
            },
        })
    content_blocks.append({
        "type": "text",
        "text": "Please evaluate this complete user journey against Nielsen's 10 heuristics.",
    })
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=JOURNEY_HEURISTICS_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
    ) as stream:
        print("  Receiving evaluation", end="", flush=True)
        chunks = []
        for text in stream.text_stream:
            chunks.append(text)
            print(".", end="", flush=True)
        print(" done.")
        return "".join(chunks)
# ── Step 4: Parse Claude response ───────────────────────────────
def parse_response(full_text: str) -> tuple[str, list]:
    locations = []
    match = re.search(r"<LOCATIONS>\s*([\s\S]*?)\s*</LOCATIONS>", full_text)
    if match:
        try:
            locations = json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    report_text = re.sub(r"<LOCATIONS>[\s\S]*?</LOCATIONS>", "", full_text).strip()
    return report_text, locations
# ── Step 5: Locate elements with Playwright ─────────────────────
_JS_FIND_TEXT = """
(text) => {
    const lower = text.trim().toLowerCase();
    function rectForEl(el) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while ((node = walker.nextNode())) {
        if (node.textContent.trim().toLowerCase().includes(lower)) {
            const el = node.parentElement;
            if (!el) continue;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const r = rectForEl(el);
            if (r) return r;
        }
    }
    const all = Array.from(document.querySelectorAll('*'));
    for (const el of all) {
        if ((el.innerText || '').trim().toLowerCase().includes(lower)) {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const r = rectForEl(el);
            if (r) return r;
        }
    }
    return null;
}
"""
def locate_elements(url: str, locations: list, viewport: dict = None) -> list:
    if not locations:
        return []
    vp = viewport or DESKTOP_VIEWPORT
    found = []
    print(f"  Locating issues on the rendered page ({vp['width']}x{vp['height']}) ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=vp)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        for loc in locations:
            text = loc.get("text_to_find")
            if not text:
                continue
            bbox = None
            try:
                locator = page.get_by_text(text, exact=False).first
                b = locator.bounding_box(timeout=2000)
                if b and b["width"] > 0 and b["height"] > 0:
                    bbox = b
            except Exception:
                pass
            if not bbox:
                try:
                    b = page.evaluate(_JS_FIND_TEXT, text)
                    if b and b.get("width", 0) > 0 and b.get("height", 0) > 0:
                        bbox = b
                except Exception:
                    pass
            if not bbox and len(text.split()) > 3:
                short = " ".join(text.split()[:4])
                try:
                    b = page.evaluate(_JS_FIND_TEXT, short)
                    if b and b.get("width", 0) > 0 and b.get("height", 0) > 0:
                        bbox = b
                except Exception:
                    pass
            if bbox:
                found.append((
                    loc["issue_number"],
                    bbox,
                    loc.get("short_title", f"Issue {loc['issue_number']}"),
                    loc.get("severity", "Medium"),
                ))
            else:
                print(f"    Could not locate text: {text!r}")
        browser.close()
    total = len([l for l in locations if l.get("text_to_find")])
    print(f"  Located {len(found)} of {total} issues on the page.")
    return found
# ── Step 6: Annotate screenshot ──────────────────────────────────
def _load_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()
def annotate_screenshot(screenshot_bytes: bytes, found: list) -> bytes:
    if not found:
        return screenshot_bytes
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_badge = _load_font(13)
    for issue_num, bbox, short_title, severity in found:
        color = SEVERITY_RGBA.get(severity, SEVERITY_RGBA["Medium"])
        x, y = bbox["x"], bbox["y"]
        w, h = bbox["width"], bbox["height"]
        draw.rectangle([x, y, x + w, y + h], fill=(*color[:3], 40))
        for thickness in range(3):
            draw.rectangle(
                [x - thickness, y - thickness, x + w + thickness, y + h + thickness],
                outline=(*color[:3], 230),
            )
        badge_text = f"#{issue_num}"
        badge_w, badge_h = 32, 20
        bx = x
        by = y - badge_h - 2 if y >= badge_h + 2 else y + h + 2
        draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=4, fill=(*color[:3], 240))
        draw.text((bx + 4, by + 2), badge_text, fill=(255, 255, 255, 255), font=font_badge)
    combined = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    combined.save(out, format="PNG")
    return out.getvalue()
def crop_to_issue_regions(screenshot_bytes: bytes, found: list, padding: int = 80) -> list[tuple]:
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    w, h = img.size
    if not found:
        thumb = img.copy()
        thumb.thumbnail((600, 900), Image.LANCZOS)
        out = io.BytesIO()
        thumb.save(out, format="PNG")
        return [(-1, "No issues found", None, out.getvalue())]
    crops = []
    for issue_num, bbox, short_title, severity in found:
        x1 = max(0, int(bbox["x"]) - padding)
        y1 = max(0, int(bbox["y"]) - padding)
        x2 = min(w, int(bbox["x"] + bbox["width"]) + padding)
        y2 = min(h, int(bbox["y"] + bbox["height"]) + padding)
        cropped = img.crop((x1, y1, x2, y2))
        out = io.BytesIO()
        cropped.save(out, format="PNG")
        crops.append((issue_num, short_title, severity, out.getvalue()))
    return crops
def annotate_screenshot_from_locs(screenshot_bytes: bytes, locations: list) -> tuple[bytes, list]:
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    W, H = img.size
    found = []
    for loc in locations:
        bp = loc.get("bbox_pct")
        if not bp:
            continue
        x = bp.get("x", 0) * W
        y = bp.get("y", 0) * H
        w = bp.get("w", 0.1) * W
        h = bp.get("h", 0.05) * H
        if w > 0 and h > 0:
            found.append((
                loc["issue_number"],
                {"x": x, "y": y, "width": w, "height": h},
                loc.get("short_title", f"Issue {loc['issue_number']}"),
                loc.get("severity", "Medium"),
            ))
    print(f"  Annotating {len(found)} issue(s) from bbox coordinates.")
    annotated = annotate_screenshot(screenshot_bytes, found)
    return annotated, found
# ── HTML helpers ─────────────────────────────────────────────────
RUBRIC = [
    (9, 10, "#16a34a", "Excellent",  "Near-perfect usability, minor polish only"),
    (7,  8, "#2563eb", "Good",       "Functional with some friction points"),
    (5,  6, "#d97706", "Needs Work", "Notable violations affecting key flows"),
    (3,  4, "#ea580c", "Poor",       "Significant UX failures hurting conversions"),
    (0,  2, "#dc2626", "Critical",   "Fundamental redesign required"),
]
SEV_WEIGHTS = {"Critical": 2.0, "High": 1.5, "Medium": 0.75, "Low": 0.25}
def _extract_score(report_text: str) -> float | None:
    m = re.search(r'\b(\d+(?:\.\d+)?)\s*/\s*10\b', report_text, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        if 0 <= v <= 10:
            return v
    return None
def _score_color_py(score: float) -> str:
    if score >= 9: return "#16a34a"
    if score >= 7: return "#2563eb"
    if score >= 5: return "#d97706"
    if score >= 3: return "#ea580c"
    return "#dc2626"
def _score_label_py(score: float) -> str:
    if score >= 9: return "Excellent"
    if score >= 7: return "Good"
    if score >= 5: return "Needs Work"
    if score >= 3: return "Poor"
    return "Critical"
def _rubric_rows_html(score: float, vp: str) -> str:
    rows = ""
    for mn, mx, col, label, desc in RUBRIC:
        active = mn <= score <= mx
        bg     = "rgba(99,102,241,.12)" if active else "transparent"
        border = "3px solid #6366f1"    if active else "3px solid transparent"
        tc     = "#f8fafc"              if active else "#94a3b8"
        rows += (
            f'<div class="rr" data-min="{mn}" data-max="{mx}" '
            f'style="display:flex;align-items:baseline;gap:.5rem;padding:.3rem .5rem;'
            f'border-radius:5px;border-left:{border};background:{bg};margin-bottom:.25rem;">'
            f'<span style="font-size:.78rem;font-weight:700;color:{col};min-width:2.25rem;">{mn}–{mx}</span>'
            f'<span style="font-size:.78rem;font-weight:700;color:{tc};">{label}</span>'
            f'<span style="font-size:.75rem;color:#64748b;"> — {desc}</span>'
            f'</div>'
        )
    return rows
def _score_card_html(score_val: float, vp: str) -> str:
    sc = _score_color_py(score_val)
    sl = _score_label_py(score_val)
    return f"""
    <div class="legend-card">
      <div style="display:flex;align-items:flex-start;gap:2rem;flex-wrap:wrap;">
        <div style="text-align:center;min-width:110px;flex-shrink:0;">
          <div id="score-display-{vp}" style="font-size:2.75rem;font-weight:800;color:{sc};">
            <span id="score-num-{vp}">{score_val:.1f}</span><span style="font-size:1rem;font-weight:400;color:#94a3b8;">&thinsp;/ 10</span>
          </div>
          <div style="font-size:.78rem;color:#64748b;margin-top:.15rem;">Overall Score</div>
          <div id="score-label-{vp}" style="font-size:.78rem;font-weight:700;color:{sc};margin-top:.2rem;">{sl}</div>
          <div id="ignored-count-{vp}" style="font-size:.72rem;color:#94a3b8;margin-top:.4rem;min-height:1em;"></div>
        </div>
        <div style="flex:1;min-width:200px;">
          <div style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:.6rem;">Score Guide</div>
          <div id="rubric-{vp}">{_rubric_rows_html(score_val, vp)}</div>
        </div>
      </div>
    </div>"""
def _score_init_script(vp: str, score_val: float, locations: list) -> str:
    weights_map = {loc["issue_number"]: SEV_WEIGHTS.get(loc.get("severity", "Medium"), 0.75) for loc in locations}
    return (
        f"<script>(function(){{"
        f"var vp='{vp}';"
        f"window._uxScores=window._uxScores||{{}};"
        f"window._uxWeights=window._uxWeights||{{}};"
        f"window._uxIgnored=window._uxIgnored||{{}};"
        f"window._uxScores[vp]={score_val};"
        f"window._uxWeights[vp]={json.dumps(weights_map)};"
        f"window._uxIgnored[vp]={{}};"
        f"}})();</script>"
    )
def _extract_issue_details(report_text: str) -> dict:
    result = {}
    issue_header = re.compile(
        r'(?m)^(?:#{1,4}\s*)?(?:\*\*)?Issue\s+(\d+)(?:\*\*)?[:\s]',
        re.IGNORECASE,
    )
    headers = list(issue_header.finditer(report_text))
    for i, m in enumerate(headers):
        issue_num = int(m.group(1))
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(report_text)
        block = report_text[start:end]
        detail = {}
        hm = re.search(r'\*\*Heuristics?\*\*[:\s*]+(.+?)(?:\n|$)', block, re.IGNORECASE)
        if hm:
            detail['heuristic'] = hm.group(1).strip().rstrip('*').strip()
        pm = re.search(r'\*\*Problem\*\*[:\s*]+(.+?)(?=\n\s*[-*]\s*\*\*|\Z)', block, re.IGNORECASE | re.DOTALL)
        if pm:
            detail['problem'] = ' '.join(pm.group(1).split())
        rm = re.search(r'\*\*Recommendation\*\*[:\s*]+(.+?)(?=\n\s*[-*]\s*\*\*|\Z)', block, re.IGNORECASE | re.DOTALL)
        if rm:
            detail['recommendation'] = ' '.join(rm.group(1).split())
        if detail:
            result[issue_num] = detail
    return result
def _renumber_issues_in_report(report_text: str, offset: int) -> str:
    """Add offset to every issue number in a report text block."""
    if offset == 0:
        return report_text
    def _replace(m):
        return m.group(0).replace(m.group(1), str(int(m.group(1)) + offset), 1)
    return re.sub(
        r'(?mi)^((?:#{1,4}\s*)?(?:\*\*)?Issue\s+)(\d+)',
        lambda m: m.group(1) + str(int(m.group(2)) + offset),
        report_text,
    )

def _escape_report(report_text: str) -> str:
    escaped = (
        report_text
        .encode("utf-8", errors="replace").decode("utf-8")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    for sev, color in SEVERITY_HEX.items():
        escaped = escaped.replace(
            f"**{sev}**",
            f'<strong style="color:{color}">{sev}</strong>',
        )
        escaped = escaped.replace(
            f"Severity:** **{sev}**",
            f'Severity: <strong style="color:{color}">{sev}</strong>',
        )
    return escaped
def _viewport_tab_html(desktop_content: str, mobile_content: str) -> str:
    return f"""
    <div id="vp-desktop">{desktop_content}</div>
    <div id="vp-mobile" style="display:none">{mobile_content}</div>"""
def _modal_html() -> str:
    return """
<div id="ss-modal" style="display:none;position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.93);flex-direction:column;" onclick="if(event.target.id==='ss-modal')_ssClose()">
  <div style="background:#0f172a;padding:.6rem 1.25rem;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;border-bottom:1px solid #1e293b;">
    <span id="ss-modal-hint" style="color:#94a3b8;font-size:.82rem;font-family:system-ui,sans-serif;">Click any highlighted region to see issue details &nbsp;&middot;&nbsp; Esc to close</span>
    <button onclick="_ssClose()" style="background:none;border:none;color:#64748b;font-size:1.75rem;cursor:pointer;line-height:1;padding:0;">&times;</button>
  </div>
  <div style="display:flex;flex:1;overflow:hidden;min-height:0;">
    <div style="flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:1.5rem;min-width:0;">
      <div style="position:relative;display:inline-block;line-height:0;max-width:100%;">
        <img id="ss-modal-img" style="display:block;max-height:calc(100vh - 120px);max-width:100%;border-radius:6px;box-shadow:0 8px 60px rgba(0,0,0,.7);" />
        <div id="ss-overlays" style="position:absolute;inset:0;pointer-events:none;"></div>
      </div>
    </div>
    <div id="ss-issue-panel" style="display:none;width:340px;background:#1e293b;flex-direction:column;flex-shrink:0;border-left:1px solid #334155;">
      <div style="padding:.85rem 1rem;border-bottom:1px solid #334155;flex-shrink:0;position:sticky;top:0;background:#1e293b;z-index:1;">
        <span style="font-weight:700;color:#f8fafc;font-size:.88rem;font-family:system-ui,sans-serif;">Issue Details</span>
      </div>
      <div id="ss-issue-content" style="padding:1.25rem;overflow-y:auto;flex:1;"></div>
    </div>
  </div>
</div>
<script>
(function() {
  var _sevC = {Critical:'#dc2626',High:'#ea580c',Medium:'#d97706',Low:'#2563eb'};
  var _ssCurrentVP = null;
  var _ssCurrentIssues = [];
  function _sc(s){return _sevC[s]||'#d97706';}
  function _esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function _ssField(label, value) {
    if(!value) return '';
    return '<div style="margin-bottom:.85rem;">' +
      '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#475569;font-family:system-ui,sans-serif;margin-bottom:.3rem;">'+label+'</div>' +
      '<p style="font-size:.82rem;color:#cbd5e1;line-height:1.55;font-family:system-ui,sans-serif;">'+_esc(value)+'</p>' +
    '</div>';
  }
  window._ssOpen = function(src, issues, viewport) {
    _ssCurrentVP = viewport || null;
    _ssCurrentIssues = issues || [];
    var modal = document.getElementById('ss-modal');
    var img   = document.getElementById('ss-modal-img');
    var hint  = document.getElementById('ss-modal-hint');
    document.getElementById('ss-issue-panel').style.display = 'none';
    document.getElementById('ss-overlays').innerHTML = '';
    var hasOverlays = _ssCurrentIssues.some(function(i){return i.bbox_pct;});
    hint.textContent = hasOverlays
      ? 'Click any highlighted region to see issue details \u00b7 Esc to close'
      : 'Esc to close';
    img.src = src;
    modal.style.display = 'flex';
    function render() { _ssRenderOverlays(); }
    if(img.complete && img.naturalWidth){render();}else{img.onload=render;}
  };
  function _ssRenderOverlays() {
    var wrap = document.getElementById('ss-overlays');
    wrap.innerHTML = '';
    wrap.style.pointerEvents = 'auto';
    var ignored = (_ssCurrentVP && window._uxIgnored && window._uxIgnored[_ssCurrentVP]) || {};
    _ssCurrentIssues.forEach(function(issue) {
      var b = issue.bbox_pct; if(!b) return;
      var color = _sc(issue.severity);
      var isIgnored = !!ignored[issue.issue_number];
      var div = document.createElement('div');
      div.dataset.issueNum = issue.issue_number;
      div.style.cssText = 'position:absolute;box-sizing:border-box;cursor:pointer;' +
        'left:'+(b.x*100)+'%;top:'+(b.y*100)+'%;' +
        'width:'+(b.w*100)+'%;height:'+(b.h*100)+'%;' +
        'border:3px '+(isIgnored?'dashed':'solid')+' '+color+';' +
        'background:'+color+(isIgnored?'14':'26')+';' +
        'opacity:'+(isIgnored?'.45':'1')+';transition:background .15s;';
      var badge = document.createElement('div');
      badge.textContent = '#'+issue.issue_number+(isIgnored?' (ignored)':'');
      badge.style.cssText = 'position:absolute;top:-24px;left:0;pointer-events:none;' +
        'background:'+color+';color:#fff;padding:1px 8px;border-radius:4px;' +
        'font-size:12px;font-weight:700;white-space:nowrap;font-family:system-ui,sans-serif;' +
        (isIgnored?'opacity:.6;':'');
      div.appendChild(badge);
      div.onmouseover = function(){if(!ignored[issue.issue_number])div.style.background=color+'44';};
      div.onmouseout  = function(){div.style.background=color+(ignored[issue.issue_number]?'14':'26');};
      div.onclick = function(e){e.stopPropagation();window._ssShowIssue(issue);};
      wrap.appendChild(div);
    });
  }
  window._ssShowIssue = function(issue) {
    var vp      = _ssCurrentVP;
    var color   = _sc(issue.severity);
    var panel   = document.getElementById('ss-issue-panel');
    var content = document.getElementById('ss-issue-content');
    var hasChat = !!document.getElementById('cp-input');
    var ignored = (vp && window._uxIgnored && window._uxIgnored[vp] && window._uxIgnored[vp][issue.issue_number]);
    var ignLabel = ignored ? '\u21a9 Unignore' : '\ud83d\udeab Ignore this issue';
    var ignStyle = ignored
      ? 'width:100%;padding:.5rem;background:#334155;color:#94a3b8;border:1px solid #475569;border-radius:8px;font-size:.82rem;cursor:pointer;font-family:system-ui,sans-serif;margin-bottom:.5rem;'
      : 'width:100%;padding:.5rem;background:transparent;color:#64748b;border:1px solid #334155;border-radius:8px;font-size:.82rem;cursor:pointer;font-family:system-ui,sans-serif;margin-bottom:.5rem;';
    var ignBtn = vp
      ? '<button id="ss-ign-btn" style="'+ignStyle+'">'+ignLabel+'</button>'
      : '';
    var chatBtn = hasChat
      ? '<button id="ss-ask-btn" style="width:100%;padding:.65rem;background:#6366f1;color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:.88rem;font-family:system-ui,sans-serif;">&#128172; Ask Claude about this</button>'
      : '';
    content.innerHTML =
      '<div style="display:flex;align-items:center;gap:.6rem;margin-bottom:1rem;">' +
        '<span style="background:'+color+';color:#fff;border-radius:6px;padding:.25rem .7rem;font-size:1rem;font-weight:700;font-family:system-ui,sans-serif;">#'+issue.issue_number+'</span>' +
        '<span style="background:'+color+'33;color:'+color+';border-radius:9999px;padding:.15rem .65rem;font-size:.78rem;font-weight:600;font-family:system-ui,sans-serif;">'+_esc(issue.severity)+'</span>' +
      '</div>' +
      '<h3 style="font-size:.95rem;font-weight:700;color:#f8fafc;margin-bottom:1rem;font-family:system-ui,sans-serif;line-height:1.4;">'+_esc(issue.short_title)+'</h3>' +
      '<div style="border-top:1px solid #334155;padding-top:.9rem;margin-bottom:1rem;">' +
        _ssField('Heuristic', issue.heuristic) +
        _ssField('Problem', issue.problem) +
        _ssField('Recommendation', issue.recommendation) +
      '</div>' +
      ignBtn +
      chatBtn;
    if(vp) {
      var ib = document.getElementById('ss-ign-btn');
      if(ib) ib.onclick = function(){ window._toggleIgnore(vp, issue.issue_number); window._ssShowIssue(issue); };
    }
    if(hasChat){
      var ab = document.getElementById('ss-ask-btn');
      if(ab) ab.onclick = function(){
        window._ssClose();
        if(window.openChat) window.openChat();
        setTimeout(function(){
          var inp = document.getElementById('cp-input');
          if(inp){
            inp.value = 'Explain Issue #'+issue.issue_number+': "'+((issue.short_title||'').replace(/"/g,"'"))+'". What is the exact UX problem, why does it matter to users, and what are the specific steps to fix it?';
            inp.focus();
          }
        }, 80);
      };
    }
    panel.style.display = 'flex';
  };
  window._ssCropClick = function(src, issueData, viewport) {
    _ssCurrentVP = viewport || null;
    _ssCurrentIssues = [];
    var modal = document.getElementById('ss-modal');
    var img   = document.getElementById('ss-modal-img');
    document.getElementById('ss-overlays').innerHTML = '';
    document.getElementById('ss-modal-hint').textContent = 'Esc to close';
    img.src = src;
    modal.style.display = 'flex';
    window._ssShowIssue(issueData);
  };
  window._ssClose = function(){
    document.getElementById('ss-modal').style.display = 'none';
  };
  window._toggleIgnore = function(vp, issueNum) {
    if(!vp || !window._uxIgnored) return;
    window._uxIgnored[vp] = window._uxIgnored[vp] || {};
    window._uxIgnored[vp][issueNum] = !window._uxIgnored[vp][issueNum];
    _recalcScore(vp);
    _updateLegendRow(vp, issueNum, !!window._uxIgnored[vp][issueNum]);
    var modal = document.getElementById('ss-modal');
    if(modal && modal.style.display !== 'none' && vp === _ssCurrentVP) {
      _ssRenderOverlays();
    }
  };
  function _recalcScore(vp) {
    if(!window._uxScores || window._uxScores[vp] === undefined) return;
    var base    = window._uxScores[vp];
    var weights = (window._uxWeights || {})[vp] || {};
    var ignored = (window._uxIgnored  || {})[vp] || {};
    var adj = 0, cnt = 0;
    for(var n in ignored){ if(ignored[n]){ adj += (weights[n]||0); cnt++; } }
    var score = Math.min(10, base + adj);
    var numEl = document.getElementById('score-num-'+vp);
    if(numEl) numEl.textContent = score.toFixed(1);
    var sc = _scoreColor(score);
    var disp = document.getElementById('score-display-'+vp);
    if(disp) disp.style.color = sc;
    var lbl = document.getElementById('score-label-'+vp);
    if(lbl){ lbl.textContent = _scoreLabel(score); lbl.style.color = sc; }
    var cntEl = document.getElementById('ignored-count-'+vp);
    if(cntEl) cntEl.textContent = cnt > 0 ? cnt+' issue'+(cnt===1?'':'s')+' ignored' : '';
    _updateRubricHighlight(vp, score);
  }
  function _scoreColor(s){
    if(s>=9)return'#16a34a';if(s>=7)return'#2563eb';if(s>=5)return'#d97706';if(s>=3)return'#ea580c';return'#dc2626';
  }
  function _scoreLabel(s){
    if(s>=9)return'Excellent';if(s>=7)return'Good';if(s>=5)return'Needs Work';if(s>=3)return'Poor';return'Critical';
  }
  function _updateRubricHighlight(vp, score) {
    var rows = document.querySelectorAll('#rubric-'+vp+' .rr');
    rows.forEach(function(row){
      var mn = parseFloat(row.dataset.min), mx = parseFloat(row.dataset.max);
      var active = score >= mn && score <= mx;
      row.style.background    = active ? 'rgba(99,102,241,.12)' : 'transparent';
      row.style.borderLeft    = active ? '3px solid #6366f1'    : '3px solid transparent';
      row.querySelector('span:nth-child(2)').style.color = active ? '#f8fafc' : '#94a3b8';
    });
  }
  function _updateLegendRow(vp, issueNum, isIgnored) {
    var row = document.getElementById('leg-row-'+vp+'-'+issueNum);
    if(row){ row.style.opacity = isIgnored?'.45':'1'; }
    var cells = row ? row.querySelectorAll('td') : [];
    cells.forEach(function(td, i){ if(i<3) td.style.textDecoration = isIgnored?'line-through':''; });
    var btn = document.getElementById('ign-leg-btn-'+vp+'-'+issueNum);
    if(btn){
      btn.textContent = isIgnored ? 'Unignore' : 'Ignore';
      btn.style.background   = isIgnored ? '#1e293b' : 'white';
      btn.style.color        = isIgnored ? '#94a3b8' : '#64748b';
      btn.style.borderColor  = isIgnored ? '#475569' : '#cbd5e1';
    }
  }
  document.addEventListener('keydown',function(e){if(e.key==='Escape')window._ssClose();});
})();
</script>
"""
def _html_shell(title: str, subtitle: str, body: str, extra_css: str = "", api_url: str = None) -> str:
    chat_btn = """<button id="open-chat-btn" style="margin-left:auto;display:flex;align-items:center;gap:.4rem;padding:.45rem 1rem;background:#6366f1;color:#fff;border:none;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:background .15s;" onmouseover="this.style.background='#4f46e5'" onmouseout="this.style.background='#6366f1'">&#128172; Ask Claude</button>""" if api_url else ""
    chat_panel = _chat_panel_html(api_url) if api_url else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f1f5f9; color: #1e293b; line-height: 1.6; }}
    header {{ background: #0f172a; color: #f8fafc; padding: 1.25rem 2rem; }}
    header h1 {{ font-size: 1.4rem; font-weight: 700; }}
    header p  {{ font-size: 0.85rem; color: #94a3b8; margin-top: 0.25rem; }}
    #top-bar {{ position: sticky; top: 0; z-index: 100; background: #1e293b;
      padding: 0.55rem 2rem; display: flex; align-items: center; gap: 0.75rem;
      box-shadow: 0 2px 8px rgba(0,0,0,.25); }}
    .vp-tab {{ padding: .35rem 1rem; border: 1.5px solid #475569; background: transparent;
      border-radius: 6px; font-size: .85rem; font-weight: 600; cursor: pointer;
      color: #94a3b8; transition: all .15s; }}
    .vp-tab.active {{ background: #f8fafc; color: #0f172a; border-color: #f8fafc; }}
    .vp-tab:hover:not(.active) {{ border-color: #94a3b8; color: #e2e8f0; }}
    main {{ max-width: 1300px; margin: 0 auto; padding: 2rem; }}
    .screenshot-card, .legend-card, .report-card {{
      background: white; border-radius: 12px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.08); padding: 1.5rem; margin-bottom: 2rem;
    }}
    .screenshot-card h2, .legend-card h2, .report-card h2 {{
      margin-bottom: 1rem; font-size: 1.1rem; color: #475569; }}
    .screenshot-card img {{ width: 100%; border: 1px solid #e2e8f0; border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th {{ text-align: left; padding: 0.5rem 0.75rem; background: #f8fafc;
      border-bottom: 2px solid #e2e8f0; color: #64748b; font-weight: 600; }}
    td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }}
    .badge {{ display: inline-block; width: 36px; text-align: center; padding: 0.2rem 0;
      border-radius: 6px; color: white; font-weight: 700; font-size: 0.8rem; }}
    .sev-pill {{ display: inline-block; padding: 0.15rem 0.6rem; border-radius: 9999px;
      color: white; font-weight: 600; font-size: 0.75rem; }}
    .report-body {{ font-size: 0.92rem; white-space: pre-wrap; word-wrap: break-word;
      color: #334155; line-height: 1.75; }}
    ::selection {{ background: #bfdbfe; color: #1e3a8a; }}
    {extra_css}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>{subtitle}</p>
  </header>
  <div id="top-bar">
    <button class="vp-tab active" onclick="switchVP('desktop',this)">&#128760; Desktop (1440px)</button>
    <button class="vp-tab" onclick="switchVP('mobile',this)">&#128241; Mobile (390px)</button>
    {chat_btn}
  </div>
  <script>
  function switchVP(name,btn){{
    document.getElementById('vp-desktop').style.display=name==='desktop'?'':'none';
    document.getElementById('vp-mobile').style.display=name==='mobile'?'':'none';
    document.querySelectorAll('.vp-tab').forEach(function(b){{b.classList.remove('active');}});
    btn.classList.add('active');
  }}
  </script>
  <main>{body}
  </main>
  {chat_panel}
  {_modal_html()}
</body>
</html>"""
def _single_viewport_section(
    report_text: str, annotated_png: bytes, locations: list,
    viewport_label: str = "desktop", api_url: str = None,
) -> str:
    vp = viewport_label
    img_b64 = base64.b64encode(annotated_png).decode()
    score_val = _extract_score(report_text) or 0.0
    legend_rows = ""
    for loc in locations:
        sev = loc.get("severity", "Medium")
        hex_color = SEVERITY_HEX.get(sev, "#d97706")
        n = loc['issue_number']
        legend_rows += f"""
        <tr id="leg-row-{vp}-{n}">
          <td><span class="badge" style="background:{hex_color}">#{n}</span></td>
          <td>{loc.get('short_title', '')}</td>
          <td><span class="sev-pill" style="background:{hex_color}">{sev}</span></td>
          <td><button id="ign-leg-btn-{vp}-{n}"
                onclick="_toggleIgnore('{vp}',{n})"
                style="padding:.15rem .55rem;font-size:.72rem;border:1px solid #cbd5e1;background:white;border-radius:6px;cursor:pointer;color:#64748b;white-space:nowrap;transition:all .15s;">
              Ignore</button></td>
        </tr>"""
    legend_html = ""
    if legend_rows:
        legend_html = f"""
    <div class="legend-card">
      <h2>Issue Legend</h2>
      <table>
        <thead><tr><th>#</th><th>Issue</th><th>Severity</th><th></th></tr></thead>
        <tbody>{legend_rows}</tbody>
      </table>
    </div>"""
    escaped_report = _escape_report(report_text)
    issue_details = _extract_issue_details(report_text)
    modal_issues = json.dumps([
        {
            "issue_number": loc["issue_number"],
            "short_title":  loc.get("short_title", f"Issue {loc['issue_number']}"),
            "severity":     loc.get("severity", "Medium"),
            "bbox_pct":     loc.get("bbox_pct"),
            **issue_details.get(loc["issue_number"], {}),
        }
        for loc in locations
    ])
    modal_data = _html_mod.escape(modal_issues)
    return f"""
    {_score_init_script(vp, score_val, locations)}
    <div class="screenshot-card">
      <h2>Annotated Page Screenshot <small style="font-size:.75rem;color:#94a3b8;font-weight:400;">&nbsp;&mdash; click to explore issues</small></h2>
      <img src="data:image/png;base64,{img_b64}" alt="Annotated screenshot"
           style="cursor:zoom-in;"
           data-issues="{modal_data}"
           onclick="_ssOpen(this.src,JSON.parse(this.dataset.issues),'{vp}')" />
    </div>
    {_score_card_html(score_val, vp)}
    {legend_html}
    <div class="report-card">
      <h2>Full Evaluation</h2>
      <div class="report-body">{escaped_report}</div>
    </div>"""
# ── Step 7a: Single-page HTML report ────────────────────────────
def generate_html(
    url: str,
    desktop_report: str, desktop_png: bytes, desktop_locs: list,
    mobile_report: str,  mobile_png: bytes,  mobile_locs: list,
    api_url: str = None,
) -> str:
    desktop_section = _single_viewport_section(desktop_report, desktop_png, desktop_locs, "desktop", api_url)
    mobile_section  = _single_viewport_section(mobile_report,  mobile_png,  mobile_locs,  "mobile",  api_url)
    return _html_shell(
        title="Heuristic Evaluation Report",
        subtitle=url,
        body=_viewport_tab_html(desktop_section, mobile_section),
        api_url=api_url,
    )
# ── Step 7b: Journey HTML report ────────────────────────────────
def _single_journey_section(
    steps_data: list[dict], report_text: str, locations: list,
    viewport_label: str = "desktop", api_url: str = None,
) -> str:
    vp = viewport_label
    issue_details = _extract_issue_details(report_text)
    score_val = _extract_score(report_text) or 0.0
    step_cards = ""
    for step in steps_data:
        crops = step.get("issue_crops", [])
        step_locs = step.get("locations", [])
        has_issues = any(issue_num != -1 for issue_num, *_ in crops)

        # Full annotated screenshot — always shown, clickable to open modal
        full_img_b64 = base64.b64encode(step["screenshot_bytes"]).decode()
        modal_issues = json.dumps([
            {
                "issue_number": loc["issue_number"],
                "short_title":  loc.get("short_title", f"Issue {loc['issue_number']}"),
                "severity":     loc.get("severity", "Medium"),
                "bbox_pct":     loc.get("bbox_pct"),
                **issue_details.get(loc["issue_number"], {}),
            }
            for loc in step_locs
        ])
        modal_data = _html_mod.escape(modal_issues)
        cursor = "zoom-in" if step_locs else "default"
        click_handler = f'onclick="_ssOpen(this.src,JSON.parse(this.dataset.issues),\'{vp}\')"' if step_locs else ""
        full_ss_html = f"""
        <img src="data:image/png;base64,{full_img_b64}" alt="Step {step['step_num']} screenshot"
             style="cursor:{cursor};width:100%;display:block;"
             data-issues="{modal_data}"
             {click_handler} />"""

        # Issue crops — only shown when there are actual issues (not the "no issues" placeholder)
        crop_imgs = ""
        if has_issues:
            for issue_num, short_title, severity, crop_bytes in crops:
                if issue_num == -1:
                    continue
                img_b64 = base64.b64encode(crop_bytes).decode()
                hex_color = SEVERITY_HEX.get(severity, "#d97706")
                issue_data_attr = _html_mod.escape(json.dumps({
                    "issue_number": issue_num,
                    "short_title":  short_title or f"Issue {issue_num}",
                    "severity":     severity or "Medium",
                    **issue_details.get(issue_num, {}),
                }))
                crop_imgs += f"""
          <div class="crop-block">
            <div class="crop-label" style="background:{hex_color}">
              <span class="crop-badge">#{issue_num}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;">{short_title}</span>
            </div>
            <img src="data:image/png;base64,{img_b64}" alt="Issue #{issue_num}"
                 style="cursor:zoom-in;"
                 data-issue="{issue_data_attr}"
                 onclick="_ssCropClick(this.src,JSON.parse(this.dataset.issue),'{vp}')" />
          </div>"""
        else:
            crop_imgs = '<div style="padding:.4rem .6rem;font-size:.75rem;color:#94a3b8;">No issues found</div>'

        step_cards += f"""
      <div class="step-card">
        <div class="step-header">
          <span class="step-num">Step {step['step_num']}</span>
          <span class="step-label">{step['label']}</span>
          <span class="step-url">{step['url']}</span>
        </div>
        {full_ss_html}
        <div class="crop-list">{crop_imgs}</div>
      </div>"""
    legend_rows = ""
    for loc in locations:
        sev = loc.get("severity", "Medium")
        hex_color = SEVERITY_HEX.get(sev, "#d97706")
        step_num = loc.get("step_num", "?")
        n = loc['issue_number']
        legend_rows += f"""
        <tr id="leg-row-{vp}-{n}">
          <td><span class="badge" style="background:{hex_color}">#{n}</span></td>
          <td>Step {step_num}</td>
          <td>{loc.get('short_title', '')}</td>
          <td><span class="sev-pill" style="background:{hex_color}">{sev}</span></td>
          <td><button id="ign-leg-btn-{vp}-{n}"
                onclick="_toggleIgnore('{vp}',{n})"
                style="padding:.15rem .55rem;font-size:.72rem;border:1px solid #cbd5e1;background:white;border-radius:6px;cursor:pointer;color:#64748b;white-space:nowrap;transition:all .15s;">
              Ignore</button></td>
        </tr>"""
    legend_html = ""
    if legend_rows:
        legend_html = f"""
    <div class="legend-card">
      <h2>Issue Legend</h2>
      <table>
        <thead><tr><th>#</th><th>Step</th><th>Issue</th><th>Severity</th><th></th></tr></thead>
        <tbody>{legend_rows}</tbody>
      </table>
    </div>"""
    escaped_report = _escape_report(report_text)
    return f"""
    {_score_init_script(vp, score_val, locations)}
    <div class="screenshot-card timeline-section">
      <h2>Journey Timeline ({len(steps_data)} steps — scroll horizontally)</h2>
      <div class="timeline">{step_cards}</div>
    </div>
    {_score_card_html(score_val, vp)}
    {legend_html}
    <div class="report-card">
      <h2>Full Journey Evaluation</h2>
      <div class="report-body">{escaped_report}</div>
    </div>"""
def generate_journey_html(
    start_url: str,
    desktop_steps: list[dict], desktop_report: str, desktop_locs: list,
    mobile_steps: list[dict],  mobile_report: str,  mobile_locs: list,
    api_url: str = None,
) -> str:
    desktop_section = _single_journey_section(desktop_steps, desktop_report, desktop_locs, "desktop", api_url)
    mobile_section  = _single_journey_section(mobile_steps,  mobile_report,  mobile_locs,  "mobile",  api_url)
    return _html_shell(
        title="Journey Heuristic Evaluation Report",
        subtitle=f"Journey starting at {start_url} &nbsp;&middot;&nbsp; {len(desktop_steps)} steps",
        extra_css="""
    .timeline { display: flex; gap: 1.5rem; overflow-x: auto; padding-bottom: 1rem; align-items: flex-start; }
    .step-card { flex: 0 0 360px; background: white; border-radius: 12px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.08); overflow: hidden; }
    .step-header { padding: 0.75rem 1rem; background: #0f172a; }
    .step-num { display: inline-block; background: #6366f1; color: white;
      border-radius: 9999px; padding: 0.1rem 0.6rem; font-size: 0.75rem;
      font-weight: 700; margin-right: 0.5rem; }
    .step-label { color: #f8fafc; font-weight: 600; font-size: 0.9rem; }
    .step-url { display: block; color: #94a3b8; font-size: 0.7rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 0.2rem; }
    .crop-list { display: flex; flex-direction: column; gap: 0; }
    .crop-block { border-top: 1px solid #e2e8f0; }
    .crop-block img { width: 100%; display: block; }
    .crop-label { padding: 0.3rem 0.6rem; font-size: 0.72rem; font-weight: 700;
      color: white; display: flex; align-items: center; gap: 0.4rem; }
    .crop-badge { background: rgba(0,0,0,0.25); border-radius: 9999px;
      padding: 0.05rem 0.45rem; font-size: 0.7rem; }
    .no-issues-block { border-top: none; }
    .no-issues-label { background: #64748b; color: #e2e8f0; font-weight: 500; }
    .no-issues-block img { opacity: 0.6; }
    .timeline-section h2 { margin-bottom: 1rem; font-size: 1.1rem; color: #475569; }
    """,
        body=_viewport_tab_html(desktop_section, mobile_section),
        api_url=api_url,
    )

# ── Screenshot helpers ────────────────────────────────────────────
def _to_png(img_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

def call_claude_screenshot(screenshot_bytes: bytes, viewport_label: str = "desktop") -> str:
    print(f"  Sending {viewport_label} screenshot to Claude for heuristic evaluation ...")
    resized = _resize_screenshot(_to_png(screenshot_bytes))
    img_b64 = base64.standard_b64encode(resized).decode()
    client = anthropic.Anthropic(api_key=API_KEY, http_client=httpx.Client(verify=False))
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=HEURISTICS_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"Please evaluate this {viewport_label} page screenshot:"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
        ]}],
    )
    return response.content[0].text

def call_claude_journey_screenshots(step_images: list[bytes], step_offset: int = 0, viewport_label: str = "desktop") -> str:
    """Call Claude for a batch of journey screenshots. step_offset shifts the step numbers."""
    total = len(step_images)
    print(f"  Sending steps {step_offset}–{step_offset+total-1} ({total} screenshots) to Claude ...")
    client = anthropic.Anthropic(api_key=API_KEY, http_client=httpx.Client(verify=False))
    content_blocks: list = [{
        "type": "text",
        "text": (
            f"I am providing steps {step_offset} to {step_offset+total-1} of a user journey "
            f"({viewport_label} viewport) as screenshots, in order. "
            f"Please evaluate this segment against Nielsen's 10 heuristics.\n\n"
        ),
    }]
    for i, img_bytes in enumerate(step_images):
        img = Image.open(io.BytesIO(_to_png(img_bytes))).convert("RGB")
        img.thumbnail((900, 900), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content_blocks.append({"type": "text", "text": f"--- STEP {step_offset + i} ---\nScreenshot:"})
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })
    content_blocks.append({
        "type": "text",
        "text": "Please evaluate this journey segment against Nielsen's 10 heuristics.",
    })
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=JOURNEY_HEURISTICS_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
    ) as stream:
        print("  Receiving evaluation", end="", flush=True)
        chunks = []
        for text in stream.text_stream:
            chunks.append(text)
            print(".", end="", flush=True)
        print(" done.")
        return "".join(chunks)

# ── Internal viewport helpers ─────────────────────────────────────
def _analyze_viewport(url: str, viewport: dict, label: str) -> tuple[str, bytes, list]:
    content, screenshot = playwright_scrape_and_screenshot(url, viewport)
    formatted = format_for_prompt(content)
    full_response = call_claude(formatted, label.lower())
    report_text, locations = parse_response(full_response)
    found = locate_elements(url, locations, viewport)
    annotated_png = annotate_screenshot(screenshot, found)
    return report_text, annotated_png, locations

def _analyze_journey_viewport(
    url: str, steps: list[dict], viewport: dict, label: str
) -> tuple[list, str, list]:
    steps_data = playwright_journey_scrape(url, steps, viewport)
    full_response = call_claude_journey(steps_data, label.lower())
    report_text, locations = parse_response(full_response)
    locs_by_step = defaultdict(list)
    for loc in locations:
        locs_by_step[loc.get("step_num", 0)].append(loc)
    for step in steps_data:
        step_locs = locs_by_step.get(step["step_num"], [])
        found = locate_elements(step["url"], step_locs, viewport) if step_locs else []
        if found:
            step["screenshot_bytes"] = annotate_screenshot(step["screenshot_bytes"], found)
        step["issue_crops"] = crop_to_issue_regions(step["screenshot_bytes"], found)
        step["locations"] = step_locs
    return steps_data, report_text, locations

# ── Public API functions ──────────────────────────────────────────
def analyze_url(url: str, api_url: str = None) -> dict:
    """
    Run a single-page heuristic evaluation (desktop + mobile).
    Returns a dict with keys:
      html, desktop_report, mobile_report, desktop_locs, mobile_locs,
      desktop_score, mobile_score, report_text
    """
    print(f"\n── Desktop analysis ──")
    desktop_report, desktop_png, desktop_locs = _analyze_viewport(url, DESKTOP_VIEWPORT, "Desktop")
    print(f"\n── Mobile analysis ──")
    mobile_report, mobile_png, mobile_locs = _analyze_viewport(url, MOBILE_VIEWPORT, "Mobile")
    html = generate_html(
        url,
        desktop_report, desktop_png, desktop_locs,
        mobile_report,  mobile_png,  mobile_locs,
        api_url=api_url,
    )
    return {
        "html":           html,
        "desktop_report": desktop_report,
        "mobile_report":  mobile_report,
        "desktop_locs":   desktop_locs,
        "mobile_locs":    mobile_locs,
        "desktop_score":  _extract_score(desktop_report),
        "mobile_score":   _extract_score(mobile_report),
        "report_text": (
            f"=== DESKTOP VIEWPORT ===\n{desktop_report}\n\n"
            f"=== MOBILE VIEWPORT ===\n{mobile_report}"
        ),
    }

def analyze_journey(url: str, steps: list[dict], api_url: str = None) -> dict:
    """
    Run a multi-step journey heuristic evaluation (desktop + mobile).
    Returns a dict with keys:
      html, desktop_report, mobile_report, desktop_locs, mobile_locs,
      desktop_score, mobile_score, report_text
    """
    print(f"\n── Desktop journey analysis ──")
    desktop_steps, desktop_report, desktop_locs = _analyze_journey_viewport(
        url, steps, DESKTOP_VIEWPORT, "Desktop"
    )
    print(f"\n── Mobile journey analysis ──")
    mobile_steps, mobile_report, mobile_locs = _analyze_journey_viewport(
        url, steps, MOBILE_VIEWPORT, "Mobile"
    )
    html = generate_journey_html(
        url,
        desktop_steps, desktop_report, desktop_locs,
        mobile_steps,  mobile_report,  mobile_locs,
        api_url=api_url,
    )
    return {
        "html":           html,
        "desktop_report": desktop_report,
        "mobile_report":  mobile_report,
        "desktop_locs":   desktop_locs,
        "mobile_locs":    mobile_locs,
        "desktop_score":  _extract_score(desktop_report),
        "mobile_score":   _extract_score(mobile_report),
        "report_text": (
            f"=== DESKTOP VIEWPORT ===\n{desktop_report}\n\n"
            f"=== MOBILE VIEWPORT ===\n{mobile_report}"
        ),
    }

def analyze_screenshots(
    desktop_bytes: bytes, mobile_bytes: bytes = None, api_url: str = None
) -> dict:
    """
    Run a heuristic evaluation from uploaded screenshot bytes.
    Returns a dict with keys: html, desktop_report, mobile_report, report_text
    """
    print("\n── Desktop screenshot analysis ──")
    desktop_png = _to_png(desktop_bytes)
    desktop_report, desktop_locs = parse_response(call_claude_screenshot(desktop_png, "desktop"))
    desktop_annotated, _ = annotate_screenshot_from_locs(desktop_png, desktop_locs)

    if mobile_bytes:
        print("\n── Mobile screenshot analysis ──")
        mobile_png = _to_png(mobile_bytes)
        mobile_report, mobile_locs = parse_response(call_claude_screenshot(mobile_png, "mobile"))
        mobile_annotated, _ = annotate_screenshot_from_locs(mobile_png, mobile_locs)
    else:
        mobile_report    = "(No mobile screenshot provided.)"
        mobile_annotated = desktop_annotated
        mobile_locs      = []

    html = generate_html(
        "Uploaded Screenshots",
        desktop_report, desktop_annotated, desktop_locs,
        mobile_report,  mobile_annotated,  mobile_locs,
        api_url=api_url,
    )
    return {
        "html":           html,
        "desktop_report": desktop_report,
        "mobile_report":  mobile_report,
        "desktop_locs":   desktop_locs,
        "mobile_locs":    mobile_locs,
        "desktop_score":  _extract_score(desktop_report),
        "mobile_score":   _extract_score(mobile_report),
        "report_text": (
            f"=== DESKTOP VIEWPORT ===\n{desktop_report}\n\n"
            f"=== MOBILE VIEWPORT ===\n{mobile_report}"
        ),
    }

_JOURNEY_BATCH_SIZE = 15

def analyze_journey_screenshots(
    step_images: list[bytes], api_url: str = None
) -> dict:
    """
    Run a journey heuristic evaluation from a list of screenshot bytes (one per step).
    Automatically batches large journeys (>15 steps) into groups for reliable evaluation.
    Returns a dict with keys: html, report_text, locations, score
    """
    all_locations: list = []
    report_parts: list[str] = []
    issue_offset = 0

    for batch_start in range(0, len(step_images), _JOURNEY_BATCH_SIZE):
        batch = step_images[batch_start:batch_start + _JOURNEY_BATCH_SIZE]
        full_response = call_claude_journey_screenshots(batch, step_offset=batch_start)
        report_text, locations = parse_response(full_response)
        # Renumber report text FIRST (before offsetting locations) so numbers stay in sync
        renumbered_report = _renumber_issues_in_report(report_text, issue_offset)
        # Offset issue numbers so they don't collide across batches
        for loc in locations:
            loc["issue_number"] += issue_offset
        if locations:
            issue_offset = max(loc["issue_number"] for loc in locations)
        all_locations.extend(locations)
        report_parts.append(renumbered_report)

    # Merge report texts; prepend overall score line for _extract_score to find
    if len(report_parts) == 1:
        merged_report = report_parts[0]
    else:
        batch_scores = [s for s in (_extract_score(r) for r in report_parts) if s is not None]
        overall = round(sum(batch_scores) / len(batch_scores), 1) if batch_scores else 5.0
        sections = []
        for idx, rpt in enumerate(report_parts):
            b_start = idx * _JOURNEY_BATCH_SIZE
            b_end   = min(b_start + _JOURNEY_BATCH_SIZE - 1, len(step_images) - 1)
            sections.append(f"=== Steps {b_start}–{b_end} ===\n{rpt}")
        merged_report = f"Overall Score: {overall}/10\n\n" + "\n\n".join(sections)

    locs_by_step = defaultdict(list)
    for loc in all_locations:
        locs_by_step[loc.get("step_num", 0)].append(loc)

    steps_data = []
    for i, img_bytes in enumerate(step_images):
        png = _to_png(img_bytes)
        step_locs = locs_by_step.get(i, [])
        annotated, found = annotate_screenshot_from_locs(png, step_locs)
        steps_data.append({
            "step_num":         i,
            "label":            f"Step {i}",
            "url":              f"step_{i}",
            "screenshot_bytes": annotated,
            "issue_crops":      crop_to_issue_regions(annotated, found),
            "locations":        step_locs,
        })

    html = generate_journey_html(
        "Uploaded Screenshots",
        steps_data, merged_report, all_locations,
        steps_data, merged_report, all_locations,
        api_url=api_url,
    )
    return {
        "html":        html,
        "report_text": merged_report,
        "locations":   all_locations,
        "score":       _extract_score(merged_report),
    }
