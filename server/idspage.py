"""Public page listing the game ids (characters, items, equipment) players use with the Discord bot's /claim.

Served by main.py at exactly /ids (no file read from disk, GET only, no player data): the HTML is built once, names
escaped, and the search runs in the browser. Names: English from localization.bin where it has one, else the
Japanese master name and the internal label. Built from the raw master data and cached in
server_cache/master_ids_page.json (the VPS has no raw master data: copy that file with the deploy); falls back to the
admin panel names (master.NAMES) when neither exists.
"""
import json
import os
import re

import master

_page = {}
# item.bin sub 8 / 9 are the exchange shop's inputs and outputs (524xxxxxx / 525xxxxxx), not items: one of them
# claimed through the Discord bot ("monthly21 04 01" 524101090) crashed the gift box (tester report, 2026-10-20)
EXCHANGE_SHEETS = ("item.8.json", "item.9.json")
LABEL = re.compile(r"^[a-z][a-z0-9_]*\.[A-Za-z0-9_.]+$")  # "material.story7_exchange"


def _rows(name):
    with open(os.path.join(master.JSON_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _label_text(label: str) -> str:
    """"material.story7_exchange" -> "story7 exchange"."""
    return label.split(".", 1)[-1].replace("_", " ")


def _build() -> dict:
    import glob  # noqa: PLC0415
    loc = {}
    for r in _rows("localization.0.json"):
        label, field, text = r.get("1"), r.get("3"), r.get("4")
        if not (isinstance(label, str) and isinstance(text, str) and text):
            continue
        if field == "name" and (label not in loc or text.startswith("[")):  # "[Title] Name" beats the bare name
            loc[label] = text
    items = []
    # lottery.0 holds the scout tickets (177xxxxxx), real items too (the admin panel names read it as well)
    for path in sorted(glob.glob(os.path.join(master.JSON_DIR, "item.*.json"))) + [
            os.path.join(master.JSON_DIR, "lottery.0.json")]:
        if os.path.basename(path) in EXCHANGE_SHEETS:
            continue
        for r in _rows(os.path.basename(path)):
            if not isinstance(r.get("0"), int):
                continue
            # the item sub-tables put the label and the Japanese name in different fields: look at all of them
            labels = [v for _, v in sorted(r.items(), key=lambda kv: int(kv[0]))
                      if isinstance(v, str) and LABEL.match(v) and not v.startswith("release.")]
            jp = next((r[k] for k in ("1", "2") if isinstance(r.get(k), str) and not r[k].isascii()
                       and "\n" not in r[k]), "")
            en = next((loc[lb] for lb in labels if lb in loc), "")
            if not (en or jp or labels):  # nameless rows (campaign bonus illustrations): nothing to claim
                continue
            items.append([r["0"], en or jp or _label_text(labels[0]),
                          labels[0].split(".")[0] if labels else "", jp if en else ""])
    seen = set()
    items = [i for i in items if not (i[0] in seen or seen.add(i[0]))]
    equipment = []
    for r in _rows("equipment.6.json"):
        label = r.get("6") or ""
        equipment.append([r["0"], r.get("7") or "", _label_text(label), label.replace("equipment.", "").split("_")[0]])
    pcs = []
    for r in _rows("pc.0.json"):
        label = r.get("6") or ""
        pcs.append([r["0"], loc.get(label) or _label_text(label), r.get("1") or ""])
    return {"pcs": pcs, "items": items, "equipment": equipment}


def data() -> dict:
    try:
        return master._cached("ids_page2", _build)
    except (OSError, ValueError):
        n = master.NAMES
        return {"pcs": [[int(k), v, ""] for k, v in n.get("pcs", {}).items()],
                "items": [[int(k), v, "", ""] for k, v in n.get("items", {}).items()],
                "equipment": [[int(k), v, "", ""] for k, v in n.get("equipment", {}).items()]}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SAO UB — game ids</title>
<style>
:root{--bg:#14161c;--card:#1d2029;--line:#2c3040;--text:#e6e8ee;--muted:#8e94a6;--accent:#f0a830}
@media (prefers-color-scheme:light){:root{--bg:#f4f5f8;--card:#fff;--line:#dde0e8;--text:#1b1d24;--muted:#646a7d;--accent:#b8750b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}
p.muted,.muted{color:var(--muted)}code{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.bar{position:sticky;top:0;background:var(--bg);padding:12px 0;display:flex;gap:8px;flex-wrap:wrap;z-index:1}
.tabs button{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 12px;
cursor:pointer;font:inherit}.tabs button.on{border-color:var(--accent);color:var(--accent)}
input,select{flex:1;min-width:180px;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:6px;
padding:8px 10px;font:inherit}select{flex:0 1 auto;min-width:0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
td,th{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}
td.id{font-family:ui-monospace,monospace;white-space:nowrap;cursor:copy}td.id:hover{color:var(--accent)}
.sub{color:var(--muted);font-size:13px}#count{margin:6px 0;color:var(--muted)}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--accent);color:#000;padding:6px 12px;
border-radius:6px;opacity:0;transition:opacity .2s}#toast.on{opacity:1}
</style></head><body><main>
<h1>SAO Unleash Blading — game ids</h1>
<p class="muted">Ids for the Discord bot: <code>/claim character</code> (type the name, suggestions appear),
<code>/claim item</code> (name or id). Click an id to copy it.</p>
<div class="bar"><div class="tabs" id="tabs"></div><input id="q" type="search" placeholder="Search a name or an id…"
autocomplete="off"><select id="cat"></select></div>
<div id="count"></div><table><thead id="head"></thead><tbody id="rows"></tbody></table>
<div id="toast">Copied</div></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const TABS = {pcs: ["Characters", ["Id", "Name"]], items: ["Items", ["Id", "Name", "Type"]],
              equipment: ["Equipment", ["Id", "Name", "Type"]]};
let tab = (location.hash.slice(1) in TABS) ? location.hash.slice(1) : "pcs";
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const CAT = {items: 2, equipment: 3};  // column holding the type filter
const cats = () => tab in CAT ? [...new Set(D[tab].map(r => r[CAT[tab]]).filter(Boolean))].sort() : [];
function row(r) {
  if (tab === "pcs") return `<td class="id">${r[0]}</td><td>${esc(r[1])}<div class="sub">${esc(r[2])}</div></td>`;
  if (tab === "items") return `<td class="id">${r[0]}</td><td>${esc(r[1])}${r[3] ? `<div class="sub">${esc(r[3])}</div>` : ""}</td><td class="sub">${esc(r[2].replace(/_/g, " "))}</td>`;
  return `<td class="id">${r[0]}</td><td>${esc(r[1])}<div class="sub">${esc(r[2])}</div></td><td class="sub">${esc(r[3])}</td>`;
}
function render() {
  const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean), c = cat.value;
  const ci = tab === "items" ? 2 : 3;
  const hits = D[tab].filter(r => (!c || r[ci] === c) && words.every(w => r.join(" ").toLowerCase().includes(w)));
  count.textContent = `${hits.length} / ${D[tab].length}` + (hits.length > 500 ? " — first 500 shown, refine the search" : "");
  rows.innerHTML = hits.slice(0, 500).map(r => `<tr>${row(r)}</tr>`).join("");
}
function show(t) {
  tab = t; history.replaceState(null, "", "#" + t);
  tabs.innerHTML = Object.entries(TABS).map(([k, v]) => `<button data-t="${k}" class="${k === tab ? "on" : ""}">${v[0]} (${D[k].length})</button>`).join("");
  head.innerHTML = "<tr>" + TABS[tab][1].map(h => `<th>${h}</th>`).join("") + "</tr>";
  const cs = cats(); cat.value = ""; cat.style.display = cs.length ? "" : "none";
  cat.innerHTML = `<option value="">All types</option>` + cs.map(x => `<option>${esc(x)}</option>`).join("");
  render();
}
tabs.onclick = e => { const t = e.target.closest("button"); if (t) show(t.dataset.t); };
q.oninput = cat.onchange = render;
rows.onclick = e => {
  const td = e.target.closest("td.id"); if (!td) return;
  navigator.clipboard && navigator.clipboard.writeText(td.textContent);
  toast.classList.add("on"); setTimeout(() => toast.classList.remove("on"), 900);
};
show(tab);
</script></body></html>"""


def page() -> bytes:
    if "html" not in _page:
        import api  # noqa: PLC0415
        d = dict(data())
        giftable = api.giftable_items()  # the others crash the gift box: not listed
        d["items"] = [r for r in d["items"] if r[0] in giftable]
        for k in d:
            d[k].sort(key=lambda r: (str(r[1]).lower(), r[0]) if k == "pcs" else r[0])
        # JSON inside <script>: "</" and "<!--" must not appear, html.escape is not usable there
        blob = json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        _page["html"] = PAGE.replace("__DATA__", blob).encode("utf-8")
    return _page["html"]

