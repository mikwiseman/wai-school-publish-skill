#!/usr/bin/env python3
"""Bundle a static HTML project and publish it to wai.school.

No third-party dependencies: this script is meant to run inside Claude.ai code
execution after the WAI School Publish skill is installed.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("WAI_SCHOOL_PUBLISH_ENDPOINT", "https://wai.school/api/projects/publish")
SKILL_VERSION = "2026-06-22.2"
MAX_INLINE_ASSET_BYTES = 900_000

TEXT_EXTENSIONS = {".html", ".htm", ".css", ".js", ".mjs", ".svg", ".txt", ".json"}
IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
SECRET_FILE_NAMES = {".env", ".env.local", ".env.production"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I),
    re.compile(r"\b(?:OPENAI|ANTHROPIC|GEMINI|NOTION|RESEND|WAIPAY)_[A-Z0-9_]*\s*[:=]", re.I),
]


def fail(message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def project_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.name.startswith(".") and p.name.lower() not in SECRET_FILE_NAMES:
                continue
            out.append(p)
    return out


def scan_for_secrets(root: Path) -> None:
    for path in project_files(root):
        if path.name.lower() in SECRET_FILE_NAMES:
            fail(f"Refusing to publish secret-looking file: {path.relative_to(root)}")
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        text = read_text(path)
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                fail(f"Refusing to publish because {path.relative_to(root)} appears to contain a secret")


def choose_html(root: Path) -> Path:
    if root.is_file():
        if root.suffix.lower() not in {".html", ".htm"}:
            fail("The selected file is not HTML. Create or select index.html first.")
        return root

    index = root / "index.html"
    if index.exists():
        return index

    html_files = sorted([p for p in project_files(root) if p.suffix.lower() in {".html", ".htm"}])
    if not html_files:
        fail("No HTML file found. Create index.html first, then publish again.")
    return html_files[0]


def local_asset_path(base: Path, raw_url: str) -> Path | None:
    if not raw_url or raw_url.startswith(("http://", "https://", "data:", "mailto:", "#", "javascript:")):
        return None
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme or parsed.netloc:
        return None
    candidate = (base / urllib.parse.unquote(parsed.path)).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def inline_text_assets(html: str, html_path: Path) -> tuple[str, list[str]]:
    base = html_path.parent
    warnings: list[str] = []

    def style_repl(match: re.Match[str]) -> str:
        href = match.group("href")
        asset = local_asset_path(base, href)
        if not asset:
            warnings.append(f"CSS not inlined: {href}")
            return match.group(0)
        return f"<style>\n{read_text(asset)}\n</style>"

    def script_repl(match: re.Match[str]) -> str:
        src = match.group("src")
        asset = local_asset_path(base, src)
        if not asset:
            warnings.append(f"Script not inlined: {src}")
            return match.group(0)
        return f"<script>\n{read_text(asset)}\n</script>"

    html = re.sub(
        r"<link\b(?=[^>]*rel=[\"']?stylesheet[\"']?)(?=[^>]*href=[\"'](?P<href>[^\"']+)[\"'])[^>]*>",
        style_repl,
        html,
        flags=re.I,
    )
    html = re.sub(
        r"<script\b(?=[^>]*src=[\"'](?P<src>[^\"']+)[\"'])[^>]*>\s*</script>",
        script_repl,
        html,
        flags=re.I,
    )
    return html, warnings


def inline_binary_assets(html: str, html_path: Path) -> tuple[str, list[str]]:
    base = html_path.parent
    warnings: list[str] = []

    def repl(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        src = match.group("src")
        suffix = match.group("suffix")
        asset = local_asset_path(base, src)
        if not asset:
            return match.group(0)
        size = asset.stat().st_size
        if size > MAX_INLINE_ASSET_BYTES:
            warnings.append(f"Asset too large to inline: {asset.name}")
            return match.group(0)
        mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
        return f'{prefix}data:{mime};base64,{encoded}{suffix}'

    html = re.sub(
        r"(?P<prefix>\b(?:src|href)=['\"])(?P<src>[^'\"]+\.(?:png|jpe?g|gif|webp|svg|mp3|wav|ogg|woff2?|ttf))(?P<suffix>['\"])",
        repl,
        html,
        flags=re.I,
    )
    return html, warnings


def bundle_html(root_or_file: Path) -> tuple[str, str, list[str]]:
    html_path = choose_html(root_or_file)
    root = html_path.parent if root_or_file.is_file() else root_or_file
    scan_for_secrets(root)

    html = read_text(html_path)
    warnings: list[str] = []
    html, w = inline_text_assets(html, html_path)
    warnings.extend(w)
    html, w = inline_binary_assets(html, html_path)
    warnings.extend(w)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else html_path.stem
    return html, title[:80] or "Мой проект", warnings


def publish(html: str, title: str) -> dict:
    payload = json.dumps(
        {
            "html": html,
            "title": title,
            "source": "claude-ai-skill",
            "skillVersion": SKILL_VERSION,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"content-type": "application/json", "user-agent": f"wai-school-publish/{SKILL_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            message = data.get("error") or body
        except json.JSONDecodeError:
            message = body or str(e)
        if re.search(r"host.*not.*allow|allowlist|egress|blocked", message, re.I):
            fail(
                "Claude code environment cannot reach wai.school. "
                "Ask a mentor to allow wai.school network access or publish through the WAI School page."
            )
        fail(f"Server rejected publish ({e.code}): {message}")
    except urllib.error.URLError as e:
        message = str(e.reason if hasattr(e, "reason") else e)
        if re.search(r"host.*not.*allow|allowlist|egress|blocked|name or service not known", message, re.I):
            fail(
                "Claude code environment cannot reach wai.school. "
                "Ask a mentor to allow wai.school network access or publish through the WAI School page."
            )
        fail(f"Could not reach WAI School publish server: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a static project to wai.school")
    parser.add_argument("--dir", default=".", help="Project folder or HTML file to publish")
    parser.add_argument("--dry-run", action="store_true", help="Bundle and validate locally, but do not upload")
    args = parser.parse_args()

    target = Path(args.dir).expanduser().resolve()
    if not target.exists():
        fail(f"Path does not exist: {target}")

    html, title, warnings = bundle_html(target)
    if args.dry_run:
        print(json.dumps({"ok": True, "title": title, "bytes": len(html.encode("utf-8")), "warnings": warnings}, ensure_ascii=False))
        return

    result = publish(html, title)
    result["warnings"] = warnings
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
