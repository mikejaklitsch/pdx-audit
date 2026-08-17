"""Interactive HTML report for --display.

render_report turns the audits' findings into a self-contained page: files that
have issues, each expandable to the exact findings, and for a changed override
a three-way "Your block" view built from finding.data (the same reconciliation
the terminal audit uses). Colour encodes action, not diff direction: green is
"add this", amber is "review" (including a line vanilla dropped that you still
carry, which is usually intentional), and red is reserved for the severity of
the finding itself. No external assets; theme-aware."""
import html
import json

from .report import KIND, finding_severity, SEV_INFO


def render_report(findings, mod_name, old_msg, new_msg):
    """Return the report as one self-contained HTML string."""
    records = []
    for f in findings:
        sev, audit, klass, _fix = KIND[f.kind]
        if sev == SEV_INFO:
            continue
        loc = f.location or ""
        has_line = ":" in loc and loc.rsplit(":", 1)[1].isdigit()
        rec = {"name": f.name, "audit": audit, "sev": sev, "klass": klass,
               "file": loc.rsplit(":", 1)[0] if has_line else (loc or "?"),
               "line": loc.rsplit(":", 1)[1] if has_line else "",
               "detail": f.detail or ""}
        if f.data:
            rec.update(f.data)
        records.append(rec)
    data = {"mod": mod_name, "old": old_msg or "", "new": new_msg or "",
            "records": records}
    return (PAGE.replace("@@DATA@@", json.dumps(data))
                .replace("@@MOD@@", html.escape(mod_name))
                .replace("@@TITLE@@", html.escape(f"{old_msg} → {new_msg}")))


PAGE = r"""<title>pdx-audit report: @@MOD@@</title>
<style>
  :root{
    --bg:#f4f6fa; --panel:#ffffff; --edge:#e2e6ef; --edge2:#eef1f7;
    --ink:#1c2230; --muted:#68708a; --faint:#9aa2b8; --accent:#4f46e5;
    --broken:#c0392b; --stale:#c0392b; --review:#b7791f; --info:#3d8b5f;
    --add-bg:#e5f6ea; --add-ink:#1c7a43; --del-bg:#fdeaea; --del-ink:#b23434;
    --mine-bg:#eceafe; --mine-ink:#4f46e5; --track-bg:#dcf3ef; --track-ink:#0f766e;
    --rev-bg:#fbf1dd; --rev-ink:#9a6b12; --hunk:#5b63a8; --code:#f7f8fb;
  }
  @media (prefers-color-scheme:dark){
    :root{
      --bg:#0e1017; --panel:#161923; --edge:#262b3a; --edge2:#1e222e;
      --ink:#e5e8f0; --muted:#98a0b8; --faint:#6b7290; --accent:#8b8cf7;
      --broken:#f0776a; --stale:#f0776a; --review:#e0b15a; --info:#6cc292;
      --add-bg:#12331f; --add-ink:#77d69a; --del-bg:#3a1a1a; --del-ink:#f0918a;
      --mine-bg:#211d44; --mine-ink:#a5a0f5; --track-bg:#103330; --track-ink:#5fd0bf;
      --rev-bg:#33280f; --rev-ink:#e0b15a; --hunk:#9aa0e0; --code:#11141c;
    }
  }
  :root[data-theme=light]{
    --bg:#f4f6fa; --panel:#ffffff; --edge:#e2e6ef; --edge2:#eef1f7;
    --ink:#1c2230; --muted:#68708a; --faint:#9aa2b8; --accent:#4f46e5;
    --broken:#c0392b; --stale:#c0392b; --review:#b7791f; --info:#3d8b5f;
    --add-bg:#e5f6ea; --add-ink:#1c7a43; --del-bg:#fdeaea; --del-ink:#b23434;
    --mine-bg:#eceafe; --mine-ink:#4f46e5; --track-bg:#dcf3ef; --track-ink:#0f766e;
    --rev-bg:#fbf1dd; --rev-ink:#9a6b12; --hunk:#5b63a8; --code:#f7f8fb;
  }
  :root[data-theme=dark]{
    --bg:#0e1017; --panel:#161923; --edge:#262b3a; --edge2:#1e222e;
    --ink:#e5e8f0; --muted:#98a0b8; --faint:#6b7290; --accent:#8b8cf7;
    --broken:#f0776a; --stale:#f0776a; --review:#e0b15a; --info:#6cc292;
    --add-bg:#12331f; --add-ink:#77d69a; --del-bg:#3a1a1a; --del-ink:#f0918a;
    --mine-bg:#211d44; --mine-ink:#a5a0f5; --track-bg:#103330; --track-ink:#5fd0bf;
    --rev-bg:#33280f; --rev-ink:#e0b15a; --hunk:#9aa0e0; --code:#11141c;
  }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--ink); line-height:1.5;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }
  .mono{ font-family:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace; }
  .wrap{ max-width:960px; margin:0 auto; padding:clamp(16px,3vw,40px); }
  h1{ font-size:19px; margin:0 0 3px; letter-spacing:-.01em; }
  h1 .v{ color:var(--muted); font-weight:500; }
  .sub{ color:var(--muted); font-size:13px; margin:0 0 14px; }
  .pills{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .pill{ display:inline-flex; align-items:center; gap:6px; font-size:12.5px; font-weight:600;
    padding:4px 11px; border-radius:999px; border:1px solid var(--edge); background:var(--panel); }
  .pill .dot{ width:8px; height:8px; border-radius:50%; }
  .d-broken,.d-stale{ background:var(--stale); } .d-review{ background:var(--review); }
  .spacer{ flex:1; }
  .files{ display:flex; flex-direction:column; gap:10px; margin-top:20px; }
  details.file{ background:var(--panel); border:1px solid var(--edge); border-radius:12px; overflow:hidden; }
  details.file>summary{ list-style:none; cursor:pointer; padding:13px 16px;
    display:flex; align-items:center; gap:12px; }
  details.file>summary::-webkit-details-marker{ display:none; }
  .chev{ color:var(--faint); transition:transform .15s; font-size:12px; }
  details.file[open] .chev{ transform:rotate(90deg); }
  .fpath{ font-size:13px; font-weight:600; word-break:break-all; }
  .fpath .dir{ color:var(--muted); font-weight:500; }
  .fmeta{ margin-left:auto; display:flex; gap:5px; align-items:center; flex-shrink:0; }
  .fcount{ color:var(--muted); font-size:12px; margin-left:6px; }
  .mini{ width:7px; height:7px; border-radius:50%; }
  .findings{ border-top:1px solid var(--edge2); padding:6px 16px 14px; }
  .find{ padding:14px 0; border-bottom:1px solid var(--edge2); }
  .find:last-child{ border-bottom:none; }
  .frow{ display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .fname{ font-size:14px; font-weight:650; }
  .badge{ font-size:10.5px; font-weight:700; letter-spacing:.04em; padding:2px 7px;
    border-radius:5px; border:1px solid var(--edge); color:var(--muted); text-transform:uppercase; }
  .sev{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:5px; color:#fff; }
  .s-broken,.s-stale{ background:var(--stale); } .s-review{ background:var(--review); }
  .loc{ margin-left:auto; color:var(--faint); font-size:12px; }
  .reason{ color:var(--muted); font-size:12.5px; margin:5px 0 0; }
  .toggle{ display:inline-flex; margin:11px 0 0; border:1px solid var(--edge);
    border-radius:8px; overflow:hidden; background:var(--bg); }
  .toggle button{ appearance:none; border:0; background:transparent; color:var(--muted);
    font:inherit; font-size:12px; font-weight:600; padding:5px 12px; cursor:pointer;
    border-right:1px solid var(--edge); }
  .toggle button:last-child{ border-right:0; }
  .toggle button[aria-pressed=true]{ background:var(--accent); color:#fff; }
  .toggle button:disabled{ opacity:.4; cursor:not-allowed; }
  .pane{ margin-top:10px; border:1px solid var(--edge); border-radius:8px;
    background:var(--code); overflow:auto; }
  pre.diff{ margin:0; padding:10px 12px; font-size:12px; line-height:1.55;
    font-family:ui-monospace,Menlo,Consolas,monospace; white-space:pre; }
  pre.diff .a{ background:var(--add-bg); color:var(--add-ink); display:block; }
  pre.diff .d{ background:var(--del-bg); color:var(--del-ink); display:block; }
  pre.diff .rm{ background:var(--rev-bg); color:var(--rev-ink); display:block;
    text-decoration:line-through; }
  pre.diff .mine{ background:var(--mine-bg); color:var(--mine-ink); display:block; }
  pre.diff .track{ background:var(--track-bg); color:var(--track-ink); display:block; }
  pre.diff .h{ color:var(--hunk); display:block; }
  .gap{ padding:11px 13px; font-size:12.5px; }
  .gap h4{ margin:0 0 6px; font-size:11px; letter-spacing:.05em; text-transform:uppercase;
    color:var(--muted); font-weight:700; }
  .gap ul{ margin:0 0 12px; padding:0; list-style:none; }
  .gap li{ padding:2px 0; } .gap li code{ background:var(--code); }
  .gap .none{ color:var(--faint); }
  .gap .ov{ margin-bottom:12px; }
  .gap .ovk{ font-weight:700; color:var(--ink); display:inline-block; margin-bottom:5px; }
  .miss code{ color:var(--add-ink); } .keep code{ color:var(--rev-ink); }
  .legend{ display:flex; flex-wrap:wrap; gap:14px; font-size:11px; padding:9px 12px 0; }
  .legend span{ background:none; }
  .legend .a{ color:var(--add-ink); } .legend .rm{ color:var(--rev-ink); }
  .legend .mine{ color:var(--mine-ink); } .legend .track{ color:var(--track-ink); }
  .absent{ margin:0 0 4px; padding:10px 13px; border:1px solid var(--review); border-radius:8px; font-size:12.5px; }
  .absent b{ color:var(--review); } .absent ul{ margin:6px 0 0; padding:0; list-style:none; }
  .absent li{ padding:2px 0; } .absent .hint{ color:var(--muted); font-style:italic; }
  .absent pre.vanilla{ margin:6px 0 0; border:1px solid var(--edge); border-radius:6px;
    background:var(--code); color:var(--ink); }
  .rmnote{ margin:10px 0 2px; padding:10px 13px; border:1px solid var(--edge);
    border-radius:8px; font-size:12.5px; color:var(--muted); }
  .rmnote ul{ margin:6px 0 0; padding:0; list-style:none; }
  .rmnote li{ padding:2px 0; } .rmnote code{ color:var(--rev-ink); }
  .themebtn{ appearance:none; border:1px solid var(--edge); background:var(--panel);
    color:var(--muted); border-radius:8px; padding:5px 11px; font:inherit; font-size:12px;
    font-weight:600; cursor:pointer; }
  .clean{ margin-top:20px; padding:16px; border:1px solid var(--edge); border-radius:12px;
    background:var(--panel); color:var(--info); font-weight:600; }
</style>
<div class="wrap">
  <header>
    <h1>@@MOD@@ <span class="v">audit report</span></h1>
    <p class="sub mono">@@TITLE@@</p>
    <div class="pills" id="pills"></div>
  </header>
  <div class="files" id="files"></div>
</div>
<script id="data" type="application/json">@@DATA@@</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const SEV = ['broken','stale','review'];
const SEVLABEL = {broken:'broken',stale:'stale',review:'review'};
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function diffHtml(txt){
  if(!txt) return '<pre class="diff">(no diff)</pre>';
  const rows = txt.split('\n').map(l=>{
    const c = l[0]==='+'&&l[1]!=='+' ? 'a' : l[0]==='-'&&l[1]!=='-' ? 'd'
            : l.startsWith('@@') ? 'h' : '';
    return c ? '<span class="'+c+'">'+esc(l)+'</span>' : esc(l)+'\n';
  });
  return '<pre class="diff">'+rows.join('')+'</pre>';
}
function blockHtml(r){
  if(!r.patch || !r.patch.length) return '<pre class="diff">(your block was not found)</pre>';
  var h='';
  if(r.absent && r.absent.length){
    h+='<div class="absent"><b>Vanilla changed a block your copy omits.</b> Your REPLACE does not define these, so decide whether the change applies to how you do it:<ul>'+
       r.absent.map(function(a){
         var s='<li><code>'+esc(a.line)+'</code>';
         if(a.related) s+=' <span class="hint">your block defines <code>'+esc(a.related)+'</code> (similar name); is that your equivalent?</span>';
         if(a.vanilla) s+='<pre class="diff vanilla">'+esc(a.vanilla)+'</pre>';
         return s+'</li>';
       }).join('')+'</ul></div>';
  }
  h+='<div class="legend"><span class="a">+ missing (vanilla has it)</span>'+
     '<span class="track">✓ you match vanilla’s change</span>'+
     '<span class="mine">• your own change</span>'+
     '<span class="rm">- vanilla removed, still yours (review)</span></div>';
  var rows=r.patch.map(function(p){
    var t=esc(p.t);
    if(p.c==='add') return '<span class="a">+ '+t+'</span>';
    if(p.c==='removed') return '<span class="rm">- '+t+'</span>';
    if(p.c==='tracks') return '<span class="track">✓ '+t+'</span>';
    if(p.c==='mine') return '<span class="mine">• '+t+'</span>';
    return '  '+t+'\n';
  });
  h+='<pre class="diff">'+rows.join('')+'</pre>';
  if(r.removed_note && r.removed_note.length){
    h+='<div class="rmnote"><b>Vanilla also removed these, and your block never had them</b> (reference, nothing to change):<ul>'+
       r.removed_note.map(function(x){return '<li><code>'+esc(x)+'</code></li>';}).join('')+'</ul></div>';
  }
  return h;
}
function gapHtml(r){
  if(r.type==='INJECT'){
    if(r.overlap && r.overlap.length) return '<div class="gap"><h4>Vanilla also changed these top-level keys you inject</h4>'+
      r.overlap.map(function(o){return '<div class="ov"><code class="ovk">'+esc(o.key)+'</code><pre class="diff">'+
        (o.old?'<span class="d">'+esc(o.old)+'</span>':'')+(o.new?'<span class="a">'+esc(o.new)+'</span>':'')+'</pre></div>';}).join('')+'</div>';
    return '<div class="gap"><span class="none">Injection point untouched: vanilla changed other parts of this block, not the keys this INJECT adds.</span></div>';
  }
  var h='<div class="gap"><div class="miss"><h4>Missing from your copy (vanilla added)</h4><ul>'+
    ((r.missing&&r.missing.length)? r.missing.map(function(l){return '<li><code>'+esc(l)+'</code></li>';}).join('') : '<li class="none">none</li>')+'</ul></div>';
  h+='<div class="keep"><h4>Carried but vanilla removed</h4><ul>'+
    ((r.kept&&r.kept.length)? r.kept.map(function(l){return '<li><code>'+esc(l)+'</code></li>';}).join('') : '<li class="none">none</li>')+'</ul></div>';
  return h+'</div>';
}

function findingEl(r){
  const el = document.createElement('div'); el.className='find';
  const rich = r.patch && r.patch.length;
  const stat = (r.n_add||r.n_rem) ? '<span class="loc mono">+'+r.n_add+' −'+r.n_rem+(r.line?' · :'+r.line:'')+'</span>'
             : (r.line ? '<span class="loc mono">:'+r.line+'</span>' : '');
  const badge = r.type || r.audit;
  el.innerHTML =
    '<div class="frow"><span class="fname mono">'+esc(r.name)+'</span>'+
    '<span class="badge">'+esc(badge)+'</span>'+
    '<span class="sev s-'+r.sev+'">'+SEVLABEL[r.sev]+'</span>'+stat+'</div>'+
    '<p class="reason">'+esc(r.klass)+(r.detail?' ('+esc(r.detail)+')':'')+'</p>';
  if(rich){
    el.innerHTML +=
      '<div class="toggle"><button data-m="diff" aria-pressed="true">Vanilla’s change</button>'+
      '<button data-m="gap">Your gap</button>'+
      '<button data-m="mine">Your block</button></div><div class="pane"></div>';
    const pane = el.querySelector('.pane');
    const render = m => pane.innerHTML = m==='mine' ? blockHtml(r) : m==='gap' ? gapHtml(r) : diffHtml(r.diff);
    render('diff');
    el.querySelectorAll('.toggle button').forEach(b=>{
      b.onclick = ()=>{ el.querySelectorAll('.toggle button').forEach(x=>x.setAttribute('aria-pressed', x===b?'true':'false')); render(b.dataset.m); };
    });
  }
  return el;
}

const worst = rs => SEV.find(s=>rs.some(r=>r.sev===s));
const byFile = {};
DATA.records.forEach(r=>(byFile[r.file]=byFile[r.file]||[]).push(r));
const fileList = Object.entries(byFile).sort((a,b)=>
  SEV.indexOf(worst(a[1]))-SEV.indexOf(worst(b[1])) || b[1].length-a[1].length);

const counts = {};
DATA.records.forEach(r=>counts[r.sev]=(counts[r.sev]||0)+1);
document.getElementById('pills').innerHTML =
  SEV.filter(s=>counts[s]).map(s=>'<span class="pill"><span class="dot d-'+s+'"></span>'+counts[s]+' '+s+'</span>').join('')+
  '<span class="pill"><span class="dot" style="background:var(--faint)"></span>'+fileList.length+' files</span>'+
  '<span class="spacer"></span><button class="themebtn" id="theme">Toggle theme</button>';

const host = document.getElementById('files');
if(!fileList.length){
  host.innerHTML = '<div class="clean">No action needed: everything the mod overrides is current with vanilla.</div>';
}
fileList.forEach(([file, rs])=>{
  rs.sort((a,b)=>SEV.indexOf(a.sev)-SEV.indexOf(b.sev));
  const d = document.createElement('details'); d.className='file';
  const slash = file.lastIndexOf('/');
  const dir = slash>=0 ? file.slice(0,slash+1) : '';
  const base = slash>=0 ? file.slice(slash+1) : file;
  const dots = SEV.filter(s=>rs.some(r=>r.sev===s)).map(s=>'<span class="mini d-'+s+'"></span>').join('');
  const sm = document.createElement('summary');
  sm.innerHTML = '<span class="chev">▶</span>'+
    '<span class="fpath mono"><span class="dir">'+esc(dir)+'</span>'+esc(base)+'</span>'+
    '<span class="fmeta">'+dots+'<span class="fcount">'+rs.length+'</span></span>';
  d.appendChild(sm);
  const box = document.createElement('div'); box.className='findings';
  rs.forEach(r=>box.appendChild(findingEl(r)));
  d.appendChild(box);
  host.appendChild(d);
});

document.getElementById('theme').onclick = ()=>{
  const cur = document.documentElement.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  document.documentElement.setAttribute('data-theme', cur==='dark'?'light':'dark');
};
</script>
"""
