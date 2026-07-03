#!/usr/bin/env python3
# scripts/clickaudit.py — run from repo root:  python3 scripts/clickaudit.py
# Static "act like the user" audit: routes vs clicks vs API vs Django URLConf.
# Adjust ROOT/APPS/BACKEND below to your layout. Add to CI to catch dead
# clicks and 404-bound API calls before they ship.
import os, re, json, sys
from collections import defaultdict

ROOT = "/home/claude/project"
APPS = ["src_frontend", "src_student", "src_teacher", "src_admin"]
BACKEND = os.path.join(ROOT, "backend/backend")

# ── helpers ──────────────────────────────────────────────────────────────
def files(app):
    for dp, _, fns in os.walk(os.path.join(ROOT, app, "src")):
        for fn in fns:
            if fn.endswith((".jsx", ".js")) and "blogs" not in dp:
                yield os.path.join(dp, fn)

def norm_dynamic(p):
    p = re.sub(r"\$\{[^}]*\}", ":dyn", p)         # ${var} → :dyn
    p = re.sub(r"//+", "/", p)
    return p

def route_to_regex(path):
    """React-Router path → regex. Handles :params, * splat, optional?."""
    p = path
    p = re.sub(r":[A-Za-z0-9_]+\?", r"[^/]*", p)
    p = re.sub(r":[A-Za-z0-9_]+", r"[^/]+", p)
    p = p.replace("*", ".*")
    if not p.startswith("/"): p = "/" + p
    p = p.rstrip("/") or "/"
    return re.compile("^" + p + "/?$")

# ── 1. collect routes per app ────────────────────────────────────────────
TAG_RE = re.compile(r"<Route\b([^>]*?)(/?)>|</Route\s*>")
PATHATTR_RE = re.compile(r"path\s*=\s*[\"'{]+([^\"'}]+)[\"'}]")

def collect_routes(app):
    """Nesting-aware: walks <Route> open/close tags, joining child paths."""
    routes = []
    for f in files(app):
        s = open(f, encoding="utf-8", errors="ignore").read()
        stack = []
        for m in TAG_RE.finditer(s):
            if m.group(0).startswith("</"):
                if stack: stack.pop()
                continue
            attrs, selfclose = m.group(1) or "", m.group(2) == "/"
            pm = PATHATTR_RE.search(attrs)
            seg = pm.group(1) if pm else ""
            base = stack[-1] if stack else ""
            if seg.startswith("/"):
                full = seg
            elif seg:
                full = (base.rstrip("/") + "/" + seg) if base else "/" + seg
            else:
                full = base
            if pm or " index" in attrs or attrs.strip().startswith("index"):
                routes.append((full or "/", os.path.relpath(f, ROOT)))
            if not selfclose:
                stack.append(full or base)
    return routes

# ── 2. collect clicks (navigate/Link/href) per app ───────────────────────
NAV_RES = [
    re.compile(r"navigate\(\s*[\"'`]([^\"'`\)]+)[\"'`]"),
    re.compile(r"\bto\s*=\s*[\"'`]([^\"'`]+)[\"'`]"),
    re.compile(r"\bto\s*=\s*\{\s*`([^`]+)`"),
    re.compile(r"navigate\(\s*`([^`]+)`"),
    re.compile(r"href\s*=\s*[\"'`](/[^\"'`]*)[\"'`]"),
    re.compile(r"window\.location(?:\.href)?\s*=\s*[\"'`]([^\"'`]+)[\"'`]"),
]
def collect_clicks(app):
    clicks = []
    for f in files(app):
        src = open(f, encoding="utf-8", errors="ignore").read()
        for rx in NAV_RES:
            for m in rx.finditer(src):
                tgt = m.group(1).strip()
                if tgt in ("#",) or tgt.startswith(("http", "mailto:", "tel:")):
                    clicks.append(("external", tgt, os.path.relpath(f, ROOT)))
                    continue
                if tgt in ("-1", "0") or tgt.startswith("?"):
                    continue
                clicks.append(("internal", norm_dynamic(tgt.split("?")[0].split("#")[0]), os.path.relpath(f, ROOT)))
    return clicks

# ── 3. backend URLConf flatten ───────────────────────────────────────────
PATH_RE = re.compile(r"(?:re_)?path\(\s*[\"']([^\"']*)[\"']")
INCLUDE_RE = re.compile(r"path\(\s*[\"']([^\"']*)[\"']\s*,\s*include\(\s*[\"']([^\"']+)[\"']")

def collect_backend():
    conf = open(os.path.join(BACKEND, "config/urls.py")).read()
    endpoints = []
    includes = INCLUDE_RE.findall(conf)
    # direct paths in config/urls.py (non-include)
    for m in PATH_RE.finditer(conf):
        pass  # covered via includes below; direct ones rare
    for prefix, module in includes:
        mod_path = os.path.join(BACKEND, module.replace(".", "/") + ".py")
        if not os.path.exists(mod_path):
            endpoints.append((prefix + "<MISSING MODULE " + module + ">", "config/urls.py"))
            continue
        sub = open(mod_path).read()
        for m in PATH_RE.finditer(sub):
            endpoints.append(("/" + prefix + m.group(1), module))
    return endpoints

def backend_regexes(endpoints):
    out = []
    for ep, mod in endpoints:
        p = re.sub(r"<[^>]+>", "[^/]+", ep)
        p = p.rstrip("/") or "/"
        out.append((re.compile("^" + re.escape("") + p + "/?$"), ep, mod))
    return out

# ── 4. frontend API calls ────────────────────────────────────────────────
API_RES = [
    re.compile(r"\bapi\.(?:get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\bapi\.(?:get|post|put|patch|delete)\(\s*`([^`]+)`"),
]
def collect_api_calls(app):
    calls = []
    for f in files(app):
        src = open(f, encoding="utf-8", errors="ignore").read()
        for rx in API_RES:
            for m in rx.finditer(src):
                p = norm_dynamic(m.group(1).split("?")[0])
                calls.append((p, os.path.relpath(f, ROOT)))
    return calls

def api_to_backend_path(p):
    """Frontend api client baseURL is /api — join."""
    if p.startswith("/api/"): return p[4:]
    if p.startswith("api/"): return "/" + p[4:]
    if not p.startswith("/"): p = "/" + p
    return p  # baseURL adds /api; backend prefixes exclude leading /api

# ── run ──────────────────────────────────────────────────────────────────
backend_eps = collect_backend()
b_rex = []
for ep, mod in backend_eps:
    canon = "/" + ep.lstrip("/")
    canon = re.sub(r"^/api/", "/", canon)
    canon = re.sub(r"<[^>]+>", ":dyn", canon)
    rx = re.sub(r":dyn", "[^/]+", canon)
    rx = rx.rstrip("/") or "/"
    b_rex.append((re.compile("^" + rx + "/?$"), canon, mod))

report = {}
for app in APPS:
    routes = collect_routes(app)
    clicks = collect_clicks(app)
    route_rx = [(route_to_regex(p), p, f) for p, f in routes if p not in ("*",)]

    dead_clicks, ok_clicks, external = [], set(), []
    for kind, tgt, f in clicks:
        if kind == "external":
            external.append((tgt, f)); continue
        if ":dyn" in tgt:
            probe = tgt.replace(":dyn", "x")
        else:
            probe = tgt
        if not probe.startswith("/"):
            # relative navigation — resolve ambiguous, count as dynamic-skip
            continue
        if any(rx.match(probe) for rx, _, _ in route_rx):
            ok_clicks.add(tgt)
        else:
            dead_clicks.append((tgt, f))

    unreached = []
    for rx, p, f in route_rx:
        if p in ("/", "*") or p.endswith("*"): continue
        probe_hits = False
        for tgt in ok_clicks:
            if rx.match(tgt.replace(":dyn", "x")):
                probe_hits = True; break
        if not probe_hits:
            unreached.append((p, f))

    api_calls = collect_api_calls(app)
    dead_api = []
    for p, f in api_calls:
        bp = api_to_backend_path(p)
        probe = bp.replace(":dyn", "x")
        matched = any(rx.match(probe) for rx, _, _ in b_rex)
        if not matched and probe.endswith("/x"):
            # A trailing ${var} is often a query-string suffix (e.g. `?q=...`)
            # invisible to static analysis — retry without the last segment.
            matched = any(rx.match(probe[: -2].rstrip("/") + "/") or rx.match(probe[: -2].rstrip("/")) for rx, _, _ in b_rex)
        if not matched:
            dead_api.append((p, f))

    report[app] = {
        "routes": len(routes),
        "clicks_checked": len([c for c in clicks if c[0] == "internal"]),
        "dead_clicks": sorted(set(dead_clicks)),
        "unreachable_routes": sorted(set(unreached)),
        "api_calls": len(api_calls),
        "dead_api_calls": sorted(set(dead_api)),
        "external_links": sorted(set(external)),
    }

print(json.dumps(report, indent=1)[:12000])
print("\nBACKEND endpoints parsed:", len(backend_eps))
missing_mods = [e for e, m in backend_eps if "MISSING MODULE" in e]
if missing_mods: print("MISSING URL MODULES:", missing_mods)
