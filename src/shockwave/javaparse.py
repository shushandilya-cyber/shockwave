"""Dependency-free Java outline parser.

Produces class / method / field declarations with line ranges so diff hunks can be
mapped to the enclosing symbol. Heuristic by design (spec: "false positives are
acceptable; Story 3 validates against the real code graph") but handles nested
classes, generics, annotations with args, throws clauses, anonymous classes,
lambdas, text blocks, and interface/abstract methods.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

_CLASS_RE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
_ANNOT_DECL_RE = re.compile(r"@interface\s+([A-Za-z_$][\w$]*)")
_PKG_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IDENT_BEFORE = re.compile(r"([A-Za-z_$][\w$]*)\s*$")
_NOT_METHODS = {"if", "for", "while", "switch", "catch", "synchronized", "try", "else", "do", "new", "return",
                "throw", "super", "this", "finally", "case", "default", "assert"}
_HTTP_ANN = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
_SPRING_MAP = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}


@dataclass
class Decl:
    kind: str                 # class | method | field
    cls: str                  # Outer.Inner
    name: str                 # member name (== simple class name for kind=class)
    start: int                # 1-based inclusive
    end: int
    params: str = ""
    annotations: str = ""     # raw (unsanitized) header text, used for endpoint extraction
    children: list = field(default_factory=list)


def sanitize(src: str) -> str:
    """Blank out comments and string/char/text-block literals, preserving offsets & newlines."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j); i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j); i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            blank(i + 1, j - 1); i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            blank(i + 1, j - 1); i = j
        else:
            i += 1
    return "".join(out)


def _method_sig(header: str):
    """Return (name, params) if header (sanitized, stripped) ends like a method declaration."""
    h = re.sub(r"\bthrows\s+[\w.$<>,\s]+$", "", header.strip()).rstrip()
    if not h.endswith(")"):
        return None
    depth, k = 0, len(h) - 1
    while k >= 0:
        if h[k] == ")":
            depth += 1
        elif h[k] == "(":
            depth -= 1
            if depth == 0:
                break
        k -= 1
    if k < 0:
        return None
    before = h[:k]
    m = _IDENT_BEFORE.search(before)
    if not m:
        return None
    name = m.group(1)
    if name in _NOT_METHODS or "=" in before or "->" in before:
        return None
    # must look like a declaration: something precedes the name (type/modifier) or it's a constructor
    return name, re.sub(r"\s+", " ", h[k + 1:-1]).strip()


def _simplify_params(params: str) -> str:
    """'@PathParam( ) String id, final List<X> xs' -> 'String,List<X>'"""
    if not params:
        return ""
    params = re.sub(r"@[\w.]+(\s*\([^)]*\))?", " ", params)
    depth, cur, parts = 0, "", []
    for ch in params:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    parts.append(cur)
    types = []
    for p in parts:
        toks = [t for t in p.replace("final ", " ").split() if t]
        if len(toks) >= 2:
            types.append(" ".join(toks[:-1]).replace(" ", ""))
        elif toks:
            types.append(toks[0])
    return ",".join(types)


def outline(src: str) -> tuple[str, list[Decl]]:
    """Return (package, flat list of declarations)."""
    s = sanitize(src)
    pkg_m = _PKG_RE.search(s)
    pkg = pkg_m.group(1) if pkg_m else ""
    line_starts = [0] + [i + 1 for i, ch in enumerate(s) if ch == "\n"]

    def line_of(off: int) -> int:
        return bisect.bisect_right(line_starts, off)

    decls: list[Decl] = []
    # stack entries: (kind, Decl|None) ; kind in {'class','method','block'}
    stack: list[tuple[str, Decl | None]] = []
    seg_start = 0
    paren = 0

    def cls_path() -> str:
        return ".".join(d.name for k, d in stack if k == "class" and d)

    def in_class_body() -> bool:
        return bool(stack) and stack[-1][0] == "class"

    def header_start(a: int, b: int) -> int:
        k = a
        while k < b and s[k].isspace():
            k += 1
        return k

    for i, ch in enumerate(s):
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        if paren:
            continue
        if ch == "{":
            header = s[seg_start:i]
            hs = header_start(seg_start, i)
            m_cls = _ANNOT_DECL_RE.search(header) or _CLASS_RE.search(header)
            is_top_or_class = not stack or stack[-1][0] == "class"
            if m_cls and is_top_or_class and "=" not in header.split(m_cls.group(0))[0] and "new " not in header:
                name = m_cls.group(m_cls.lastindex)
                d = Decl("class", cls_path(), name, line_of(hs), 0, annotations=src[seg_start:i])
                decls.append(d); stack.append(("class", d))
            elif in_class_body() and (sig := _method_sig(header)):
                d = Decl("method", cls_path(), sig[0], line_of(hs), 0, params=_simplify_params(sig[1]), annotations=src[seg_start:i])
                decls.append(d); stack.append(("method", d))
            else:
                stack.append(("block", None))
            seg_start = i + 1
        elif ch == "}":
            if stack:
                k, d = stack.pop()
                if d:
                    d.end = line_of(i)
            seg_start = i + 1
        elif ch == ";":
            if in_class_body():
                header = s[seg_start:i]
                if header.strip() and not re.match(r"\s*(import|package)\b", header):
                    hs = header_start(seg_start, i)
                    sig = _method_sig(header)
                    if sig and "=" not in header:
                        decls.append(Decl("method", cls_path(), sig[0], line_of(hs), line_of(i), params=_simplify_params(sig[1]), annotations=src[seg_start:i]))
                    else:
                        lhs = header.split("=", 1)[0]
                        m = _IDENT_BEFORE.search(lhs.rstrip())
                        if m and m.group(1) not in _NOT_METHODS and len(lhs.split()) >= 2:
                            decls.append(Decl("field", cls_path(), m.group(1), line_of(hs), line_of(i)))
            seg_start = i + 1
    for d in decls:
        if d.end == 0:
            d.end = len(line_starts)
    return pkg, decls


def class_fqn(pkg: str, cls_path: str, name: str) -> str:
    inner = f"{cls_path}.{name}" if cls_path else name
    return f"{pkg}.{inner}" if pkg else inner


def endpoints_for(decl: Decl, class_decl: Decl | None) -> list[dict]:
    """Extract (HTTP method, path) from JAX-RS / Spring annotations on a method."""
    def ann_path(text: str, names: tuple[str, ...]) -> str | None:
        for nm in names:
            m = re.search(r"@" + nm + r"\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*\"([^\"]*)\"", text)
            if m:
                return m.group(1)
        return None

    text = decl.annotations or ""
    methods = [a for a in _HTTP_ANN if re.search(r"@" + a + r"\b", text)]
    for sp, hm in _SPRING_MAP.items():
        if re.search(r"@" + sp + r"\b", text):
            methods.append(hm)
    rm = re.search(r"RequestMethod\.(\w+)", text)
    if rm:
        methods.append(rm.group(1))
    if not methods and "@RequestMapping" not in text:
        return []
    base = ""
    if class_decl:
        base = ann_path(class_decl.annotations or "", ("Path", "RequestMapping")) or ""
    sub = ann_path(text, ("Path", "RequestMapping", *_SPRING_MAP.keys())) or ""
    path = ("/" + "/".join(p.strip("/") for p in (base, sub) if p.strip("/"))) or "/"
    return [{"method": m, "path": path, "operationId": decl.name} for m in sorted(set(methods or ["ANY"]))]

