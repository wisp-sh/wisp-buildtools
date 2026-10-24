"""Self-contained interactive view of measured Wisp toolchain bundles."""
from __future__ import annotations

import json


def render_html(data: dict) -> str:
    """Render a portable report without network access or third-party scripts."""
    # The interactive view needs content identities and sizes, not thousands of
    # full paths repeated in each package inventory. Keep the complete inventory
    # in sizes.json and use stable content indexes for exact selection totals.
    content_ids, content_sizes, packages = {}, [], []
    for package in data.get("packages", []):
        item = {key: value for key, value in package.items() if key != "filesInventory"}
        item["contentIds"] = []
        for file in package.get("filesInventory", {}).values():
            identity = (file["sha256"], file["size"])
            if identity not in content_ids:
                content_ids[identity] = len(content_sizes)
                content_sizes.append(file["size"])
            item["contentIds"].append(content_ids[identity])
        packages.append(item)
    view = {"packages": packages, "contentSizes": content_sizes,
            "executionHosts": [{key: value for key, value in host.items() if key != "files"}
                               for host in data.get("executionHosts", [])],
            "measurement": data.get("measurement", "")}
    encoded = json.dumps(view, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    # The report contains source filenames and notes, which must not terminate
    # the JSON script element when a package has an unusual name.
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _DOCUMENT.replace("__REPORT_DATA__", encoded)


_DOCUMENT = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wisp compiler package sizes</title>
<style>
:root{color-scheme:light dark;--report-wasm:#5689d9;--report-native:#c68a42;--report-support:#8b939f}
*{box-sizing:border-box}body{margin:0;background:transparent;color:var(--foreground,#263142);font:14px/1.5 var(--font-sans,system-ui,sans-serif)}
main{max-width:1100px;margin:0 auto}h1{font-size:22px;letter-spacing:-.02em;line-height:1.2;margin:0 0 9px}h2{font-size:14px;margin:0 0 8px}p{margin:0 0 14px}.muted{color:var(--muted-foreground,#687384)}
.intro{font-size:13px;max-width:850px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:17px 0}.stat{padding:14px 15px;background:var(--card,#f4f6f8);border:1px solid var(--border,#e0e4e9);border-radius:var(--radius,10px)}.stat span{display:block;font-size:11px;line-height:1.35;color:var(--muted-foreground,#687384)}.stat strong{display:block;font-size:23px;letter-spacing:-.03em;font-variant-numeric:tabular-nums;margin:4px 0}.stat small{font-size:10px;display:block;color:var(--muted-foreground,#687384)}
.toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;justify-content:space-between;margin:0 0 12px}.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}button,select{font:inherit;font-size:12px;color:inherit;background:var(--card,#fff);border:1px solid var(--border,#d7dde5);border-radius:7px;padding:6px 9px;cursor:pointer}button:hover,select:hover{background:var(--secondary,#edf1f6)}button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--ring,#5689d9);outline-offset:3px}.legend{display:flex;gap:11px;font-size:11px;color:var(--muted-foreground,#687384)}.legend i{display:inline-block;height:7px;width:7px;border-radius:2px;margin-right:4px}.wasm{background:var(--report-wasm)}.native{background:var(--report-native)}.support{background:var(--report-support)}
table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}th{font-weight:500;color:var(--muted-foreground,#687384);font-size:10px;text-align:right;padding:8px 5px;border-bottom:1px solid var(--border,#dce2e8)}th:first-child{text-align:left;width:27%}th:last-child{width:67px}td{padding:10px 5px;text-align:right;vertical-align:middle;border-bottom:1px solid var(--border,#e3e8ee);font-variant-numeric:tabular-nums}td:first-child{text-align:left}.tool-label{display:flex;align-items:center;gap:8px;font-weight:650}.tool-label input{accent-color:var(--primary,#5689d9);margin:0;width:14px;height:14px;flex-shrink:0}.tool-label span{overflow-wrap:anywhere}.version{font-size:9px;color:var(--muted-foreground,#687384);margin:3px 0 0 22px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sdk{color:var(--muted-foreground,#687384);font-size:9px;font-weight:500;margin-left:4px}.unselected{opacity:.52}.number{white-space:nowrap}.breakdown{height:4px;display:flex;overflow:hidden;border-radius:3px;background:var(--secondary,#e9edf2);margin:6px 0 0 auto;max-width:130px;min-width:25px}.breakdown span{height:100%}.details-button{padding:4px 6px;font-size:10px;background:transparent}.detail-cell{text-align:left;padding:16px;background:var(--card,#f7f9fb)}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.requirement{margin:0 0 10px;font-size:11px}.requirement strong{font-weight:600}.requirement p{font-size:10px;margin:3px 0 0;color:var(--muted-foreground,#687384)}.badge{font-size:9px;padding:2px 5px;border:1px solid var(--border,#dce2e8);border-radius:5px;margin-right:5px}.top-files{margin:0;padding:0;list-style:none}.top-files li{margin:0 0 9px;font-size:10px}.top-files code{display:block;font:10px/1.4 var(--font-mono,monospace);overflow-wrap:anywhere;color:var(--muted-foreground,#687384)}.facts{font-size:10px;color:var(--muted-foreground,#687384);margin-bottom:13px}.facts code{overflow-wrap:anywhere}details{font-size:11px;margin-top:15px}summary{cursor:pointer;font-weight:600}details ul{font-size:10px;color:var(--muted-foreground,#687384);padding-left:17px}.note{font-size:11px;color:var(--muted-foreground,#687384);margin-top:13px;line-height:1.55}.note p{margin:0 0 7px}.empty{padding:30px;text-align:center;color:var(--muted-foreground,#687384)}[hidden]{display:none!important}
@media(prefers-color-scheme:dark){body{color:var(--foreground,#e5e9f0)}.stat,button,select,.detail-cell{background:var(--card,#20252e);border-color:var(--border,#353c48)}th,td{border-color:var(--border,#353c48)}.muted,.stat span,.stat small,.legend,th,.version,.sdk,.requirement p,.top-files code,.facts,.note{color:var(--muted-foreground,#a4adba)}}
@media(max-width:600px){h1{font-size:20px}.stats{gap:7px}.stat{padding:11px 9px}.stat strong{font-size:19px}.stat span{font-size:10px}.stat small{font-size:9px}.guest-column,.native-column{display:none}th:first-child{width:35%}th:last-child{width:54px}td{padding:9px 3px}th{padding:7px 3px}.toolbar{align-items:flex-start}.legend{font-size:10px;gap:8px}.detail-grid{grid-template-columns:1fr;gap:15px}.detail-cell{padding:13px}.details-button{font-size:9px;padding:4px}.intro{font-size:12px}}
@media(max-width:390px){.stats{grid-template-columns:repeat(2,1fr)}.stat strong{font-size:21px}.toolbar{gap:12px}.number{font-size:11px}.tool-label{font-size:11px}}
</style>
</head>
<body>
<main>
<h1>Compiler package sizes</h1>
<p class="intro muted">Compare the files needed to use each compiler. Downloads are measured Wisp bundles; installed payloads are their uncompressed file bytes. JavaScript and TypeScript share one package.</p>
<div class="stats" aria-live="polite" aria-atomic="true">
<div class="stat"><span>Selected downloads</span><strong id="download">—</strong><small id="selected-count">Measured bundles</small></div>
<div class="stat"><span>Installed payload</span><strong id="installed">—</strong><small>Sum across selected packages</small></div>
<div class="stat"><span>Unique file content</span><strong id="unique">—</strong><small id="duplicate">Potential content deduplication</small></div>
<div class="stat"><span>Bundled native binaries</span><strong id="native">—</strong><small>Wisp hosts are measured separately below</small></div>
</div>
<div class="toolbar">
<div class="controls"><button type="button" id="clear-tools">Clear</button><button type="button" id="all-tools">All tools</button><label for="sort" class="muted">Sort</label><select id="sort"><option value="downloadBytes">Download size</option><option value="installedBytes">Installed size</option><option value="tool">Tool name</option></select></div>
<div class="legend" aria-label="Installed file categories"><span><i class="wasm"></i>Wasm</span><span><i class="native"></i>Native</span><span><i class="support"></i>Support</span></div>
</div>
<table aria-label="Compiler bundle sizes"><thead><tr><th scope="col">Tool</th><th scope="col">Download</th><th scope="col">Installed payload</th><th scope="col" class="guest-column">Wasm</th><th scope="col" class="native-column">Native</th><th scope="col"><span class="muted">Contents</span></th></tr></thead><tbody id="packages"></tbody></table>
<div class="note"><p>Selecting a compiler also selects its required shared packages. LLVM is loaded from one external Wasm library package by Clang and Nim; Zig uses the shared Clang package.</p><p>Unique content counts identical files once by SHA-256. Shared package dependencies are counted once. Generated installation receipts, Wisp execution hosts, build caches, temporary files, filesystem overhead and RAM are outside these totals.</p><div id="execution-hosts"></div><details><summary>Measurement method and units</summary><p id="measurement"></p></details></div>
<noscript><p>This interactive view requires JavaScript. The adjacent sizes.md and sizes.json files contain the measured report.</p></noscript>
</main>
<script type="application/json" id="report-data">__REPORT_DATA__</script>
<script>
(() => {
  'use strict';
  const data=JSON.parse(document.getElementById('report-data').textContent);
  const packages=Array.isArray(data.packages)?data.packages:[];
  const selected=new Set(packages.map(p=>p.tool));
  const byName=new Map(packages.map(p=>[p.tool,p]));
  function effectiveSelection(){const names=new Set();function add(name){if(names.has(name))return;names.add(name);for(const dep of byName.get(name)?.dependencies||[])add(dep);}for(const name of selected)add(name);return names;}
  const expanded=new Set();
  const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bytes=n=>{n=Number(n)||0;if(n<1024)return n+' B';const unit=n<1048576?'KiB':n<1073741824?'MiB':'GiB';const scale=unit==='KiB'?1024:unit==='MiB'?1048576:1073741824;return (n/scale).toFixed(n/scale>=100?0:n/scale>=10?1:2)+' '+unit;};
  const exact=n=>(Number(n)||0).toLocaleString('en-US')+' bytes';
  const cell=n=>'<span class="number" title="'+exact(n)+'">'+bytes(n)+'</span>';
  const width=(n,total)=>total?Math.max(0,Number(n)||0)*100/total:0;
  function details(p){
    const requirements=(p.requirements||[]).map(r=>'<div class="requirement"><span class="badge">'+(r.included?'bundled':'external')+'</span><strong>'+escape(r.name)+(r.version?' '+escape(r.version):'')+'</strong><p>'+escape(r.note)+'</p></div>').join('')||'<p class="muted">No additional host requirements recorded.</p>';
    const files=(p.topFiles||[]).slice(0,8).map(f=>'<li><strong>'+bytes(f.size)+'</strong> · '+escape(f.role)+'<code>'+escape(f.path)+'</code></li>').join('');
    const bootstrap=(p.bootstrapRequirements||[]).map(x=>'<li>'+escape(x)+'</li>').join('');
    return '<div class="facts">'+escape(p.identity)+'@'+escape(p.version)+' · '+escape(p.files)+' files · package '+escape(p.upstreamVersion)+'<br>Complete installation '+bytes(p.closureInstalledBytes)+' · download with dependencies '+bytes(p.closureDownloadBytes)+'<br><code>'+escape(p.archive)+'</code></div><div class="detail-grid"><section><h2>Host requirements</h2>'+requirements+'</section><section><h2>Largest files</h2><ol class="top-files">'+files+'</ol></section></div>'+(bootstrap?'<details><summary>Building this compiler package from source</summary><ul>'+bootstrap+'</ul></details>':'');
  }
  function totals(){
    let download=0,installed=0,native=0;const content=new Map(),effective=effectiveSelection();
    for(const p of packages){if(!effective.has(p.tool))continue;download+=p.downloadBytes||0;installed+=p.installedBytes||0;native+=p.nativeBytes||0;for(const id of p.contentIds||[]){content.set(id,data.contentSizes[id]);}}
    const unique=[...content.values()].reduce((a,b)=>a+b,0);
    for(const [id,n] of [['download',download],['installed',installed],['unique',unique],['native',native]]){const e=document.getElementById(id);e.textContent=bytes(n);e.title=exact(n);}
    document.getElementById('selected-count').textContent=effective.size+' of '+packages.length+' packages, including dependencies';
    document.getElementById('duplicate').textContent=bytes(Math.max(0,installed-unique))+' duplicate content';
  }
  function render(){
    const sort=document.getElementById('sort').value,effective=effectiveSelection();
    const ordered=[...packages].sort((a,b)=>sort==='tool'?a.tool.localeCompare(b.tool):(b[sort]||0)-(a[sort]||0));
    document.getElementById('packages').innerHTML=ordered.map(p=>{
      const on=effective.has(p.tool),open=expanded.has(p.tool),id='detail-'+p.tool;
      return '<tr class="'+(on?'':'unselected')+'"><td><label class="tool-label"><input type="checkbox" data-tool="'+escape(p.tool)+'" aria-label="Include '+escape(p.tool)+' in totals" '+(on?'checked':'')+'><span>'+escape(p.tool)+(on&&!selected.has(p.tool)?'<span class="sdk">required</span>':'')+'</span></label><div class="version" title="'+escape(p.upstreamVersion)+'">'+escape(p.upstreamVersion)+'</div></td><td>'+cell(p.downloadBytes)+'</td><td>'+cell(p.installedBytes)+'<div class="breakdown" role="img" aria-label="'+escape(bytes(p.wasmBytes)+' Wasm, '+bytes(p.nativeBytes)+' native, '+bytes(p.supportBytes)+' support')+'"><span class="wasm" style="width:'+width(p.wasmBytes,p.installedBytes)+'%"></span><span class="native" style="width:'+width(p.nativeBytes,p.installedBytes)+'%"></span><span class="support" style="width:'+width(p.supportBytes,p.installedBytes)+'%"></span></div></td><td class="guest-column">'+cell(p.wasmBytes)+'</td><td class="native-column">'+cell(p.nativeBytes)+'</td><td><button class="details-button" type="button" data-detail="'+escape(p.tool)+'" aria-expanded="'+open+'" aria-controls="'+escape(id)+'">'+(open?'Close':'Details')+'</button></td></tr><tr id="'+escape(id)+'" '+(open?'':'hidden')+'><td class="detail-cell" colspan="6">'+details(p)+'</td></tr>';
    }).join('')||'<tr><td colspan="6" class="empty">No compiler packages measured.</td></tr>';
    totals();
  }
  document.getElementById('packages').addEventListener('change',e=>{const tool=e.target.dataset.tool;if(!tool)return;if(e.target.checked)selected.add(tool);else selected.delete(tool);render();});
  document.getElementById('packages').addEventListener('click',e=>{const tool=e.target.dataset.detail;if(!tool)return;if(expanded.has(tool))expanded.delete(tool);else expanded.add(tool);render();document.querySelector('[data-detail="'+CSS.escape(tool)+'"]').focus();});
  document.getElementById('sort').addEventListener('change',render);
  document.getElementById('clear-tools').addEventListener('click',()=>{selected.clear();render();});
  document.getElementById('all-tools').addEventListener('click',()=>{for(const p of packages)selected.add(p.tool);render();});
  document.getElementById('measurement').textContent=(data.measurement||'')+' Units: KiB = 1,024 bytes; MiB = 1,048,576 bytes.';
  document.getElementById('execution-hosts').innerHTML='<h2>Wisp execution hosts, outside compiler packages</h2>'+((data.executionHosts||[]).map(host=>'<p><strong>'+escape(host.name)+'</strong>: '+bytes(host.bytes)+' · '+escape(host.scope)+'</p>').join('')||'<p>Set WISP_WASMTIME, WISP_RUST_WASMTIME and WISP_WASMTIME_PYTHON when generating the report to measure the configured hosts.</p>')+'<p>Each configured host is measured once. System libraries, Cargo, Python standard library and execution caches are outside these measurements.</p>';
  render();
})();
</script>
</body>
</html>'''
