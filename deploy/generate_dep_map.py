#!/usr/bin/env python3
"""Generate an interactive HTML dependency map of the repo's Python files.

Parses each .py file's AST to collect:
  - top-level function/method signatures (name + args)
  - import edges to other repo modules

Outputs a self-contained HTML file (vis-network via CDN) where hovering
a node shows its functions/args, and edges show the import relationship.
"""
import ast
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, ROOT)


def module_name_for(relpath):
    # turns modules/utils/config.py -> modules.utils.config
    no_ext = relpath[:-3]
    return no_ext.replace(os.sep, ".")


def format_args(args: ast.arguments):
    parts = []
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(args.args, defaults):
        if d is None:
            parts.append(a.arg)
        else:
            try:
                parts.append(f"{a.arg}={ast.unparse(d)}")
            except Exception:
                parts.append(f"{a.arg}=...")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if d is None:
            parts.append(f"{a.arg}")
        else:
            try:
                parts.append(f"{a.arg}={ast.unparse(d)}")
            except Exception:
                parts.append(f"{a.arg}=...")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return parts


def extract_functions(tree):
    funcs = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.class_stack = []

        def visit_ClassDef(self, node):
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node):
            self._record(node)
            # don't descend into nested functions for top-level clarity
            for child in node.body:
                if isinstance(child, (ast.ClassDef,)):
                    self.visit(child)

        visit_AsyncFunctionDef = visit_FunctionDef

        def _record(self, node):
            prefix = ".".join(self.class_stack) + "." if self.class_stack else ""
            args = format_args(node.args)
            funcs.append({"name": prefix + node.name, "args": args, "line": node.lineno})

    Visitor().visit(tree)
    return funcs


def extract_calls(tree):
    """Collect distinct function-call names used in the file (for rollover info)."""
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            try:
                if isinstance(f, ast.Name):
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.add(f.attr)
            except Exception:
                pass
    return sorted(calls)


def extract_imports(tree, modules_index):
    edges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules_index:
                    edges.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module
                if mod in modules_index:
                    edges.append(mod)
                else:
                    # from modules import utils  (relative-ish package import)
                    for alias in node.names:
                        candidate = f"{mod}.{alias.name}"
                        if candidate in modules_index:
                            edges.append(candidate)
    return sorted(set(edges))


def main():
    files = sorted(iter_py_files())
    modules_index = {module_name_for(f): f for f in files}

    nodes = []
    links = []
    file_info = {}

    for relpath in files:
        full = os.path.join(ROOT, relpath)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=relpath)
        except Exception as e:
            file_info[relpath] = {"functions": [], "calls": [], "error": str(e)}
            continue

        funcs = extract_functions(tree)
        calls = extract_calls(tree)
        imports = extract_imports(tree, modules_index)
        file_info[relpath] = {"functions": funcs, "calls": calls}

        for imp_mod in imports:
            target = modules_index[imp_mod]
            if target != relpath:
                links.append({"source": relpath, "target": target})

    # categorize for coloring
    def group_for(relpath):
        if relpath.startswith("modules/utils/"):
            return "utils"
        if relpath.startswith("modules/"):
            return "module"
        if relpath.startswith("scripts/"):
            return "script"
        if relpath.startswith("deploy/"):
            return "deploy"
        return "entry"

    for relpath in files:
        nodes.append({"id": relpath, "group": group_for(relpath)})

    data = {"nodes": nodes, "links": links, "info": file_info}

    html = TEMPLATE.replace("__DATA__", json.dumps(data))
    out_path = os.path.join(ROOT, "deploy", "dependency_map.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {out_path}")


TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>s3reporting dependency map</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  html, body { margin:0; padding:0; height:100%; font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1117; color:#e8e8e8; }
  #app { display:flex; height:100vh; }
  #graph { flex: 1 1 auto; }
  #panel { width: 380px; border-left: 1px solid #2a2d36; padding: 16px; overflow-y:auto; background:#14161d; }
  #panel h2 { font-size: 14px; margin: 0 0 8px; word-break: break-all; color:#7dd3fc; }
  #panel .group { display:inline-block; font-size:11px; padding:2px 6px; border-radius:4px; margin-bottom:10px; }
  .g-entry { background:#3b2; }
  .g-module { background:#357; }
  .g-utils { background:#a63; }
  .g-script { background:#636; }
  .g-deploy { background:#555; }
  #panel h3 { font-size:12px; text-transform:uppercase; color:#888; margin:14px 0 6px; letter-spacing:.05em; }
  #panel ul { list-style:none; margin:0; padding:0; font-size:12.5px; }
  #panel li { padding:3px 0; border-bottom:1px solid #1f2128; }
  .fn-name { color:#fbbf24; }
  .fn-args { color:#9ca3af; }
  .calls { color:#86efac; }
  #legend { position:absolute; top:10px; left:10px; background:#14161dcc; padding:8px 12px; border-radius:6px; font-size:12px; }
  #legend span { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }
  #hint { color:#888; font-size:12.5px; }
  #search { width:100%; box-sizing:border-box; padding:6px 8px; margin-bottom:10px; background:#1f2128; border:1px solid #2a2d36; color:#eee; border-radius:4px; }
</style>
</head>
<body>
<div id="app">
  <div id="graph">
    <div id="legend">
      <div><span style="background:#3b2"></span>entry script</div>
      <div><span style="background:#357"></span>modules/</div>
      <div><span style="background:#a63"></span>modules/utils</div>
      <div><span style="background:#636"></span>scripts/</div>
      <div><span style="background:#555"></span>deploy/</div>
    </div>
  </div>
  <div id="panel">
    <input id="search" placeholder="Filter files...">
    <div id="detail"><p id="hint">Hover or click a node to inspect its functions, arguments, and calls. Click an edge to see the import relationship.</p></div>
  </div>
</div>
<script>
const DATA = __DATA__;

const groupColor = {entry:"#2ecc71", module:"#3b82f6", utils:"#d97742", script:"#a855f7", deploy:"#6b7280"};

const nodes = DATA.nodes.map(n => ({
  id: n.id,
  label: n.id.split("/").pop(),
  title: n.id,
  group: n.group,
  color: groupColor[n.group],
  font: {color:"#eee", size:12}
}));

const edges = DATA.links.map((l,i) => ({
  id: i, from: l.source, to: l.target, arrows: "to",
  color: {color:"#444", highlight:"#7dd3fc"}, smooth:{type:"dynamic"}
}));

const container = document.getElementById("graph");
const network = new vis.Network(container, {nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges)}, {
  nodes: { shape: "dot", size: 10 },
  edges: { width: 1 },
  physics: { stabilization: true, barnesHut: { gravitationalConstant: -3000, springLength: 120 } },
  interaction: { hover: true, tooltipDelay: 80 }
});

function renderDetail(fileId) {
  const info = DATA.info[fileId] || {functions: [], calls: []};
  const incoming = DATA.links.filter(l => l.target === fileId).map(l => l.source);
  const outgoing = DATA.links.filter(l => l.source === fileId).map(l => l.target);
  let html = `<h2>${fileId}</h2>`;
  const grp = nodes.find(n => n.id === fileId).group;
  html += `<span class="group g-${grp}">${grp}</span>`;

  html += `<h3>Functions (${info.functions.length})</h3><ul>`;
  if (info.functions.length === 0) html += `<li style="color:#666">none found</li>`;
  for (const f of info.functions) {
    html += `<li><span class="fn-name">${f.name}</span>(<span class="fn-args">${f.args.join(", ")}</span>) <span style="color:#555">L${f.line}</span></li>`;
  }
  html += `</ul>`;

  html += `<h3>Calls referenced (${info.calls.length})</h3><ul><li class="calls">${info.calls.join(", ") || "none"}</li></ul>`;

  html += `<h3>Imported by (${incoming.length})</h3><ul>`;
  for (const f of incoming) html += `<li>${f}</li>`;
  html += `</ul>`;

  html += `<h3>Imports (${outgoing.length})</h3><ul>`;
  for (const f of outgoing) html += `<li>${f}</li>`;
  html += `</ul>`;

  document.getElementById("detail").innerHTML = html;
}

network.on("hoverNode", params => renderDetail(params.node));
network.on("click", params => { if (params.nodes.length) renderDetail(params.nodes[0]); });

document.getElementById("search").addEventListener("input", e => {
  const q = e.target.value.toLowerCase();
  const matchIds = nodes.filter(n => n.id.toLowerCase().includes(q)).map(n => n.id);
  network.setSelection({nodes: q ? matchIds : [], edges: []});
  if (q && matchIds.length) network.focus(matchIds[0], {scale: 1.2, animation: true});
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
