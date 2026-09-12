"""Lightweight markdown viewer for an SLRHarness workspace.

Serves a single workspace over HTTP with server-side markdown rendering.
Renders GFM tables, task lists, footnotes, and strikethrough. Resolves
relative `.md` links and Obsidian-style `[[wikilinks]]` so clicks stay
inside the viewer. Non-markdown assets (images, PDFs) are served as
static files.

Usage:
    uv run --group viewer python tools/viewer.py \\
        --workspace workspaces/sop-executing-agent-use-cases

Then open http://localhost:8765 in your browser. Middle-click or
Cmd/Ctrl-click any link to open in a new tab.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin
except ImportError:
    sys.stderr.write(
        "Missing viewer dependencies. Install with:\n    uv sync --group viewer\n"
    )
    raise SystemExit(1) from None


# --- Markdown rendering ------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]")


def _build_wikilink_index(root: Path) -> dict[str, Path]:
    """Map basename (without .md) -> file path for all markdown in workspace."""
    index: dict[str, Path] = {}
    for md in root.rglob("*.md"):
        key = md.stem.lower()
        # Prefer shallower matches when duplicates exist.
        if key not in index or len(md.parts) < len(index[key].parts):
            index[key] = md
    return index


def _expand_wikilinks(
    text: str, current: Path, root: Path, index: dict[str, Path]
) -> str:
    """Rewrite `[[name]]` / `[[name|label]]` to standard markdown links."""

    def repl(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        label = (m.group(2) or target).strip()
        key = target.lower()
        if key in index:
            rel = index[key].relative_to(root).as_posix()
            return f"[{label}](/view/{rel})"
        # Unresolved — render as dim text.
        return f'<span class="wikilink-missing">{html.escape(label)}</span>'

    return _WIKILINK_RE.sub(repl, text)


def _make_renderer() -> MarkdownIt:
    md = (
        MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
        .enable(["table", "strikethrough"])
        .use(footnote_plugin)
        .use(tasklists_plugin, enabled=False)
    )
    return md


def _rewrite_links(html_text: str, current: Path, root: Path) -> str:
    """Rewrite relative links in rendered HTML.

    - `.md` links → `/view/<path>` and open in a new tab
    - Other relative links (images, PDFs, etc.) → `/raw/<path>`
    - Absolute/external links untouched
    """
    current_dir = current.parent

    def fix_href(m: re.Match[str]) -> str:
        attr, quote, url = m.group("attr"), m.group("q"), m.group("url")
        new_url = _resolve_url(url, current_dir, root, is_link=(attr == "href"))
        extra = ""
        if attr == "href" and new_url.startswith("/view/"):
            extra = ' target="_blank" rel="noopener"'
        return f"{attr}={quote}{new_url}{quote}{extra}"

    pattern = re.compile(r'(?P<attr>href|src)=(?P<q>["\'])(?P<url>[^"\']+)(?P=q)')
    return pattern.sub(fix_href, html_text)


def _resolve_url(url: str, current_dir: Path, root: Path, *, is_link: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or url.startswith(("#", "mailto:")):
        return url
    # Fragment-only already handled; split off fragment for path resolution.
    path_part, _, fragment = url.partition("#")
    if not path_part:
        return url
    root_abs = root.resolve()
    target = (current_dir / unquote(path_part)).resolve()
    try:
        rel = target.relative_to(root_abs).as_posix()
    except ValueError:
        # Link escapes workspace. Fall back to matching by basename at any
        # depth — content often has off-by-one `../` counts and the author's
        # intent is almost always to stay inside the workspace.
        fallback = _find_by_basename(root_abs, Path(unquote(path_part)).name)
        if fallback is None:
            return url
        rel = fallback.relative_to(root_abs).as_posix()
    prefix = "/view/" if is_link and Path(rel).suffix.lower() == ".md" else "/raw/"
    suffix = f"#{fragment}" if fragment else ""
    return f"{prefix}{rel}{suffix}"


def _find_by_basename(root: Path, name: str) -> Path | None:
    """Find the shallowest file in `root` with the given basename."""
    best: Path | None = None
    for candidate in root.rglob(name):
        if best is None or len(candidate.parts) < len(best.parts):
            best = candidate
    return best


def render_markdown(path: Path, root: Path, index: dict[str, Path]) -> str:
    raw = path.read_text(encoding="utf-8")
    # Strip YAML front matter if present (Obsidian-style).
    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end != -1:
            raw = raw[end + 4 :].lstrip("\n")
    expanded = _expand_wikilinks(raw, path, root, index)
    md = _make_renderer()
    body = md.render(expanded)
    return _rewrite_links(body, path, root)


# --- File tree ---------------------------------------------------------------


def _build_tree_html(root: Path, current: Path | None) -> str:
    """Render a collapsible file tree of the workspace."""
    current_rel = current.relative_to(root).as_posix() if current else ""

    def walk(dir_path: Path) -> str:
        entries = sorted(
            (p for p in dir_path.iterdir() if not p.name.startswith(".")),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
        items = []
        for entry in entries:
            if entry.is_dir():
                inner = walk(entry)
                if not inner.strip():
                    continue
                items.append(
                    f'<li class="dir"><details open>'
                    f"<summary>{html.escape(entry.name)}</summary>"
                    f"<ul>{inner}</ul></details></li>"
                )
            elif entry.suffix.lower() == ".md":
                rel = entry.relative_to(root).as_posix()
                active = " active" if rel == current_rel else ""
                items.append(
                    f'<li class="file{active}">'
                    f'<a href="/view/{rel}">{html.escape(entry.name)}</a></li>'
                )
        return "".join(items)

    return f'<ul class="tree">{walk(root)}</ul>'


# --- Page template -----------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --bg: #fafafa; --fg: #222; --muted: #666; --accent: #0969da;
    --border: #e1e4e8; --code-bg: #f6f8fa; --sidebar-bg: #f0f2f5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      sans-serif; color: var(--fg); background: var(--bg);
    display: grid; grid-template-columns: 280px 1fr; min-height: 100vh;
  }}
  aside {{
    background: var(--sidebar-bg); border-right: 1px solid var(--border);
    padding: 1rem; overflow-y: auto; max-height: 100vh; position: sticky;
    top: 0; font-size: 13px;
  }}
  aside h2 {{
    margin: 0 0 .5rem; font-size: 11px; text-transform: uppercase;
    letter-spacing: .05em; color: var(--muted);
  }}
  ul.tree, aside ul {{ list-style: none; padding-left: .9rem; margin: 0; }}
  ul.tree {{ padding-left: 0; }}
  li.file a {{
    color: var(--fg); text-decoration: none; display: block;
    padding: 2px 4px; border-radius: 3px;
  }}
  li.file a:hover {{ background: rgba(0,0,0,.05); }}
  li.file.active a {{ background: var(--accent); color: white; }}
  summary {{ cursor: pointer; padding: 2px 0; font-weight: 500; }}
  summary:hover {{ color: var(--accent); }}
  main {{
    padding: 2rem 3rem; max-width: 1100px; overflow-x: auto;
  }}
  main h1:first-child {{ margin-top: 0; }}
  a {{ color: var(--accent); }}
  .wikilink-missing {{ color: #aaa; text-decoration: line-through; }}
  code {{
    background: var(--code-bg); padding: .15em .35em; border-radius: 3px;
    font-size: .9em;
  }}
  pre {{
    background: var(--code-bg); padding: 1rem; border-radius: 6px;
    overflow-x: auto;
  }}
  pre code {{ padding: 0; background: transparent; }}
  table {{
    border-collapse: collapse; margin: 1rem 0; font-size: .9em;
    display: block; overflow-x: auto; max-width: 100%;
  }}
  th, td {{
    border: 1px solid var(--border); padding: .4rem .6rem;
    text-align: left; vertical-align: top;
  }}
  th {{ background: var(--code-bg); font-weight: 600; }}
  blockquote {{
    border-left: 4px solid var(--border); margin: 1rem 0;
    padding: .2rem 1rem; color: var(--muted);
  }}
  input[type="checkbox"] {{ margin-right: .4em; }}
  img {{ max-width: 100%; }}
  .crumbs {{
    font-size: 12px; color: var(--muted); margin-bottom: 1rem;
  }}
  .crumbs a {{ color: var(--muted); }}
</style>
</head>
<body>
<aside>
  <h2>{workspace}</h2>
  {tree}
</aside>
<main>
  <div class="crumbs">{crumbs}</div>
  {body}
</main>
</body>
</html>
"""


def _crumbs(rel: str) -> str:
    parts = rel.split("/")
    out = ['<a href="/">~</a>']
    acc: list[str] = []
    for p in parts[:-1]:
        acc.append(p)
        out.append(f'<a href="/view/{"/".join(acc)}">{html.escape(p)}</a>')
    out.append(f"<span>{html.escape(parts[-1])}</span>")
    return " / ".join(out)


# --- HTTP handler ------------------------------------------------------------


class ViewerHandler(BaseHTTPRequestHandler):
    workspace: Path
    wikilink_index: dict[str, Path]

    # Silence default noisy access log; keep errors.
    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"[viewer] {format % args}\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", ""):
            self._serve_index()
        elif path.startswith("/view/"):
            self._serve_markdown(path[len("/view/") :])
        elif path.startswith("/raw/"):
            self._serve_raw(path[len("/raw/") :])
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    # --- handlers ---

    def _serve_index(self) -> None:
        # Pick a sensible entry point.
        for candidate in ("README.md", "SUMMARY.md", "SCOPE.md"):
            entry = self.workspace / candidate
            if entry.exists():
                self._render_page(entry)
                return
        # Fallback: first markdown file.
        first = next(self.workspace.rglob("*.md"), None)
        if first:
            self._render_page(first)
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "No markdown files found")

    def _serve_markdown(self, rel: str) -> None:
        target = self._safe_join(rel)
        if target is None or not target.exists() or target.suffix.lower() != ".md":
            self._send_error(HTTPStatus.NOT_FOUND, f"Not found: {rel}")
            return
        self._render_page(target)

    def _serve_raw(self, rel: str) -> None:
        target = self._safe_join(rel)
        if target is None or not target.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, f"Not found: {rel}")
            return
        ctype, _ = mimetypes.guess_type(target.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        data = target.read_bytes()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- helpers ---

    def _safe_join(self, rel: str) -> Path | None:
        candidate = (self.workspace / rel).resolve()
        try:
            candidate.relative_to(self.workspace.resolve())
        except ValueError:
            return None
        return candidate

    def _render_page(self, md_path: Path) -> None:
        try:
            body = render_markdown(md_path, self.workspace, self.wikilink_index)
        except Exception as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Render failed: {exc}",
            )
            return
        rel = md_path.relative_to(self.workspace).as_posix()
        page = _PAGE.format(
            title=f"{md_path.name} · {self.workspace.name}",
            workspace=html.escape(self.workspace.name),
            tree=_build_tree_html(self.workspace, md_path),
            crumbs=_crumbs(rel),
            body=body,
        )
        data = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, msg: str) -> None:
        data = f"<h1>{status.value} {status.phrase}</h1><p>{html.escape(msg)}</p>"
        body = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --- Entry point -------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lightweight markdown viewer for an SLRHarness workspace.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="Path to the workspace directory to serve.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port (default: 8765).",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace not found or not a directory: {workspace}")

    ViewerHandler.workspace = workspace
    ViewerHandler.wikilink_index = _build_wikilink_index(workspace)

    server = ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    url = f"http://{args.host}:{args.port}"
    sys.stderr.write(
        f"[viewer] serving {workspace} at {url}\n[viewer] Ctrl-C to stop\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[viewer] shutting down\n")
        server.server_close()


if __name__ == "__main__":
    main()
