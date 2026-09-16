"""WCAG contrast + layout audit for the TrafficAgent-DSS dashboard.

Why this exists
---------------
The defects this catches are the kind nobody notices by eye: a label at 2.26:1 still
"looks like text", and a dark empty-state rendered on a translucent well only fails
once you composite the ancestor background stack. Reading the CSS is not enough —
several of the real bugs found here (a token whose dark value was reused on a light
surface, a dark overlay that only misbehaves in light mode) are invisible in the
stylesheet. So this tool renders the page in a real browser and measures.

What it does
------------
1. Launches headless Chromium, forces the theme via localStorage.
2. For every text-bearing leaf element, walks the ancestor chain and *alpha-composites*
   the real background — including linear-gradients, which have a transparent
   `background-color` and would otherwise be silently skipped.
3. Computes the WCAG 2.1 contrast ratio and flags anything below 4.5:1 (3.0:1 for
   large text: >=24px, or >=18.66px at weight >=700).
4. Reports horizontal overflow and clipped text as well.

Usage
-----
    python scripts/ui_contrast_audit.py [PORT] [TAG] [THEME]

    PORT   port the dev server is listening on (default 8600)
    TAG    label for the output json file      (default "run")
    THEME  "dark" or "light"                   (default "dark")

Writes `ui_audit_<TAG>_<THEME>.json` in the working directory and prints one line per
violation plus a summary:  OK theme=... real_contrast=<n> overflow=<n> clipped=<n>

Gotchas learned the hard way
----------------------------
* The theme key is ``traffic_dss_theme``. An earlier version of this script wrote
  ``traffic-theme``, so the "light" run was really re-auditing dark mode and the
  light-theme failures stayed hidden. If the counts look identical across themes,
  check this first.
* A transparent ``backgroundColor`` is NOT a background. Treating it as one produces
  ratio=1.00 false positives.
* Gradient backgrounds must be measured against their own colour stops, not against
  the page background, or a dark button reads as "white on white".
"""

import os
import sys
import json

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8600"
TAG = sys.argv[2] if len(sys.argv) > 2 else "run"
THEME = sys.argv[3] if len(sys.argv) > 3 else "dark"

OUT = os.path.join(os.getcwd(), "ui_audit_%s_%s.json" % (TAG, THEME))

# The application reads/writes this exact key (see dashboard.js: state.theme).
THEME_STORAGE_KEY = "traffic_dss_theme"


JS = r"""
(sampleMode) => {
  function parse(c){
    if(!c) return null;
    if(c==='transparent') return [0,0,0,0];
    let m=c.match(/rgba?\(([^)]+)\)/);
    if(!m) return null;
    const p=m[1].split(',').map(s=>parseFloat(s.trim()));
    return [p[0],p[1],p[2],p.length>3?p[3]:1];
  }
  function over(fg,bg){
    const a=fg[3];
    return [fg[0]*a+bg[0]*(1-a), fg[1]*a+bg[1]*(1-a), fg[2]*a+bg[2]*(1-a), 1];
  }
  function lum(c){
    const [r,g,b]=c.slice(0,3).map(v=>{
      v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);
    });
    return 0.2126*r+0.7152*g+0.0722*b;
  }
  function ratio(a,b){
    const l1=lum(a), l2=lum(b);
    const hi=Math.max(l1,l2), lo=Math.min(l1,l2);
    return (hi+0.05)/(lo+0.05);
  }
  // All colour stops of a gradient, ignoring their positions.
  function gradientStops(bgi){
    if(!bgi || bgi==='none' || bgi.indexOf('gradient')<0) return [];
    const out=[];
    const re=/rgba?\([^)]*\)/g;
    let m;
    while((m=re.exec(bgi))!==null){ const c=parse(m[0]); if(c && c[3]>0) out.push(c); }
    return out;
  }
  // Composite the real background down the ancestor chain.
  // Any ancestor may be a gradient (this project's panels and buttons use them heavily);
  // such a layer has a transparent background-color, so it must be read from
  // backgroundImage. Skipping it drops through to <body> and turns "dark text on a light
  // card" into "dark text on a dark body" — a background colour that does not exist.
  function realBg(el){
    const stack=[];
    let n=el;
    while(n && n!==document.documentElement){
      const cs=getComputedStyle(n);
      const c=parse(cs.backgroundColor);
      if(c && c[3]>0){
        stack.push(c);
      } else {
        const stops=gradientStops(cs.backgroundImage);
        if(stops.length){
          // Lightest stop = worst case for dark text.
          let best=stops[0];
          for(const s of stops){ if(lum(s)>lum(best)) best=s; }
          stack.push(best);
        }
      }
      n=n.parentElement;
    }
    const rootC=parse(getComputedStyle(document.documentElement).backgroundColor);
    if(rootC && rootC[3]>0) stack.push(rootC);
    const bodyC=parse(getComputedStyle(document.body).backgroundColor);
    let base=[255,255,255,1];
    if(bodyC && bodyC[3]>0) base=bodyC;
    let out=base;
    for(let i=stack.length-1;i>=0;i--) out=over(stack[i],out);
    return out;
  }
  const out=[];
  const sel='p,span,td,th,li,h1,h2,h3,h4,label,small,strong,code,b,i,button,a';
  document.querySelectorAll(sel).forEach(el=>{
    // Only leaves that directly contain text.
    const hasText=[...el.childNodes].some(n=>n.nodeType===3 && n.textContent.trim());
    if(!hasText) return;
    const r=el.getBoundingClientRect();
    if(r.width<3||r.height<3) return;
    const cs=getComputedStyle(el);
    if(cs.visibility==='hidden'||cs.display==='none'||parseFloat(cs.opacity)===0) return;
    const fg=parse(cs.color);
    if(!fg || fg[3]===0) return;
    // Gradient background: measure every stop and keep the worst.
    const stops=gradientStops(cs.backgroundImage);
    let bg, cr;
    if(stops.length){
      const parentBg=realBg(el.parentElement||el);
      let worst=Infinity, worstBg=null;
      for(const st of stops){
        const base=over(st,parentBg);
        const v=ratio(over(fg,base),base);
        if(v<worst){worst=v; worstBg=base;}
      }
      cr=worst; bg=worstBg;
    } else {
      bg=realBg(el);
      cr=ratio(over(fg,bg),bg);
    }
    const fs=parseFloat(cs.fontSize);
    const fw=parseInt(cs.fontWeight)||400;
    const isLarge = fs>=24 || (fs>=18.66 && fw>=700);
    const need = isLarge?3.0:4.5;
    if(cr<need){
      out.push({
        text: el.textContent.trim().slice(0,45),
        cls:(el.className||'').toString().slice(0,55),
        tag: el.tagName.toLowerCase(),
        fgColor: cs.color,
        bgSynth: `rgb(${bg.slice(0,3).map(v=>Math.round(v)).join(', ')})`,
        fs, fw,
        ratio: Math.round(cr*100)/100,
        need,
        gap: Math.round((need-cr)*100)/100,
      });
    }
  });
  return out;
}
"""


LAYOUT_JS = """() => {
  const docW=document.documentElement.clientWidth;
  const ov=[];
  document.querySelectorAll('*').forEach(el=>{
    const r=el.getBoundingClientRect();
    if(r.width>0 && r.right>docW+2)
      ov.push({tag:el.tagName.toLowerCase(), cls:(el.className||'').toString().slice(0,50), right:Math.round(r.right)});
  });
  const clipped=[];
  document.querySelectorAll('*').forEach(el=>{
    const cs=getComputedStyle(el);
    if((cs.overflow==='hidden'||cs.overflowX==='hidden') && el.scrollWidth>el.clientWidth+3 && el.clientWidth>0)
      clipped.push({tag:el.tagName.toLowerCase(), cls:(el.className||'').toString().slice(0,50),
        sw:el.scrollWidth, cw:el.clientWidth, text:(el.textContent||'').trim().slice(0,40)});
  });
  return {docW, pageH:document.documentElement.scrollHeight, overflowX:ov, clipped};
}"""


def main():
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    page.add_init_script(
        "try{localStorage.setItem('%s','%s')}catch(e){}" % (THEME_STORAGE_KEY, THEME)
    )
    page.goto("http://127.0.0.1:%s/" % PORT, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(6000)

    # Guard against the silent-failure mode described in the module docstring: if the theme
    # did not actually apply, the numbers below describe the other theme.
    applied = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    if applied != THEME:
        sys.stderr.write(
            "WARNING: requested theme %r but the page rendered %r. "
            "The results below describe the WRONG theme.\n" % (THEME, applied)
        )

    violations = page.evaluate(JS, "all")
    layout = page.evaluate(LAYOUT_JS)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(
            {"theme": THEME, "appliedTheme": applied, "contrast": violations, "layout": layout},
            f,
            ensure_ascii=False,
            indent=2,
        )

    for v in violations:
        sys.stdout.write(
            "  ratio=%5.2f (need %.1f) fs=%-5s fg=%-20s bg=%-18s %s\n"
            % (v["ratio"], v["need"], v["fs"], v["fgColor"], v["bgSynth"], v["cls"][:38] or v["tag"])
        )

    sys.stdout.write(
        "OK theme=%s applied=%s real_contrast=%d overflow=%d clipped=%d\n"
        % (THEME, applied, len(violations), len(layout["overflowX"]), len(layout["clipped"]))
    )
    sys.stdout.flush()
    # Deliberately no browser.close(): see the pytest.ini notes about Chromium teardown
    # blocking process exit in this sandbox.
    os._exit(0)


if __name__ == "__main__":
    main()
