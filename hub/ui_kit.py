# ruff: noqa: E501 -- inline SVG paths, CSS and script source read best unwrapped
"""The customer console's design system: tokens, app shell, components, charts.

WHY A MODULE OF ITS OWN
-----------------------
hub/console.py renders pages; this module decides how they look and behave.
The console used to borrow the operator console's stylesheet (hub/admin.py's
`_CSS`), which suits one employee scanning queues and reads like a document to
a customer deciding whether to renew. Keeping the customer-facing system here
lets it evolve without restyling the operator's tool underneath them.

CONSTRAINTS THAT SHAPED IT
--------------------------
* **No build step, no network.** The Hub image ships no frontend toolchain,
  and the Content-Security-Policy (hub/admin.py `content_security_policy`)
  is `default-src 'none'`: no external stylesheet, font, script or image.
  Everything is inline: one stylesheet, two scripts allowed by their SHA-256,
  and charts drawn as inline SVG on the server.
* **Works without script.** Every page is complete server-rendered HTML.
  Script adds the command palette, keyboard shortcuts, the theme toggle,
  sortable and filterable tables, relative timestamps and chart tooltips --
  never content. Controls that need script start `hidden` and are revealed
  by it, so nothing on screen does nothing.
* **Accessible by construction.** Landmarks, a skip link, visible focus,
  `aria-current` on navigation, every chart paired with a data table, and
  colour never carrying meaning alone (status colours ship with a label).
* **Light and dark are both designed.** Dark follows the OS unless the viewer
  picks one; the choice is kept per browser (localStorage, a convenience --
  the page renders correctly without it).

Chart colours are the dataviz reference palette's first two categorical slots
(blue "with memory", orange "without"), validated for CVD separation and
contrast on this console's own surfaces in both modes; status colours are
reserved for verdicts.
"""
from __future__ import annotations

import html
import math
from datetime import date, datetime, timezone


def h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --- Stylesheet ----------------------------------------------------------------

CSS = """
:root{
  color-scheme:light;
  --bg:#F6F7F9; --surface:#FFFFFF; --surface-2:#F3F5F8; --surface-3:#EBEEF3;
  --ink:#0E1621; --ink-2:#344150; --muted:#5E6B7A; --faint:#66717F;
  --rule:#E3E7ED; --rule-soft:#EEF1F5; --ring:rgba(14,22,33,.08);
  --accent:#3651D4; --accent-hover:#2B43B8; --accent-ink:#FFFFFF;
  --accent-soft:#EBEFFF; --accent-text:#2E46C2;
  --ok:#0E7650; --ok-soft:#E6F5EE; --warn:#A35A00; --warn-soft:#FDF2E3;
  --bad:#C0352B; --bad-soft:#FCEBEA; --info:#2E6BC6; --info-soft:#E8F0FB;
  --series-1:#2a78d6; --series-2:#eb6834; --grid:#E8EBF0; --axis:#C9CFD8;
  --shadow-sm:0 1px 2px rgba(16,24,40,.05);
  --shadow:0 1px 3px rgba(16,24,40,.06),0 1px 2px rgba(16,24,40,.04);
  --shadow-lg:0 12px 32px rgba(16,24,40,.16),0 2px 6px rgba(16,24,40,.08);
  --radius:12px; --radius-sm:8px;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --side-w:252px;
  /* names the shared components (hub/admin.py secret_field, live badge) use */
  --paper:var(--bg); --code:var(--surface-2);
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  color-scheme:dark;
  --bg:#0A0E13; --surface:#121821; --surface-2:#171E28; --surface-3:#1E2632;
  --ink:#E7ECF2; --ink-2:#C3CCD7; --muted:#93A0B0; --faint:#8592A3;
  --rule:#242D39; --rule-soft:#1B232D; --ring:rgba(255,255,255,.08);
  --accent:#8199FF; --accent-hover:#9AAEFF; --accent-ink:#0A0E13;
  --accent-soft:#1D2547; --accent-text:#A9B8FF;
  --ok:#40C98F; --ok-soft:#10291F; --warn:#F0A64A; --warn-soft:#2E2111;
  --bad:#F2766D; --bad-soft:#341716; --info:#6FA4F0; --info-soft:#132238;
  --series-1:#3987e5; --series-2:#d95926; --grid:#1F2731; --axis:#334050;
  --shadow-sm:0 1px 2px rgba(0,0,0,.3); --shadow:0 1px 3px rgba(0,0,0,.35);
  --shadow-lg:0 16px 40px rgba(0,0,0,.55);
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --bg:#0A0E13; --surface:#121821; --surface-2:#171E28; --surface-3:#1E2632;
  --ink:#E7ECF2; --ink-2:#C3CCD7; --muted:#93A0B0; --faint:#8592A3;
  --rule:#242D39; --rule-soft:#1B232D; --ring:rgba(255,255,255,.08);
  --accent:#8199FF; --accent-hover:#9AAEFF; --accent-ink:#0A0E13;
  --accent-soft:#1D2547; --accent-text:#A9B8FF;
  --ok:#40C98F; --ok-soft:#10291F; --warn:#F0A64A; --warn-soft:#2E2111;
  --bad:#F2766D; --bad-soft:#341716; --info:#6FA4F0; --info-soft:#132238;
  --series-1:#3987e5; --series-2:#d95926; --grid:#1F2731; --axis:#334050;
  --shadow-sm:0 1px 2px rgba(0,0,0,.3); --shadow:0 1px 3px rgba(0,0,0,.35);
  --shadow-lg:0 16px 40px rgba(0,0,0,.55);
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.55 var(--sans);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent-text);text-decoration:none}
a:hover{text-decoration:underline}
code,.m{font-family:var(--mono);font-size:.86em;background:var(--surface-2);
  padding:.08rem .32rem;border-radius:5px;border:1px solid var(--rule-soft)}
p{margin:.35rem 0}
ul{margin:.4rem 0;padding-left:1.2rem}
li{margin:.25rem 0}
h1{font-size:1.55rem;line-height:1.25;margin:0;letter-spacing:-.015em;font-weight:680}
h2{font-size:1.02rem;margin:0 0 .65rem;font-weight:650;letter-spacing:-.005em}
h3{font-size:.95rem;margin:0 0 .4rem;font-weight:650}
.sub{color:var(--muted);font-size:.93rem;margin:.35rem 0 0;max-width:80ch}
.muted{color:var(--muted)}
/* A link inside running text must not rely on colour alone (WCAG 1.4.1). */
p a,.muted a,.steps a,.flash a,.empty-state a{text-decoration:underline;text-underline-offset:2px}
.faint{color:var(--faint)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip{position:absolute;left:-9999px;top:0}
.skip:focus{left:1rem;top:.75rem;z-index:100;background:var(--surface);color:var(--ink);
  padding:.5rem .8rem;border:1px solid var(--rule);border-radius:8px;box-shadow:var(--shadow)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}

/* --- App shell -------------------------------------------------------------- */
.shell{display:grid;grid-template-columns:var(--side-w) minmax(0,1fr);min-height:100vh;
  background:linear-gradient(90deg,var(--surface) calc(var(--side-w) - 1px),var(--rule) calc(var(--side-w) - 1px),
  var(--rule) var(--side-w),var(--bg) var(--side-w))}
.side{position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;
  gap:.9rem;padding:1rem .8rem;background:var(--surface);border-right:1px solid var(--rule)}
.brand{display:flex;align-items:center;gap:.55rem;padding:.25rem .45rem;font-weight:700;
  letter-spacing:-.01em;color:var(--ink);text-decoration:none;font-size:1rem}
.brand:hover{text-decoration:none}
.brand .mark{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),#7A5CF0);color:#fff;flex:none}
.orgcard{display:flex;gap:.6rem;align-items:center;padding:.55rem .6rem;border:1px solid var(--rule);
  border-radius:10px;background:var(--surface-2)}
.avatar{width:32px;height:32px;border-radius:8px;display:grid;place-items:center;flex:none;
  font-size:.78rem;font-weight:700;color:var(--accent-text);background:var(--accent-soft)}
.orgcard .who{min-width:0;display:flex;flex-direction:column;line-height:1.25}
.orgcard b{font-size:.86rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.orgcard span{font-size:.74rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.side nav{display:flex;flex-direction:column;gap:.1rem}
.side .grp{font-size:.68rem;font-weight:650;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);padding:.8rem .6rem .3rem}
.side nav a{display:flex;align-items:center;gap:.6rem;padding:.42rem .6rem;border-radius:8px;
  color:var(--ink-2);font-size:.9rem;font-weight:520;text-decoration:none}
.side nav a svg{width:17px;height:17px;flex:none;color:var(--faint)}
.side nav a:hover{background:var(--surface-2);color:var(--ink)}
.side nav a[aria-current=page]{background:var(--accent-soft);color:var(--accent-text);font-weight:620}
.side nav a[aria-current=page] svg{color:var(--accent-text)}
.side nav a kbd{margin-left:auto;opacity:0;transition:opacity .15s}
.side nav a:hover kbd{opacity:1}
.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:.5rem;padding-top:.6rem;
  border-top:1px solid var(--rule)}
.side-foot form{margin:0}
.side-foot .plan{display:flex;justify-content:space-between;align-items:center;font-size:.8rem;
  color:var(--muted);padding:0 .4rem}
.col{display:flex;flex-direction:column;min-width:0}
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:.75rem;
  padding:.65rem 1.75rem;min-height:56px;background:color-mix(in srgb,var(--bg) 82%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);
  border-bottom:1px solid var(--rule)}
.crumbs{display:flex;align-items:center;gap:.45rem;font-size:.88rem;color:var(--muted);min-width:0}
.crumbs b{color:var(--ink);font-weight:620;white-space:nowrap}
.crumbs .sep{color:var(--faint)}
.top-actions{margin-left:auto;display:flex;align-items:center;gap:.5rem}
.search-btn{display:flex;align-items:center;gap:.5rem;min-width:15rem;justify-content:flex-start;
  color:var(--muted);font-weight:450}
.search-btn kbd{margin-left:auto}
.search-btn svg{width:16px;height:16px;flex:none;color:var(--faint)}
[data-theme-icon]{display:inline-grid}
kbd{font-family:var(--mono);font-size:.7rem;line-height:1;padding:.18rem .35rem;border-radius:5px;
  border:1px solid var(--rule);border-bottom-width:2px;background:var(--surface);color:var(--muted)}
.icon-btn{display:inline-grid;place-items:center;width:34px;height:34px;padding:0}
.icon-btn svg{width:17px;height:17px}
main{width:100%;max-width:1240px;margin:0 auto;padding:1.75rem 1.75rem 4rem;
  display:flex;flex-direction:column;gap:1.5rem}
main > h1:first-child{margin-top:.15rem}
main > h1 + .sub{margin-top:-1.1rem}
main > h2{margin:.6rem 0 -.6rem;padding:0;border:0}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem 1.5rem;flex-wrap:wrap}
.page-head > div:first-child{min-width:0;flex:1 1 28rem}
.page-head .actions{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.page-head .actions form{margin:0}
a.btn{display:inline-flex;align-items:center;gap:.45rem}
a.btn svg,button svg{width:16px;height:16px;flex:none}
button:has(svg){display:inline-flex;align-items:center;gap:.45rem}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin:.5rem 0 -.6rem}
.section-head h2{margin:0}
.section-head .sub{margin:0}
.grid-2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:1rem;align-items:start}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:.75rem 1rem;align-items:end}
.form-grid label{display:block;font-size:.78rem;font-weight:620;color:var(--ink-2);margin-bottom:.3rem}
.form-grid input,.form-grid select{width:100%}
.form-grid .full{grid-column:1 / -1}
.form-actions{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.35rem 1rem;font-size:.88rem;margin:0}
.kv dt{color:var(--muted)} .kv dd{margin:0}
details.more{background:var(--surface);border:1px solid var(--rule);border-radius:var(--radius);padding:.2rem 1rem}
details.more summary{cursor:pointer;padding:.7rem 0;font-weight:600;font-size:.9rem}
details.more[open] summary{border-bottom:1px solid var(--rule-soft);margin-bottom:.4rem}
details.more ul{padding-left:1.1rem;color:var(--ink-2);font-size:.88rem}
section,.panel{display:flex;flex-direction:column}

/* --- Live / status badges --------------------------------------------------- */
.ro,.badge{display:inline-flex;align-items:center;gap:.35rem;font-size:.72rem;font-weight:600;
  letter-spacing:.02em;padding:.18rem .55rem;border-radius:999px;border:1px solid var(--rule);
  background:var(--surface);color:var(--muted);text-transform:none;font-family:var(--sans)}
.ro::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.live-toggle{font:inherit;font-size:.74rem;padding:.2rem .55rem;border-radius:999px;
  border:1px solid var(--rule);background:var(--surface);color:var(--muted);cursor:pointer}
.live-toggle[aria-pressed=true]{color:var(--warn);border-color:currentColor}

/* --- Buttons & forms -------------------------------------------------------- */
button,.btn{font:inherit;font-size:.86rem;font-weight:600;line-height:1.2;padding:.5rem .9rem;
  border-radius:var(--radius-sm);border:1px solid transparent;cursor:pointer;
  background:var(--accent);color:var(--accent-ink);box-shadow:var(--shadow-sm);
  transition:background .12s,border-color .12s,color .12s,box-shadow .12s;white-space:nowrap}
button:hover,.btn:hover{background:var(--accent-hover);text-decoration:none}
button:active{transform:translateY(.5px)}
button.secondary,.btn.secondary,button[type=button],.search-btn,.icon-btn,
td button,form.vote button.v,.copy-row .btn{
  background:var(--surface);color:var(--ink);border-color:var(--rule)}
button.secondary:hover,button[type=button]:hover,.search-btn:hover,.icon-btn:hover,td button:hover,
.copy-row .btn:hover{background:var(--surface-2);border-color:var(--axis)}
form[data-confirm] button{background:var(--surface);color:var(--ink);border-color:var(--rule)}
form[data-confirm] button:hover{background:var(--surface-2);border-color:var(--axis)}
button.danger,form[data-confirm] button.danger{background:var(--surface);color:var(--bad);
  border-color:color-mix(in srgb,var(--bad) 40%,var(--rule))}
button.danger:hover,form[data-confirm] button.danger:hover{background:var(--bad-soft);border-color:var(--bad)}
td button,td .btn{font-size:.78rem;padding:.3rem .6rem;box-shadow:none}
button.busy{opacity:.6;cursor:progress}
button.linkish{background:none;border:0;box-shadow:none;color:var(--muted);padding:.42rem .6rem;
  font-weight:520;display:flex;align-items:center;gap:.6rem;width:100%;border-radius:8px}
button.linkish:hover{background:var(--surface-2);color:var(--ink)}
button.linkish svg{width:17px;height:17px;color:var(--faint)}
input,select,textarea{font:inherit;font-size:.9rem;color:var(--ink);background:var(--surface);
  border:1px solid var(--rule);border-radius:var(--radius-sm);padding:.48rem .65rem;
  box-shadow:var(--shadow-sm);transition:border-color .12s,box-shadow .12s;max-width:100%}
input:hover,select:hover,textarea:hover{border-color:var(--axis)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
input[type=checkbox],input[type=radio]{width:16px;height:16px;padding:0;box-shadow:none;
  accent-color:var(--accent);vertical-align:-3px}
input::placeholder,textarea::placeholder{color:var(--faint)}
label{font-size:.86rem;color:var(--ink-2)}
fieldset{border:0;padding:0;margin:.3rem 0 .6rem;display:flex;flex-wrap:wrap;gap:.4rem .9rem}
fieldset legend{font-size:.74rem;font-weight:650;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);padding:0;margin:0 0 .35rem;width:100%}
fieldset label{display:inline-flex;align-items:center;gap:.4rem;padding:.3rem .6rem;
  border:1px solid var(--rule);border-radius:999px;background:var(--surface);cursor:pointer}
fieldset label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);color:var(--accent-text)}
main form:not(.act):not(.stack):not(.vote):not(.inline):not(.share-box){display:flex;flex-wrap:wrap;
  align-items:center;gap:.55rem}
form.act{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:0}
form.act input[type=text]{min-width:16rem}
form.stack{display:flex;flex-direction:column;gap:.9rem;margin:0;max-width:44rem}
form.stack label{font-size:.78rem;font-weight:620;letter-spacing:.02em;color:var(--ink-2);
  display:block;margin-bottom:.3rem;text-transform:none}
form.stack input[type=text],form.stack textarea{width:100%}
form.stack textarea{min-height:6rem;resize:vertical}
form.stack button{align-self:flex-start}
form.vote{display:flex;gap:.3rem;align-items:center;margin:0}
form.vote button.v{font-size:.8rem;line-height:1;padding:.32rem .5rem;box-shadow:none;color:var(--muted)}
form.vote button.v.voted{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
form.vote select{font-size:.75rem;padding:.25rem .4rem;max-width:9.5rem}
td form{display:inline-flex;gap:.35rem;align-items:center;margin:0 .2rem .2rem 0;vertical-align:middle}
td select{font-size:.8rem;padding:.28rem .45rem}

/* --- Panels, alerts, verdicts ----------------------------------------------- */
.panel,.share-box,.note,.verdict,.empty-state,.card{background:var(--surface);
  border:1px solid var(--rule);border-radius:var(--radius);box-shadow:var(--shadow-sm)}
.panel{padding:1.15rem 1.25rem;gap:.6rem}
.panel > h2{margin:0}
.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.panel-head p{margin:.15rem 0 0}
.share-box{padding:1rem 1.15rem;display:flex;flex-direction:column;gap:.45rem;align-items:flex-start}
.share-box > b,.share-box > span{display:block}
.share-box br{display:none}
.share-box form{margin:.2rem 0 0}
.note{padding:.9rem 1.1rem;font-size:.9rem;color:var(--ink-2);border-left:3px solid var(--accent)}
.err,.flash{display:flex;gap:.55rem;align-items:flex-start;padding:.7rem .9rem;border-radius:var(--radius-sm);
  font-size:.9rem;margin:0}
.err{background:var(--bad-soft);color:var(--bad);border:1px solid color-mix(in srgb,var(--bad) 35%,transparent)}
.flash{background:var(--ok-soft);color:var(--ok);border:1px solid color-mix(in srgb,var(--ok) 35%,transparent)}
.verdict{padding:1.15rem 1.3rem 1.15rem 1.45rem;position:relative;overflow:hidden;margin:0}
.verdict::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--axis)}
.verdict.good::before{background:var(--ok)} .verdict.warn::before{background:var(--warn)}
.verdict.bad::before{background:var(--bad)}
.verdict.good{background:linear-gradient(90deg,var(--ok-soft),var(--surface) 55%)}
.verdict.warn{background:linear-gradient(90deg,var(--warn-soft),var(--surface) 55%)}
.verdict.bad{background:linear-gradient(90deg,var(--bad-soft),var(--surface) 55%)}
.verdict h2{margin:0 0 .4rem;font-size:1.02rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.verdict p{margin:.3rem 0;font-size:.92rem;max-width:84ch}
.verdict .copy-row{margin-top:.4rem}
.check{display:flex;gap:.65rem;align-items:flex-start;padding:.5rem 0;font-size:.9rem;
  border-top:1px solid var(--rule-soft)}
.check:first-of-type{border-top:0}
.pill{display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:620;
  letter-spacing:.01em;padding:.14rem .5rem;border-radius:999px;white-space:nowrap;
  background:var(--surface-3);color:var(--ink-2);border:1px solid transparent;font-family:var(--sans)}
.pill.ok{background:var(--ok-soft);color:var(--ok)}
.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.bad{background:var(--bad-soft);color:var(--bad)}
.pill.info{background:var(--info-soft);color:var(--info)}
.pill.mute{background:transparent;border-color:var(--rule);color:var(--muted)}
.pill.ok::before,.pill.warn::before,.pill.bad::before{content:"";width:6px;height:6px;border-radius:50%;
  background:currentColor}
.concerns{margin-top:.35rem;display:flex;flex-wrap:wrap;gap:.3rem}
.rev{font-family:var(--mono);font-size:.78rem;color:var(--muted)}
.empty-state{display:flex;flex-direction:column;align-items:center;text-align:center;gap:.45rem;
  padding:2.2rem 1.5rem;color:var(--muted)}
.empty-state svg{width:30px;height:30px;color:var(--faint)}
.empty-state b{color:var(--ink);font-size:.98rem}
.empty-state p{max-width:52ch;margin:0}

/* --- KPI tiles -------------------------------------------------------------- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.85rem}
.tile{background:var(--surface);border:1px solid var(--rule);border-radius:var(--radius);
  padding:1rem 1.1rem;box-shadow:var(--shadow-sm);display:flex;flex-direction:column;gap:.35rem;min-width:0}
.tile .k{font-size:.78rem;font-weight:560;color:var(--muted);letter-spacing:.005em}
.tile .v{font-size:1.6rem;font-weight:680;letter-spacing:-.02em;line-height:1.15;color:var(--ink)}
.tile .v .muted{font-size:.95rem;font-weight:500;letter-spacing:0}
.tile .v.warn{color:var(--warn)} .tile .v.bad{color:var(--bad)}
.tile .foot{font-size:.78rem;color:var(--muted)}
.meter{height:6px;border-radius:999px;background:var(--surface-3);overflow:hidden;margin-top:.15rem}
.meter > span{display:block;height:100%;border-radius:999px;background:var(--accent);min-width:2px}
.meter.warn > span{background:var(--warn)} .meter.bad > span{background:var(--bad)}

/* --- Tables ----------------------------------------------------------------- */
.scroll{position:relative;overflow-x:auto;background:var(--surface);border:1px solid var(--rule);border-radius:var(--radius);
  box-shadow:var(--shadow-sm)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:.88rem}
th{position:sticky;top:0;text-align:left;font-size:.72rem;font-weight:650;letter-spacing:.04em;
  text-transform:uppercase;color:var(--muted);background:var(--surface-2);padding:.6rem .9rem;
  border-bottom:1px solid var(--rule);white-space:nowrap}
th[data-sort]{cursor:pointer;user-select:none}
th[data-sort]:hover{color:var(--ink)}
th[aria-sort=ascending]::after{content:" \\2191"} th[aria-sort=descending]::after{content:" \\2193"}
td{padding:.65rem .9rem;border-bottom:1px solid var(--rule-soft);vertical-align:top}
td.n,.num{font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td{background:color-mix(in srgb,var(--surface-2) 70%,transparent)}
tbody tr:last-child td{border-bottom:0}
tr[hidden]{display:none}
.table-tools{display:flex;gap:.6rem;align-items:center;justify-content:space-between;flex-wrap:wrap;
  padding:.65rem .75rem;border-bottom:1px solid var(--rule);background:var(--surface)}
.table-tools input{min-width:14rem;padding:.38rem .6rem;font-size:.85rem}
.table-tools .count{font-size:.78rem;color:var(--muted)}
time{white-space:nowrap}
.pager{display:flex;gap:.5rem;align-items:center;font-size:.86rem;color:var(--muted)}
.pager a{padding:.35rem .7rem;border:1px solid var(--rule);border-radius:8px;background:var(--surface);
  color:var(--ink);text-decoration:none}
.pager a:hover{background:var(--surface-2)}

/* --- Command blocks & copy rows (shared components) ------------------------- */
.cmds{display:flex;flex-direction:column;gap:.5rem}
.cmd{display:grid;grid-template-columns:minmax(11rem,auto) 1fr;gap:.3rem 1rem;align-items:baseline}
.cmd .what{color:var(--muted);font-size:.88rem}
.cmd code{display:block;overflow-x:auto;white-space:pre;padding:.4rem .6rem}
.copy-row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:.5rem;width:100%}
.copy-row input{flex:1 1 18rem;min-width:0;margin:0;font-family:var(--mono);font-size:.82rem}
.copy-status{flex-basis:100%;font-size:.8rem;color:var(--muted);min-height:1em}
.copy-status:empty{display:none}

/* --- Charts ----------------------------------------------------------------- */
.chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:1rem}
.chart{display:flex;flex-direction:column;gap:.55rem;min-width:0}
.chart .plot{overflow-x:auto;overflow-y:hidden}
.chart svg{width:100%;min-width:560px;height:auto;display:block;overflow:visible}
.chart .legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.8rem;color:var(--ink-2)}
.chart .legend span{display:inline-flex;align-items:center;gap:.4rem}
.chart .legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
.chart .s1{background:var(--series-1)} .chart .s2{background:var(--series-2)}
.chart details summary{font-size:.8rem;color:var(--muted);cursor:pointer;width:max-content}
.chart details[open] summary{margin-bottom:.4rem}
.chart details .scroll{box-shadow:none}
.viz-grid line{stroke:var(--grid);stroke-width:1}
.viz-axis{fill:var(--muted);font-size:11px;font-family:var(--sans)}
.viz-base{stroke:var(--axis);stroke-width:1}
.viz-bar{fill:var(--series-1)}
.viz-l1{stroke:var(--series-1)} .viz-l2{stroke:var(--series-2)}
.viz-d1{fill:var(--series-1)} .viz-d2{fill:var(--series-2)}
.viz-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.viz-dot{stroke:var(--surface);stroke-width:2}
.viz-col .hair{stroke:var(--axis);stroke-width:1;opacity:0}
.viz-col:hover .hair,.viz-col:focus .hair{opacity:1}
.viz-col rect.hit{fill:transparent}
.viz-col:focus{outline:none}
.viz-col:focus rect.hit{fill:color-mix(in srgb,var(--accent) 8%,transparent)}
.viz-bar-g:hover .viz-bar,.viz-bar-g:focus .viz-bar{fill:var(--accent-hover)}
.viz-label{font-size:11.5px;font-weight:600;font-family:var(--sans);fill:var(--ink-2)}
.tip{position:fixed;z-index:60;pointer-events:none;background:var(--ink);color:var(--bg);
  font-size:.78rem;line-height:1.35;padding:.45rem .6rem;border-radius:8px;box-shadow:var(--shadow-lg);
  max-width:20rem;white-space:pre-line}
.tip[hidden]{display:none}

/* Forest plot: one row per memory, its estimate and 95% interval on a shared axis. */
.forest{display:grid;grid-template-columns:minmax(10rem,1.3fr) minmax(12rem,2fr) auto;
  align-items:center;gap:0 1rem;font-size:.86rem}
.forest .fh{font-size:.72rem;font-weight:650;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
  padding:.55rem 0;border-bottom:1px solid var(--rule)}
.forest .fl,.forest .ft,.forest .fv{padding:.55rem 0;border-bottom:1px solid var(--rule-soft);min-width:0}
.forest .fr{display:contents}
.forest .fl{line-height:1.3}
.forest .fv{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.forest svg{width:100%;height:22px;display:block;overflow:visible}
.fz{stroke:var(--axis);stroke-width:1;stroke-dasharray:3 3}
.fci{stroke-width:2;stroke-linecap:round;stroke:var(--faint)}
.fring{stroke:var(--surface);stroke-width:12;stroke-linecap:round}
.fpt{stroke:var(--faint);stroke-width:8;stroke-linecap:round}
.f-ok .fci,.f-ok .fpt{stroke:var(--ok)}
.f-bad .fci,.f-bad .fpt{stroke:var(--bad)}
.forest-axis{display:flex;justify-content:space-between;font-size:.72rem;color:var(--muted)}

/* Hero numbers */
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;align-items:end}
.hero .big{font-size:2.1rem;font-weight:720;letter-spacing:-.025em;line-height:1.05}
.hero .lbl{font-size:.8rem;color:var(--muted);margin-top:.2rem}
.hero .s1t{color:var(--series-1)} .hero .s2t{color:var(--series-2)}

/* Onboarding checklist */
.steps{list-style:none;padding:0;margin:.2rem 0 0;display:flex;flex-direction:column;gap:.15rem}
.steps li{display:flex;gap:.7rem;align-items:flex-start;padding:.5rem 0;border-top:1px solid var(--rule-soft)}
.steps li:first-child{border-top:0}
.steps .tick{width:20px;height:20px;border-radius:50%;flex:none;display:grid;place-items:center;
  border:1.5px solid var(--axis);color:transparent;font-size:.7rem;margin-top:.05rem}
.steps li.done .tick{background:var(--ok);border-color:var(--ok);color:#fff}
.steps li.done .what,.steps li.done .what b{color:var(--muted)}
.steps .what b{display:block;font-weight:600}
.progress-label{font-size:.8rem;color:var(--muted)}

/* --- Command palette -------------------------------------------------------- */
dialog.palette{border:1px solid var(--rule);border-radius:14px;padding:0;width:min(640px,calc(100vw - 2rem));
  background:var(--surface);color:var(--ink);box-shadow:var(--shadow-lg);margin:12vh auto auto}
dialog.palette::backdrop{background:rgba(10,14,19,.45);backdrop-filter:blur(2px)}
.palette .pin{display:flex;align-items:center;gap:.6rem;padding:.8rem 1rem;border-bottom:1px solid var(--rule)}
.palette .pin svg{width:18px;height:18px;color:var(--faint);flex:none}
.palette input{border:0;box-shadow:none;padding:.2rem 0;font-size:1rem;width:100%;background:transparent}
.palette input:focus{box-shadow:none}
.palette ul{list-style:none;margin:0;padding:.4rem;max-height:min(52vh,420px);overflow-y:auto}
.palette li{margin:0}
.palette li .grp{font-size:.68rem;font-weight:650;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint);padding:.55rem .6rem .25rem}
.palette [role=option]{display:flex;align-items:center;gap:.6rem;padding:.55rem .65rem;border-radius:8px;
  cursor:pointer;font-size:.9rem;color:var(--ink-2)}
.palette [role=option] svg{width:16px;height:16px;color:var(--faint);flex:none}
.palette [role=option] kbd{margin-left:auto}
.palette [role=option][aria-selected=true]{background:var(--accent-soft);color:var(--accent-text)}
.palette .foot{display:flex;gap:1rem;padding:.55rem 1rem;border-top:1px solid var(--rule);
  font-size:.74rem;color:var(--muted)}
.palette .none{padding:1.2rem;text-align:center;color:var(--muted);font-size:.88rem}

/* --- Sign-in / shared report ------------------------------------------------ */
main.auth{max-width:none;margin:0;gap:0}
.auth{min-height:100vh;display:grid;place-items:center;padding:2rem 1rem;
  background:radial-gradient(1200px 600px at 10% -10%,var(--accent-soft),transparent 60%),var(--bg)}
.auth .box{width:min(440px,100%);background:var(--surface);border:1px solid var(--rule);border-radius:16px;
  box-shadow:var(--shadow-lg);padding:2rem 2rem 1.6rem;display:flex;flex-direction:column;gap:1rem}
.auth .brand{padding:0;font-size:1.05rem}
.auth form{display:flex;flex-direction:column;align-items:stretch;gap:.6rem;margin:0}
.auth label{font-weight:600;font-size:.82rem}
.auth input{width:100%;font-family:var(--mono);font-size:.92rem;padding:.65rem .75rem}
.auth button{width:100%;padding:.65rem 1rem;font-size:.92rem}
.auth .fine{font-size:.8rem;color:var(--muted);margin:0}
.auth .trust{display:flex;flex-direction:column;gap:.35rem;font-size:.8rem;color:var(--muted);
  border-top:1px solid var(--rule);padding-top:.9rem}
.auth .trust span{display:flex;gap:.45rem;align-items:center}
.auth .trust svg{width:15px;height:15px;color:var(--ok);flex:none}
.signin{display:contents}
.shared-top{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;padding:.8rem 1.75rem;
  border-bottom:1px solid var(--rule);background:var(--surface)}
.shared-banner{display:flex;gap:.6rem;align-items:center;font-size:.85rem;color:var(--ink-2);
  background:var(--info-soft);border:1px solid color-mix(in srgb,var(--info) 30%,transparent);
  border-radius:var(--radius);padding:.7rem 1rem}

/* --- Responsive ------------------------------------------------------------- */
@media (max-width:980px){
  .shell{grid-template-columns:minmax(0,1fr);background:var(--bg)}
  .side > *{min-width:0}
  .side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:.5rem;
    padding:.6rem .9rem;border-right:0;border-bottom:1px solid var(--rule);overflow:visible}
  .orgcard{display:none}
  .side nav{order:3;width:100%;flex-direction:row;overflow-x:auto;gap:.25rem;padding-bottom:.15rem;
    scrollbar-width:none}
  .side nav::-webkit-scrollbar{display:none}
  .side .grp{display:none}
  .side nav a{white-space:nowrap;padding:.38rem .6rem;font-size:.85rem}
  .side nav a kbd{display:none}
  .side-foot{margin:0 0 0 auto;border:0;padding:0;flex-direction:row;align-items:center}
  .side-foot .plan{display:none}
  .top{padding:.55rem 1rem;position:static}
  .search-btn{min-width:0}
  .search-btn span,.search-btn kbd{display:none}
  main{padding:1.1rem 1rem 3rem;gap:1.2rem}
  .forest{grid-template-columns:minmax(0,1fr) auto}
  .forest .ft{grid-column:1 / -1;padding-top:0}
  .forest .fh.ft-h{display:none}
}
@media (max-width:640px){
  .cmd{grid-template-columns:1fr}
  form.act input[type=text]{min-width:0;flex:1 1 9rem}
  .tile .v{font-size:1.35rem}
  h1{font-size:1.3rem}
  .table-tools input{min-width:0;flex:1}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
@media print{
  .side,.top,.no-print,.share-box,form,.table-tools,.tip,dialog{display:none!important}
  .shell{display:block} body{background:#fff;color:#000} main{max-width:none;padding:0}
  .panel,.scroll,.tile,.verdict{box-shadow:none;break-inside:avoid}
  a{color:inherit}
}
@media (forced-colors:active){.meter > span,.viz-bar{forced-color-adjust:none}}
"""


# --- Icons -------------------------------------------------------------------
# Drawn for this console (24px grid, 1.8 stroke, currentColor), so they follow
# text colour in both themes and need no licence notice.

_ICON_PATHS = {
    "overview": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/>'
                '<rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>',
    "proof": '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.3 2.4 2.4 4.7-5.2"/>',
    "memory": '<ellipse cx="12" cy="6" rx="7" ry="2.8"/><path d="M5 6v6c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8V6"/>'
              '<path d="M5 12v6c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8v-6"/>',
    "kb": '<path d="M5 5.2A2.2 2.2 0 0 1 7.2 3H19v14.5H7.2A2.2 2.2 0 0 0 5 19.7z"/><path d="M5 19.7A2.2 2.2 0 0 0 7.2 22H19"/>'
          '<path d="M9 7.5h6"/>',
    "users": '<circle cx="9" cy="8.2" r="3.4"/><path d="M2.8 20c.7-3.4 3.2-5.4 6.2-5.4s5.5 2 6.2 5.4"/>'
             '<path d="M15.6 4.9a3.4 3.4 0 0 1 0 6.6"/><path d="M18 14.9c1.7.8 2.8 2.5 3.2 5.1"/>',
    "keys": '<circle cx="7.8" cy="15.2" r="4.2"/><path d="m10.8 12.2 8.7-8.7"/><path d="m16.2 6.8 2.8 2.8"/><path d="m18.6 4.4 1.9 1.9"/>',
    "alerts": '<path d="M6 16.5v-5.3a6 6 0 0 1 12 0v5.3l1.6 2.1H4.4z"/><path d="M10 21a2.1 2.1 0 0 0 4 0"/>',
    "webhooks": '<path d="M9.6 8.1a3.6 3.6 0 1 1 4.6 3.4L11 17"/><path d="M14.6 16.4H19a3.6 3.6 0 1 1-3.4 4.6"/>'
                '<path d="M7.1 11.9 4.4 16.6a3.6 3.6 0 1 0 5.4 3.1H15"/>',
    "audit": '<rect x="5" y="4" width="14" height="17" rx="2.2"/><path d="M9 3h6v2.5H9z"/><path d="M9 10.5h6"/>'
             '<path d="M9 14h6"/><path d="M9 17.5h3.5"/>',
    "search": '<circle cx="11" cy="11" r="6.8"/><path d="m20 20-4-4"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2'
           'M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    "moon": '<path d="M20 14.6A8.2 8.2 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6z"/>',
    "system": '<rect x="3" y="4" width="18" height="12.5" rx="2.2"/><path d="M8.5 20.5h7M12 16.5v4"/>',
    "logout": '<path d="M14.5 4H18a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3.5"/><path d="m10 16-4-4 4-4"/><path d="M6 12h10"/>',
    "print": '<path d="M7 9V3.5h10V9"/><path d="M7 17.5H5a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4.5a2 2 0 0 1-2 2h-2"/>'
             '<rect x="7" y="14" width="10" height="7" rx="1.2"/>',
    "download": '<path d="M12 3.5v11.5"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4 20.5h16"/>',
    "shield": '<path d="M12 3 4.8 6v5.6c0 4.4 3 7.8 7.2 9.4 4.2-1.6 7.2-5 7.2-9.4V6z"/><path d="m9 12 2.1 2.1L15.2 10"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2.2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
    "inbox": '<path d="M3.5 13.5h5l1.5 3h4l1.5-3h5"/><path d="M6 5h12l2.5 8.5v5.3a1.7 1.7 0 0 1-1.7 1.7H5.2a1.7 1.7 0 0 1-1.7-1.7v-5.3z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "share": '<circle cx="17.5" cy="5.5" r="2.5"/><circle cx="6.5" cy="12" r="2.5"/><circle cx="17.5" cy="18.5" r="2.5"/>'
             '<path d="m8.7 10.8 6.6-4M8.7 13.2l6.6 4"/>',
    "command": '<path d="M9 6.5A2.5 2.5 0 1 0 6.5 9H9zm0 0v11m0-11h6m-6 11A2.5 2.5 0 1 1 6.5 15H9zm0 0h6m0 0v-11m0 11'
               'a2.5 2.5 0 1 0 2.5-2.5H15zm0-11a2.5 2.5 0 1 1 2.5 2.5H15z"/>',
    "mark": '<path d="M3.5 15.5c2.6-6.5 4.8-6.5 7.2 0s4.6 6.5 7.3 0"/><circle cx="20" cy="9.5" r="1.4" fill="currentColor"/>',
}


def icon(name: str, cls: str = "") -> str:
    """An inline SVG icon, decorative (the adjacent text names the thing)."""
    paths = _ICON_PATHS.get(name, "")
    klass = f' class="{h(cls)}"' if cls else ""
    return (
        f'<svg{klass} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        f"{paths}</svg>"
    )


def initials(name: str) -> str:
    words = [w for w in str(name or "").replace("-", " ").split() if w[:1].isalnum()]
    letters = "".join(w[0] for w in words[:2]).upper()
    return letters or "?"


# --- Scripts -----------------------------------------------------------------
# Inline, and allowed by the CSP only by their exact SHA-256 (hub/admin.py
# content_security_policy hashes whatever sits between the script tags), so
# neither may ever be built from request data.

#: Runs in <head> before first paint, so a viewer who chose a theme never sees
#: the other one flash. Storage can be unavailable (private mode, blocked site
#: data): the page then simply follows the OS.
THEME_SCRIPT = (
    "<script>(function(){try{var t=localStorage.getItem('ct-theme');"
    "if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();</script>"
)

#: Everything the console does beyond server-rendered HTML. Builds every node
#: from text (createElement/textContent), never from markup strings.
APP_SCRIPT = """<script>(function(){
var d=document,root=d.documentElement;
function $(s,c){return (c||d).querySelector(s);}
function $$(s,c){return Array.prototype.slice.call((c||d).querySelectorAll(s));}
function typing(t){t=t||d.activeElement;return !!t&&(t.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));}
$$('[data-js]').forEach(function(el){el.hidden=false;});
if(/Mac|iPhone|iPad/.test(navigator.platform||'')){$$('[data-mod-key]').forEach(function(k){k.textContent='\u2318K';});}

/* Theme: system -> light -> dark, kept per browser. */
var THEMES=['system','light','dark'];
function getTheme(){try{return localStorage.getItem('ct-theme')||'system';}catch(e){return 'system';}}
function setTheme(t){
  if(t==='system'){root.removeAttribute('data-theme');}else{root.setAttribute('data-theme',t);}
  try{if(t==='system'){localStorage.removeItem('ct-theme');}else{localStorage.setItem('ct-theme',t);}}catch(e){}
  $$('[data-theme-toggle]').forEach(function(b){
    b.setAttribute('aria-label','Colour theme: '+t+'. Change theme');b.title='Theme: '+t;
    $$('[data-theme-icon]',b).forEach(function(i){i.style.display=i.getAttribute('data-theme-icon')===t?'':'none';});
  });
}
function cycleTheme(){setTheme(THEMES[(THEMES.indexOf(getTheme())+1)%3]);}
setTheme(getTheme());
$$('[data-theme-toggle]').forEach(function(b){b.addEventListener('click',cycleTheme);});

/* Relative times; the absolute one stays in the title and the datetime attribute. */
var rtf=window.Intl&&Intl.RelativeTimeFormat?new Intl.RelativeTimeFormat(undefined,{numeric:'auto'}):null;
var UNITS=[['year',31536000],['month',2592000],['week',604800],['day',86400],['hour',3600],['minute',60]];
function rel(iso){var t=Date.parse(iso);if(isNaN(t)||!rtf)return null;var s=(t-Date.now())/1000,a=Math.abs(s);
  for(var i=0;i<UNITS.length;i++){if(a>=UNITS[i][1])return rtf.format(Math.round(s/UNITS[i][1]),UNITS[i][0]);}
  return rtf.format(Math.round(s),'second');}
$$('time[data-rel]').forEach(function(el){var r=rel(el.getAttribute('datetime'));if(r)el.textContent=r;});

/* Tooltips for chart marks and anything else carrying data-tip. */
var tip=d.createElement('div');tip.className='tip';tip.hidden=true;tip.setAttribute('role','tooltip');d.body.appendChild(tip);
function place(x,y){var w=tip.offsetWidth,hgt=tip.offsetHeight,vw=window.innerWidth;
  var left=Math.min(Math.max(8,x+14),vw-w-8),top=y-hgt-12;if(top<8)top=y+18;
  tip.style.left=left+'px';tip.style.top=top+'px';}
function showTip(el,x,y){tip.textContent=el.getAttribute('data-tip');tip.hidden=false;
  if(x===undefined){var r=el.getBoundingClientRect();x=r.left+r.width/2;y=r.top;}place(x,y);}
function hideTip(){tip.hidden=true;}
$$('[data-tip]').forEach(function(el){
  el.addEventListener('pointerenter',function(e){showTip(el,e.clientX,e.clientY);});
  el.addEventListener('pointermove',function(e){if(!tip.hidden)place(e.clientX,e.clientY);});
  el.addEventListener('pointerleave',hideTip);
  el.addEventListener('focus',function(){showTip(el);});el.addEventListener('blur',hideTip);
});
window.addEventListener('scroll',hideTip,{passive:true});

/* Tables: sort by any column, filter long ones. */
function cellValue(row,i){var c=row.cells[i];if(!c)return '';var v=c.getAttribute('data-v');
  return v!==null?v:c.textContent.trim();}
function asNumber(s){var t=String(s).replace(/[,%+\\s]/g,'').replace(/^\\u2212/,'-');
  return /^-?\\d+(\\.\\d+)?$/.test(t)?parseFloat(t):null;}
$$('main .scroll > table').forEach(function(table,ti){
  var body=table.tBodies[0];if(!body)return;var rows=Array.prototype.slice.call(body.rows);
  if(rows.length<2)return;
  $$('thead th',table).forEach(function(th,i){
    if(!th.textContent.trim()||th.querySelector('.sr-only'))return;
    th.setAttribute('data-sort','');th.tabIndex=0;th.setAttribute('aria-sort','none');
    function sort(){var dir=th.getAttribute('aria-sort')==='ascending'?'descending':'ascending';
      $$('thead th',table).forEach(function(o){if(o.hasAttribute('data-sort'))o.setAttribute('aria-sort','none');});
      th.setAttribute('aria-sort',dir);var m=dir==='ascending'?1:-1;
      var all=Array.prototype.slice.call(body.rows);
      all.sort(function(a,b){var x=cellValue(a,i),y=cellValue(b,i),nx=asNumber(x),ny=asNumber(y);
        if(nx!==null&&ny!==null)return (nx-ny)*m;return x.localeCompare(y,undefined,{numeric:true})*m;});
      all.forEach(function(r){body.appendChild(r);});}
    th.addEventListener('click',sort);
    th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();sort();}});
  });
  if(rows.length<6)return;
  var tools=d.createElement('div');tools.className='table-tools';
  var input=d.createElement('input');input.type='search';input.placeholder='Filter rows\\u2026';
  input.setAttribute('aria-label','Filter this table');input.setAttribute('data-filter','');
  var count=d.createElement('span');count.className='count';count.setAttribute('aria-live','polite');
  function update(){var q=input.value.trim().toLowerCase(),n=0;
    rows.forEach(function(r){var hit=!q||r.textContent.toLowerCase().indexOf(q)!==-1;r.hidden=!hit;if(hit)n++;});
    count.textContent=n===rows.length?rows.length+' rows':n+' of '+rows.length+' rows';}
  input.addEventListener('input',update);tools.appendChild(input);tools.appendChild(count);
  table.parentNode.insertBefore(tools,table);update();
});

/* Print / save as PDF. */
$$('[data-print]').forEach(function(b){b.addEventListener('click',function(){window.print();});});

/* Command palette: every page, every action the page offers, from the keyboard. */
var items=[];
$$('.side nav a').forEach(function(a){items.push({group:'Go to',label:(a.querySelector('span')||a).textContent.trim(),href:a.getAttribute('href'),
  key:a.getAttribute('data-key')||'',icon:a.querySelector('svg')});});
$$('[data-command]').forEach(function(el){items.push({group:'On this page',label:el.getAttribute('data-command'),el:el,
  icon:el.querySelector&&el.querySelector('svg')});});
items.push({group:'Preferences',label:'Change colour theme',run:cycleTheme});
var so=$('.side-foot form[action$="/signout"]');if(so)items.push({group:'Preferences',label:'Sign out',run:function(){so.submit();}});
var dlg=null,list,input,sel=0,shown=[];
function build(){
  dlg=d.createElement('dialog');dlg.className='palette';dlg.setAttribute('aria-label','Command palette');
  var pin=d.createElement('div');pin.className='pin';var ic=$('[data-palette] svg');if(ic)pin.appendChild(ic.cloneNode(true));
  input=d.createElement('input');input.type='text';input.placeholder='Jump to a page or action\\u2026';
  input.setAttribute('role','combobox');input.setAttribute('aria-expanded','true');input.setAttribute('aria-controls','palette-list');
  input.setAttribute('aria-autocomplete','list');pin.appendChild(input);
  list=d.createElement('ul');list.id='palette-list';list.setAttribute('role','listbox');
  var foot=d.createElement('div');foot.className='foot';
  ['\\u2191\\u2193 to move','Enter to open','Esc to close','g then a letter: go to a page'].forEach(function(t){
    var s=d.createElement('span');s.textContent=t;foot.appendChild(s);});
  dlg.appendChild(pin);dlg.appendChild(list);dlg.appendChild(foot);d.body.appendChild(dlg);
  input.addEventListener('input',function(){sel=0;render();});
  input.addEventListener('keydown',function(e){
    if(e.key==='ArrowDown'){e.preventDefault();sel=Math.min(sel+1,shown.length-1);mark();}
    else if(e.key==='ArrowUp'){e.preventDefault();sel=Math.max(sel-1,0);mark();}
    else if(e.key==='Enter'){e.preventDefault();if(shown[sel])activate(shown[sel]);}
  });
  dlg.addEventListener('click',function(e){if(e.target===dlg)dlg.close();});
}
function score(label,q){label=label.toLowerCase();if(!q)return 1;var i=label.indexOf(q);if(i===0)return 3;if(i>0)return 2;
  var j=0;for(var k=0;k<label.length&&j<q.length;k++){if(label[k]===q[j])j++;}return j===q.length?1:0;}
function render(){
  var q=input.value.trim().toLowerCase();list.textContent='';
  shown=items.map(function(it){return {it:it,s:score(it.label,q)};}).filter(function(x){return x.s>0;})
    .sort(function(a,b){return b.s-a.s;}).map(function(x){return x.it;});
  if(!q){shown=items.slice();}
  if(!shown.length){var none=d.createElement('li');none.className='none';none.textContent='No match';list.appendChild(none);return;}
  var last=null;
  shown.forEach(function(it,i){
    if(it.group!==last&&!q){var g=d.createElement('li');g.setAttribute('role','presentation');
      var gl=d.createElement('div');gl.className='grp';gl.textContent=it.group;g.appendChild(gl);list.appendChild(g);last=it.group;}
    var li=d.createElement('li');var o=d.createElement('div');o.setAttribute('role','option');o.id='pal-'+i;
    if(it.icon)o.appendChild(it.icon.cloneNode(true));var t=d.createElement('span');t.textContent=it.label;o.appendChild(t);
    if(it.key){var k=d.createElement('kbd');k.textContent=it.key;o.appendChild(k);}
    o.addEventListener('click',function(){activate(it);});o.addEventListener('mousemove',function(){if(sel!==i){sel=i;mark();}});
    li.appendChild(o);list.appendChild(li);
  });mark();
}
function mark(){$$('[role=option]',list).forEach(function(o,i){var on=i===sel;o.setAttribute('aria-selected',on?'true':'false');
  if(on){input.setAttribute('aria-activedescendant',o.id);o.scrollIntoView({block:'nearest'});}});}
function activate(it){dlg.close();
  if(it.href){location.href=it.href;return;}
  if(it.run){it.run();return;}
  var el=it.el;if(!el)return;
  if(el.tagName==='A'){el.click();return;}
  el.scrollIntoView({behavior:'smooth',block:'start'});
  var f=el.matches('input,select,textarea,button')?el:el.querySelector('input:not([type=hidden]),select,textarea,button');
  if(f)setTimeout(function(){f.focus();},250);}
function openPalette(){if(!dlg)build();input.value='';sel=0;render();
  if(dlg.showModal){dlg.showModal();}else{dlg.setAttribute('open','');}input.focus();}
$$('[data-palette]').forEach(function(b){b.addEventListener('click',openPalette);});

/* Keyboard: Ctrl/Cmd+K palette, / filter or search, g+letter go to a page. */
var pendingG=0;
d.addEventListener('keydown',function(e){
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openPalette();return;}
  if(typing(e.target)||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==='/'){var f=$('main input[type=search]')||$('main [data-filter]');if(f){e.preventDefault();f.focus();}return;}
  if(e.key==='g'){pendingG=Date.now();return;}
  if(pendingG&&Date.now()-pendingG<1200){pendingG=0;var a=$('.side nav a[data-key="g '+e.key.toLowerCase()+'"]');
    if(a){e.preventDefault();location.href=a.getAttribute('href');}}
});
})();</script>"""


# --- Formatting ----------------------------------------------------------------


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def time_html(value: object, *, empty: str = "—") -> str:
    """A timestamp a person can read ("Sep 29, 2026 01:32 UTC"), machine-readable
    in `datetime`, and shown relative ("3 days ago") once script runs."""
    dt = _as_datetime(value)
    if dt is None:
        return f'<span class="faint">{h(value) if value not in (None, "") else empty}</span>'
    label = f"{dt:%b} {dt.day}, {dt:%Y} {dt:%H:%M} UTC"
    return f'<time datetime="{dt.isoformat(timespec="seconds")}" title="{label}" data-rel>{label}</time>'


def meter(used: object, limit: object, *, quota: bool = True) -> str:
    """A thin usage bar under a quota figure; nothing for an unlimited quota."""
    try:
        used_f, limit_f = float(used or 0), float(limit or 0)
    except (TypeError, ValueError):
        return ""
    if limit_f <= 0:
        return ""
    share = used_f / limit_f
    # Amber and red mean "approaching / at a quota"; progress is not a quota.
    tone = "" if not quota else (" bad" if share >= 1 else (" warn" if share >= 0.8 else ""))
    return (
        f'<div class="meter{tone}" role="img" aria-label="{share:.0%} of the limit used">'
        f'<span style="width:{min(100.0, share * 100):.1f}%"></span></div>'
    )


def _week_label(iso_day: str) -> str:
    dt = _as_datetime(iso_day)
    return f"{dt:%b} {dt.day}" if dt else iso_day


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exp:
            return step * exp
    return 10 * exp


def _count_axis(peak: float) -> tuple[float, float]:
    """(axis top, tick step) for whole-number counts: about four ticks, never a
    fractional one -- a "2.5 traces" gridline is a lie about the data."""
    peak = max(1.0, float(peak))
    step = _nice_max(peak / 4)
    step = max(1.0, math.ceil(step))
    return step * math.ceil(peak / step), step


def _table_view(caption: str, header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h(c)}</th>" for c in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (
        f'<details><summary>Show the data as a table</summary><div class="scroll"><table>'
        f'<caption class="sr-only">{h(caption)}</caption><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></details>"
    )


def bar_chart(chart_id: str, weeks: list[str], values: list[int], *, title: str, unit: str) -> str:
    """Weekly counts as bars; one series, so no legend -- the title names it."""
    width, height, left, right, top, bottom = 760, 200, 34, 8, 10, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    peak, tick = _count_axis(max(values or [0]))
    n = max(1, len(values))
    band = plot_w / n
    bar_w = max(4.0, band * 0.62)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{chart_id}-t {chart_id}-d">'
        f'<title id="{chart_id}-t">{h(title)}</title>'
        f'<desc id="{chart_id}-d">{h(unit)} per week, {h(_week_label(weeks[0]) if weeks else "")} to '
        f'{h(_week_label(weeks[-1]) if weeks else "")}; total {sum(values)}.</desc><g class="viz-grid">'
    ]
    for value in range(0, int(peak) + 1, int(tick)):
        y = top + plot_h * (1 - value / peak)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="viz-axis" x="{left - 6}" y="{y + 3.5:.1f}" text-anchor="end">'
                     f"{value:,}</text>")
    parts.append("</g>")
    label_every = max(1, math.ceil(n / 6))
    for i, (week, value) in enumerate(zip(weeks, values)):
        x = left + band * i + (band - bar_w) / 2
        bar_h = plot_h * (value / peak) if peak else 0
        y = top + plot_h - bar_h
        radius = min(4.0, bar_w / 2, bar_h)
        tip = f"Week of {_week_label(week)}\n{value:,} {unit}"
        if bar_h > 0:
            # Rounded at the data end only, anchored square to the baseline.
            path = (f"M{x:.1f},{top + plot_h:.1f} V{y + radius:.1f} Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f} "
                    f"H{x + bar_w - radius:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + radius:.1f} "
                    f"V{top + plot_h:.1f} Z")
            mark = f'<path class="viz-bar" d="{path}"/>'
        else:
            mark = ""
        parts.append(
            f'<g class="viz-bar-g" role="img" tabindex="0" data-tip="{h(tip)}" aria-label="{h(tip)}">'
            f'<rect x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" fill="transparent"/>'
            f"{mark}</g>"
        )
        if i % label_every == 0 or i == n - 1:
            parts.append(f'<text class="viz-axis" x="{left + band * i + band / 2:.1f}" y="{height - 6}" '
                         f'text-anchor="middle">{h(_week_label(week))}</text>')
    parts.append(f'<line class="viz-base" x1="{left}" x2="{width - right}" y1="{top + plot_h}" y2="{top + plot_h}"/>')
    parts.append("</svg>")
    table = _table_view(title, ["Week of", unit.capitalize()],
                        [[h(_week_label(w)), f"{v:,}"] for w, v in zip(weeks, values)])
    return f'<div class="chart"><div class="plot">{"".join(parts)}</div>{table}</div>'


#: Fewest resolved occasions an arm needs in a week for that week's rate to be
#: drawn. A rate from two occasions swings between 0% and 100% and reads as an
#: event; those weeks are gaps in the line, and stay in the tooltip and table.
MIN_POINT_N = 5


def rate_chart(chart_id: str, weeks: list[str], treated: list, control: list, *, title: str) -> str:
    """Weekly success rate with memory and without it, one shared 0-100% axis.

    Two series, so a legend and direct end labels; a week with no resolved
    occasion in an arm is a gap in that line, never a zero. A crosshair
    column per week carries both readings in one tooltip."""
    width, height, left, right, top, bottom = 760, 220, 40, 112, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    n = max(1, len(weeks))
    step = plot_w / max(1, n - 1) if n > 1 else 0

    def x_at(i: int) -> float:
        return left + (step * i if n > 1 else plot_w / 2)

    def y_at(rate: float) -> float:
        return top + plot_h * (1 - rate)

    def series(points: list) -> tuple[str, list[tuple[float, float, float, int]]]:
        path, dots, pen = [], [], False
        for i, pair in enumerate(points):
            total, ok = (pair[0], pair[1]) if pair else (0, 0)
            if total >= MIN_POINT_N:
                rate = ok / total
                x, y = x_at(i), y_at(rate)
                path.append(f"{'L' if pen else 'M'}{x:.1f},{y:.1f}")
                dots.append((x, y, rate, total))
                pen = True
            else:
                pen = False
        return " ".join(path), dots

    p1, d1 = series(treated)
    p2, d2 = series(control)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{chart_id}-t {chart_id}-d">'
        f'<title id="{chart_id}-t">{h(title)}</title>'
        f'<desc id="{chart_id}-d">Share of resolved occasions that succeeded each week, for occasions that '
        "received memory and occasions the experiment withheld it from. Weeks with fewer than "
        f"{MIN_POINT_N} occasions in an arm are gaps in that arm's line.</desc><g class=\"viz-grid\">"
    ]
    for i in range(5):
        y = top + plot_h * i / 4
        parts.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="viz-axis" x="{left - 6}" y="{y + 3.5:.1f}" text-anchor="end">'
                     f"{100 - 25 * i}%</text>")
    parts.append("</g>")
    label_every = max(1, math.ceil(n / 6))
    for i, week in enumerate(weeks):
        if i % label_every == 0 or i == n - 1:
            parts.append(f'<text class="viz-axis" x="{x_at(i):.1f}" y="{height - 6}" text-anchor="middle">'
                         f"{h(_week_label(week))}</text>")
    # Crosshair columns first, so the lines draw over their hit areas.
    col_w = step if n > 1 else plot_w
    for i, week in enumerate(weeks):
        lines = [f"Week of {_week_label(week)}"]
        for name, data in (("With memory", treated), ("Without memory", control)):
            total, ok = data[i] if i < len(data) and data[i] else (0, 0)
            lines.append(f"{name}: {ok / total:.0%} of {total:,}" if total else f"{name}: no outcomes")
        tip = "\n".join(lines)
        cx = x_at(i)
        parts.append(
            f'<g class="viz-col" role="img" tabindex="0" data-tip="{h(tip)}" aria-label="{h(tip)}">'
            f'<rect class="hit" x="{cx - col_w / 2:.1f}" y="{top}" width="{col_w:.1f}" height="{plot_h}"/>'
            f'<line class="hair" x1="{cx:.1f}" x2="{cx:.1f}" y1="{top}" y2="{top + plot_h}"/></g>'
        )
    for path, cls in ((p2, "viz-l2"), (p1, "viz-l1")):
        if path:
            parts.append(f'<path class="viz-line {cls}" d="{path}"/>')
    for dots, cls in ((d2, "viz-d2"), (d1, "viz-d1")):
        for x, y, _rate, _total in dots:
            parts.append(f'<circle class="viz-dot {cls}" cx="{x:.1f}" cy="{y:.1f}" r="4" pointer-events="none"/>')
    # Direct labels at each line's last point, nudged apart if they collide.
    ends = []
    for dots, name in ((d1, "With"), (d2, "Without")):
        if dots:
            x, y, rate, _t = dots[-1]
            ends.append([x, y, f"{name} {rate:.0%}"])
    if len(ends) == 2 and abs(ends[0][1] - ends[1][1]) < 14:
        upper, lower = sorted(ends, key=lambda e: e[1])
        mid = (upper[1] + lower[1]) / 2
        upper[1], lower[1] = mid - 7, mid + 7
    for x, y, text in ends:
        parts.append(f'<text class="viz-label" x="{x + 8:.1f}" y="{y + 4:.1f}">{h(text)}</text>')
    parts.append(f'<line class="viz-base" x1="{left}" x2="{left + plot_w}" y1="{top + plot_h}" y2="{top + plot_h}"/>')
    parts.append("</svg>")

    def cell(pair) -> str:
        total, ok = (pair[0], pair[1]) if pair else (0, 0)
        return f"{ok / total:.0%} of {total:,}" if total else '<span class="faint">—</span>'

    table = _table_view(title, ["Week of", "With memory", "Without memory"],
                        [[h(_week_label(w)), cell(t), cell(c)] for w, t, c in zip(weeks, treated, control)])
    legend = ('<div class="legend" aria-hidden="true"><span><i class="s1"></i>With memory</span>'
              '<span><i class="s2"></i>Without memory (held out)</span>'
              f'<span class="faint">weeks under {MIN_POINT_N} occasions in an arm are left as gaps</span></div>')
    return f'<div class="chart">{legend}<div class="plot">{"".join(parts)}</div>{table}</div>'


def forest_plot(effects: list[dict]) -> str:
    """Each memory's estimated effect and 95% interval on one shared axis, the
    zero line dashed. Colour is the verdict's, and the verdict is also written
    next to it, so colour never carries the meaning alone."""
    rows = [e for e in effects if isinstance(e.get("effect"), (int, float))]
    if not rows:
        return ""
    lows, highs = [0.0], [0.0]
    for e in rows:
        ci = e.get("ci_95")
        if isinstance(ci, (list, tuple)) and len(ci) == 2 and all(isinstance(v, (int, float)) for v in ci):
            lows.append(float(ci[0]))
            highs.append(float(ci[1]))
        lows.append(float(e["effect"]))
        highs.append(float(e["effect"]))
    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.08 or 0.05
    lo, hi = lo - pad, hi + pad

    def pos(value: float) -> float:
        return (value - lo) / (hi - lo) * 100

    out = ['<div class="forest" role="table" aria-label="Effect of each memory, with 95% confidence intervals">'
           '<div class="fr" role="row">'
           '<div class="fh" role="columnheader">Memory</div>'
           '<div class="fh ft-h" role="columnheader">Effect on success rate, 95% interval</div>'
           '<div class="fh" role="columnheader" style="text-align:right">Estimate</div></div>']
    zero = pos(0.0)
    for e in rows:
        effect = float(e["effect"])
        ci = e.get("ci_95") if isinstance(e.get("ci_95"), (list, tuple)) else None
        verdict = str(e.get("verdict") or "")
        significant = bool(e.get("significant"))
        if verdict == "HURTS" or (significant and effect < 0):
            tone, pill = "f-bad", "bad"
        elif significant and effect > 0:
            tone, pill = "f-ok", "ok"
        else:
            tone, pill = "f-mute", "mute"
        name = e.get("title") or e.get("trace_id")
        ci_text = f"[{ci[0]:+.1%}, {ci[1]:+.1%}]" if ci else ""
        tip = (f"{name}\nEffect {effect:+.1%} {ci_text}\n"
               f"With: {e.get('rate_injected', 0):.0%} (n={e.get('n_injected', 0)}) · "
               f"Without: {e.get('rate_withheld', 0):.0%} (n={e.get('n_withheld', 0)})")
        track = [f'<svg class="{tone}" viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true">'
                 f'<line class="fz" x1="{zero:.2f}" x2="{zero:.2f}" y1="0" y2="22" vector-effect="non-scaling-stroke"/>']
        if ci:
            track.append(f'<line class="fci" x1="{pos(float(ci[0])):.2f}" x2="{pos(float(ci[1])):.2f}" y1="11" y2="11" '
                         'vector-effect="non-scaling-stroke"/>')
        # A zero-length round-capped line is a dot that survives the track's
        # non-uniform scaling; a <circle> would be squashed into an ellipse.
        track.append(f'<line class="fring" x1="{pos(effect):.2f}" x2="{pos(effect):.2f}" y1="11" y2="11" '
                     'vector-effect="non-scaling-stroke"/>'
                     f'<line class="fpt" x1="{pos(effect):.2f}" x2="{pos(effect):.2f}" y1="11" y2="11" '
                     'vector-effect="non-scaling-stroke"/></svg>')
        out.append(
            f'<div class="fr" role="row"><div class="fl" role="cell">{h(name)}<br><span class="pill {pill}">{h(verdict or "—")}</span></div>'
            f'<div class="ft" role="cell" tabindex="0" data-tip="{h(tip)}" aria-label="{h(tip)}">{"".join(track)}</div>'
            f'<div class="fv" role="cell"><b>{effect:+.1%}</b><br><span class="faint">{h(ci_text)}</span></div></div>'
        )
    out.append("</div>")
    out.append(f'<div class="forest-axis" aria-hidden="true"><span>{lo:+.0%}</span>'
               f'<span>0 = no effect</span><span>{hi:+.0%}</span></div>')
    return "".join(out)
