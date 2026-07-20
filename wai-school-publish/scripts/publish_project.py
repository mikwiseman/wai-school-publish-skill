#!/usr/bin/env python3
"""Publish a static HTML project and local assets to wai.school.

No third-party dependencies. Runs on Python 3.8+ (python3, python, or `py -3`
on Windows). Blocking checks mirror the wai.school server; everything else is
a warning, so a normal child project publishes on the first try.

Every failure JSON has two fields: "error" (English, for logs and mentors)
and "fix" (Russian, the one action that repairs it — Claude reads this aloud).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ENDPOINT = os.environ.get("WAI_SCHOOL_PUBLISH_ENDPOINT", "https://wai.school/api/projects/publish")
SKILL_VERSION = "2026-07-20.2"
MAX_PROJECT_FILES = 400
MAX_PROJECT_FILE_BYTES = 10_000_000
MAX_PROJECT_TOTAL_FILE_BYTES = 50_000_000
NETWORK_TIMEOUT_SECONDS = 120
STATE_FILE_NAME = ".wai-school-project.json"

TEXT_EXTENSIONS = {".html", ".htm", ".css", ".gltf", ".js", ".mjs", ".txt", ".json"}
PROJECT_FILE_EXTENSIONS = {
    ".avif", ".bin", ".bmp", ".css", ".flac", ".gif", ".glb", ".gltf", ".ico",
    ".jpeg", ".jpg", ".js", ".json", ".m4a", ".mid", ".midi", ".mjs", ".mp3",
    ".mp4", ".ogg", ".otf", ".png", ".ttf", ".txt", ".wav", ".webm",
    ".webmanifest", ".webp", ".woff", ".woff2", ".wasm",
}
IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
READY_BUILD_DIRS = {"dist", "build", "out", "public"}
ENTRY_EXCLUDED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".next"}
SECRET_FILE_NAMES = {".env", ".env.local", ".env.production"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I),
    re.compile(r"\b(?:OPENAI|ANTHROPIC|GEMINI|NOTION|RESEND|WAIPAY)_[A-Z0-9_]*\s*[:=]", re.I),
]

# Platform bans, kept byte-identical to the wai.school server checks: published
# projects run in a sandboxed iframe where these APIs are blocked at runtime.
FORBIDDEN_RUNTIME_PATTERNS = [
    re.compile(r"\b(?:src|srcset|href|poster|action)\s*=\s*[\"']?\s*(?:https?:)?//", re.I),
    re.compile(r"\burl\(\s*[\"']?\s*(?:https?:)?//", re.I),
    re.compile(r"@import\s+(?:url\(\s*)?[\"']?\s*(?:https?:)?//", re.I),
    re.compile(r"\b(?:fetch|importScripts)\s*\(\s*['\"`]\s*(?:https?:)?//", re.I),
    re.compile(r"\bimport\s*\(\s*['\"`]\s*(?:https?:)?//", re.I),
    re.compile(r"\b(?:import|export)\s+(?:[\s\S]{0,200}?\s+from\s+)?['\"`]\s*(?:https?:)?//", re.I),
    re.compile(r"\bnew\s+(?:WebSocket|EventSource)\s*\(\s*['\"`]\s*(?:wss?:|https?:)?//", re.I),
    re.compile(r"\bnavigator\.sendBeacon\s*\(\s*['\"`]\s*(?:https?:)?//", re.I),
    re.compile(r"\bXMLHttpRequest\b[\s\S]{0,800}\.open\s*\(\s*['\"`][A-Z]+['\"`]\s*,\s*['\"`]\s*(?:https?:)?//", re.I),
]
FORBIDDEN_STORAGE_RE = re.compile(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie|navigator\.serviceWorker|caches)\b", re.I)
FORBIDDEN_DIALOG_RE = re.compile(r"\b(?:alert|confirm|prompt)\s*\(", re.I)

RUNTIME_FIX_RU = (
    "Проект на wai.school живёт в защищённой рамке без доступа к внешней сети. "
    "Сохрани нужный файл (картинку, скрипт, шрифт) внутрь папки проекта и подключи его относительным путём вида ./assets/имя."
)
STORAGE_FIX_RU = (
    "localStorage, sessionStorage, cookie и indexedDB не работают в опубликованном проекте (защищённая рамка). "
    "Храни данные в обычной переменной JavaScript — сохранение между визитами на wai.school пока не поддерживается."
)
DIALOG_FIX_RU = (
    "alert, confirm и prompt не работают в опубликованном проекте. "
    "Замени их на сообщение прямо на странице: например, div с текстом, который появляется и исчезает."
)
NETWORK_FIX_RU = (
    "Не получилось соединиться с wai.school. Проверь интернет и запусти команду ещё раз. "
    "Если Claude пишет, что сеть запрещена настройками, — публикуй через браузер: wai.school/student → «Мои проекты»."
)

HTML_QUOTED_REF_RE = re.compile(
    r"""(?<![-:\w])(?:src|href|poster)\s*=\s*(['"])(?P<url>[^'"]+)\1""",
    re.I,
)
# Attribute references (quoted or unquoted) are validated only OUTSIDE <script>
# blocks: JS assignments like `img.src = photoMap.preview;` are code, not
# paths, and treating them as paths broke real children's projects.
HTML_ANY_REF_RE = re.compile(
    r"""(?<![-:\w])(?:src|href|poster)\s*=\s*(?:(['"])(?P<quoted>[^'"]+)\1|(?P<unquoted>[^\s"'=<>`]+))""",
    re.I,
)
SCRIPT_BLOCK_RE = re.compile(r"""<script\b[^>]*>[\s\S]*?</script>""", re.I)
SCRIPT_OPEN_TAG_RE = re.compile(r"""<script\b[^>]*>""", re.I)
CSS_URL_RE = re.compile(r"""url\(\s*(?P<quote>['"]?)(?P<url>[^)'"]+)(?P=quote)\s*\)""", re.I)
GLTF_URI_RE = re.compile(r"""["']uri["']\s*:\s*["'](?P<url>[^"']+)["']""", re.I)
# JS quoted-literal patterns: used to COLLECT extra files for upload, and to
# validate a literal only when it names a concrete asset path with a known
# extension (fetch('./levels/one.json')). Dynamic expressions never fail.
JS_COLLECT_PATTERNS = [
    re.compile(r"""\bfetch\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*(?:[,)]|$)"""),
    re.compile(r"""\bimport\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*\)"""),
    re.compile(r"""\bnew\s+URL\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*,"""),
    re.compile(r"""\b(?:import|export)\s+(?:[^'"]+\s+from\s+)?(['"])(?P<url>[^'"]+)\1"""),
    re.compile(r"""\bnew\s+(?:Worker|SharedWorker|Audio|Image)\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*(?:[,)]|$)"""),
    re.compile(r"""\b(?:src|href)\s*=\s*(['"`])(?P<url>[^'"`]+)\1"""),
    re.compile(r"""\bsetAttribute\s*\(\s*['"](?:src|href|poster)['"]\s*,\s*(['"`])(?P<url>[^'"`]+)\1"""),
]


def fail(message: str, fix: str = "", code: int = 1) -> None:
    payload = {"ok": False, "error": message}
    if fix:
        payload["fix"] = fix
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


class ProjectPublishHttpError(Exception):
    def __init__(self, status: int, code: str, message: str, data=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data or {}


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


def ready_build_indexes(root: Path) -> list[Path]:
    indexes: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in ENTRY_EXCLUDED_DIRS and not name.startswith(".")
        ]
        directory = Path(dirpath)
        if directory == root or directory.name.lower() not in READY_BUILD_DIRS:
            continue
        for filename in ("index.html", "index.htm"):
            candidate = directory / filename
            if candidate.is_file():
                indexes.append(candidate)
        dirnames[:] = []
    return sorted(indexes)


def choose_html(root: Path) -> Path:
    if root.is_file():
        if root.suffix.lower() not in {".html", ".htm"}:
            fail(
                "The selected file is not HTML.",
                "Выбери HTML-файл проекта или папку, где лежит index.html.",
            )
        return root

    root_indexes = [candidate for candidate in (root / "index.html", root / "index.htm") if candidate.is_file()]
    all_build_indexes = ready_build_indexes(root)
    compiled_build_indexes = [path for path in all_build_indexes if path.parent.name.lower() != "public"]
    build_indexes = compiled_build_indexes or all_build_indexes
    if (root / "package.json").is_file() and build_indexes:
        if len(build_indexes) > 1:
            options = ", ".join(path.relative_to(root).as_posix() for path in build_indexes)
            fail(
                f"Several ready builds were found: {options}.",
                "Нашлось несколько готовых сборок. Запусти publisher ещё раз, указав --dir на нужную папку сборки.",
            )
        return build_indexes[0]
    if root_indexes:
        return root_indexes[0]
    if build_indexes:
        if len(build_indexes) > 1:
            options = ", ".join(path.relative_to(root).as_posix() for path in build_indexes)
            fail(
                f"Several ready builds were found: {options}.",
                "Нашлось несколько готовых сборок. Запусти publisher ещё раз, указав --dir на нужную папку сборки.",
            )
        return build_indexes[0]

    html_files = sorted([p for p in project_files(root) if p.suffix.lower() in {".html", ".htm"}])
    if not html_files:
        fail(
            "No HTML file found.",
            "В папке нет HTML-файла. Создай index.html — главную страницу проекта — и запусти публикацию снова.",
        )
    if len(html_files) > 1:
        options = ", ".join(p.relative_to(root).as_posix() for p in html_files[:6])
        fail(
            f"Several HTML files and no index.html: {options}.",
            "В папке несколько HTML-файлов, и непонятно, какой главный. Переименуй главную страницу в index.html или укажи файл: --dir путь/к/файлу.html.",
        )
    return html_files[0]


def is_external_or_virtual_ref(raw_url: str) -> bool:
    value = (raw_url or "").strip()
    if (
        not value
        or value.startswith(("#", "//"))
        or re.match(r"^[a-z][a-z0-9+.-]*:", value, flags=re.I)
        or value.startswith(("var(", "env("))
    ):
        return True
    return False


def local_reference_candidate(base: Path, raw_url: str, project_root: Path) -> Path | None:
    value = (raw_url or "").strip()
    if is_external_or_virtual_ref(value):
        return None
    if "\0" in value or "${" in value or "{" in value or "}" in value:
        return None

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    ref_path = urllib.parse.unquote(parsed.path or "").strip()
    if not ref_path or ref_path.startswith("/"):
        return None

    candidate = (base / ref_path).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def check_quoted_reference(base: Path, raw_url: str, project_root: Path, source: Path) -> None:
    """Blocking check for explicitly quoted references in HTML and CSS only.

    JS code is never validated here: `img.src = photoMap.preview` is code, not
    a path, and guessing breaks real projects.
    """
    value = (raw_url or "").strip()
    if not value or value.startswith("#") or value.startswith(("data:", "mailto:", "tel:", "javascript:", "blob:", "about:")):
        return
    if value.startswith(("http://", "https://", "//")):
        fail(
            f"External reference in {source.relative_to(project_root)}: {value}",
            RUNTIME_FIX_RU,
        )
    if "${" in value or "{" in value or "}" in value or "\0" in value:
        return

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return
    ref_path = urllib.parse.unquote(parsed.path or "").strip()
    if not ref_path:
        return
    if ref_path.startswith("/"):
        fail(
            f"Absolute path in {source.relative_to(project_root)}: {value}",
            "Используй относительные пути внутри папки проекта: ./style.css, ./assets/hero.png — без начального «/».",
        )
    suffix = Path(ref_path).suffix.lower()
    if not suffix:
        return

    candidate = (base / ref_path).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        fail(
            f"Reference escapes the project folder in {source.relative_to(project_root)}: {value}",
            "Файл лежит за пределами папки проекта. Скопируй его внутрь папки и поправь путь.",
        )
    if not candidate.exists() or not candidate.is_file():
        fail(
            f"Missing local project file referenced from {source.relative_to(project_root)}: {value}",
            f"Страница использует файл «{ref_path}», но его нет в папке. Добавь файл или исправь путь.",
        )
    # A reference to an existing file that publishing cannot carry would break
    # silently on the live page — refuse with the exact repair instead.
    if suffix in {".html", ".htm"}:
        fail(
            f"Secondary HTML page referenced from {source.relative_to(project_root)}: {value}",
            f"Публикуется только одна страница index.html. Перенеси содержимое «{ref_path}» в index.html (например, отдельным экраном или разделом) или убери ссылку на него.",
        )
    if suffix not in PROJECT_FILE_EXTENSIONS:
        fix = f"Формат файла «{ref_path}» не поддерживается публикацией. Убери ссылку на него или замени файл на поддерживаемый формат."
        if suffix == ".svg":
            fix = f"SVG-файлы не публикуются отдельными файлами. Вставь содержимое «{ref_path}» прямо в index.html как inline <svg> или сохрани картинку в PNG/WebP."
        fail(f"Unsupported referenced file type from {source.relative_to(project_root)}: {value}", fix)


def html_any_reference_url(match: re.Match) -> str:
    return match.group("quoted") or match.group("unquoted") or ""


def validate_js_literals(source: Path, text: str, project_root: Path) -> None:
    for pattern in JS_COLLECT_PATTERNS:
        for match in pattern.finditer(text):
            value = (match.group("url") or "").strip()
            if value.startswith(("http://", "https://")):
                check_quoted_reference(source.parent, value, project_root, source)
                continue
            suffix = Path(urllib.parse.urlparse(value).path or "").suffix.lower()
            if suffix and (suffix in PROJECT_FILE_EXTENSIONS or suffix in {".html", ".htm"}):
                check_quoted_reference(source.parent, value, project_root, source)


def validate_references(project_root: Path, html_path: Path, upload_paths: list[Path]) -> None:
    for path in [html_path] + upload_paths:
        suffix = path.suffix.lower()
        if suffix in {".html", ".htm"}:
            text = read_text(path)
            for tag in SCRIPT_OPEN_TAG_RE.findall(text):
                for match in HTML_ANY_REF_RE.finditer(tag):
                    check_quoted_reference(path.parent, html_any_reference_url(match), project_root, path)
            stripped = SCRIPT_BLOCK_RE.sub(" ", text)
            for match in HTML_ANY_REF_RE.finditer(stripped):
                check_quoted_reference(path.parent, html_any_reference_url(match), project_root, path)
            for match in CSS_URL_RE.finditer(stripped):
                check_quoted_reference(path.parent, match.group("url"), project_root, path)
            validate_js_literals(path, text, project_root)
        elif suffix == ".css":
            text = read_text(path)
            for match in CSS_URL_RE.finditer(text):
                check_quoted_reference(path.parent, match.group("url"), project_root, path)
        elif suffix in {".js", ".mjs"}:
            validate_js_literals(path, read_text(path), project_root)


def validate_forbidden_runtime(project_root: Path, html_path: Path, upload_paths: list[Path]) -> None:
    for path in [html_path] + upload_paths:
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        text = read_text(path)
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            rel = path.name
        if FORBIDDEN_DIALOG_RE.search(text):
            fail(f"Browser modal dialogs are not allowed ({rel}).", DIALOG_FIX_RU)
        if FORBIDDEN_STORAGE_RE.search(text):
            fail(f"Browser storage APIs are not allowed ({rel}).", STORAGE_FIX_RU)
        for pattern in FORBIDDEN_RUNTIME_PATTERNS:
            if pattern.search(text):
                fail(f"External network or external assets are not allowed ({rel}).", RUNTIME_FIX_RU)


def scan_for_secrets(project_root: Path, html_path: Path, upload_paths: list[Path]) -> None:
    for path in [html_path] + upload_paths:
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        text = read_text(path)
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                fail(
                    f"Refusing to publish: {path.name} appears to contain a secret",
                    f"В файле {path.name} есть строка, похожая на секретный ключ или пароль. Убери её — секреты публиковать нельзя.",
                )


def collect_referenced_project_files(source: Path, project_root: Path, out: set[Path], seen: set[Path]) -> None:
    resolved = source.resolve()
    if resolved in seen:
        return
    seen.add(resolved)

    suffix = source.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        return

    text = read_text(source)
    js_text = ""
    if suffix in {".html", ".htm"}:
        for match in HTML_QUOTED_REF_RE.finditer(text):
            add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)
        for match in CSS_URL_RE.finditer(text):
            add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)
        js_text = text
    elif suffix == ".css":
        for match in CSS_URL_RE.finditer(text):
            add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)
    elif suffix in {".js", ".mjs"}:
        js_text = text
    elif suffix == ".gltf":
        for match in GLTF_URI_RE.finditer(text):
            add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)

    if js_text:
        for pattern in JS_COLLECT_PATTERNS:
            for match in pattern.finditer(js_text):
                add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)


def add_referenced_project_file(base: Path, raw_url: str, project_root: Path, out: set[Path], seen: set[Path]) -> None:
    candidate = local_reference_candidate(base, raw_url, project_root)
    if not candidate:
        return
    out.add(candidate)
    collect_referenced_project_files(candidate, project_root, out, seen)


def safe_rel_path(path: Path, base: Path) -> str | None:
    rel = path.relative_to(base).as_posix()
    parts = rel.split("/")
    if (
        not rel
        or rel.startswith("/")
        or len(rel) > 180
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
    ):
        return None
    return rel


def select_upload_files(root_or_file: Path, html_path: Path, warnings: list[str]) -> list[Path]:
    """Pick which local files travel with index.html.

    Anything unsupported is SKIPPED with a warning, never a failure: children's
    folders legitimately contain CLAUDE.md, PROJECT.md, drafts, and old HTML
    versions, and none of that may block publishing.
    """
    base = html_path.parent
    if root_or_file.is_file():
        referenced: set[Path] = set()
        collect_referenced_project_files(html_path, base, referenced, set())
        candidates = sorted(referenced)
    else:
        candidates = project_files(base)

    selected: list[Path] = []
    skipped: list[str] = []
    for path in candidates:
        if path.resolve() == html_path.resolve():
            continue
        rel = safe_rel_path(path, base)
        if rel is None:
            skipped.append(path.name)
            continue
        suffix = path.suffix.lower()
        if path.name.lower() in SECRET_FILE_NAMES:
            skipped.append(rel)
            continue
        if suffix in {".html", ".htm"} or suffix not in PROJECT_FILE_EXTENSIONS:
            skipped.append(rel)
            continue
        selected.append(path)

    if skipped:
        shown = ", ".join(sorted(skipped)[:8])
        more = f" и ещё {len(skipped) - 8}" if len(skipped) > 8 else ""
        warnings.append(
            f"Не публикуются служебные и лишние файлы: {shown}{more}. "
            "На wai.school уходит только index.html и файлы, которые нужны странице (картинки, стили, скрипты, звуки, данные)."
        )
    return selected


def project_file_manifest(html_path: Path, upload_paths: list[Path]) -> list[dict]:
    base = html_path.parent
    files: list[dict] = []
    total_bytes = 0
    for path in upload_paths:
        rel = safe_rel_path(path, base)
        if rel is None:
            continue
        size = path.stat().st_size
        if size <= 0:
            continue
        if size > MAX_PROJECT_FILE_BYTES:
            fail(
                f"Project file is too large: {rel}",
                f"Файл {rel} больше 10 МБ. Сожми его (картинку — в WebP/JPEG, звук — в MP3/OGG) и попробуй снова.",
            )
        total_bytes += size
        if total_bytes > MAX_PROJECT_TOTAL_FILE_BYTES:
            fail(
                f"Project files are too large; limit is {MAX_PROJECT_TOTAL_FILE_BYTES} bytes",
                "Все файлы проекта вместе больше 50 МБ. Убери или сожми самые тяжёлые файлы.",
            )
        files.append(
            {
                "path": rel,
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "contentBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "byteLength": size,
            }
        )
        if len(files) > MAX_PROJECT_FILES:
            fail(
                f"Project has too many files; limit is {MAX_PROJECT_FILES}",
                "В проекте больше 400 файлов. Оставь только то, что реально использует страница.",
            )
    return sorted(files, key=lambda file: file["path"])


def bundle_project(root_or_file: Path) -> tuple[str, str, list[str], list[dict]]:
    html_path = choose_html(root_or_file)
    warnings: list[str] = []
    upload_paths = select_upload_files(root_or_file, html_path, warnings)
    project_root = html_path.parent if root_or_file.is_file() else root_or_file

    scan_for_secrets(project_root, html_path, upload_paths)
    validate_forbidden_runtime(project_root, html_path, upload_paths)
    validate_references(project_root, html_path, upload_paths)
    files = project_file_manifest(html_path, upload_paths)

    html = read_text(html_path)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else html_path.stem
    return html, title[:80] or "Мой проект", warnings, files


def state_path(root_or_file: Path) -> Path:
    if root_or_file.is_dir() or root_or_file.name.lower() in {"index.html", "index.htm"}:
        return (root_or_file if root_or_file.is_dir() else root_or_file.parent) / STATE_FILE_NAME
    identity = hashlib.sha256(root_or_file.name.encode("utf-8")).hexdigest()[:12]
    return root_or_file.parent / f".wai-school-project-{identity}.json"


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        fail(
            f"Cannot read safe publish state at {path}: {error}",
            "Не читается файл памяти проекта. Проверь права на папку и запусти команду ещё раз.",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        fail(
            f"Cannot read safe publish state at {path}: the file is corrupted.",
            "Файл памяти проекта повреждён. Не публикуй проект как новый — позови ментора, он восстановит связь со старой ссылкой.",
        )
    if not isinstance(data, dict):
        fail(
            f"Cannot read safe publish state at {path}: expected a JSON object.",
            "Файл памяти проекта повреждён. Не публикуй проект как новый — позови ментора.",
        )

    state: dict = {}
    slug = data.get("slug")
    edit_token = data.get("editToken")
    create_token = data.get("createToken")
    pending_request_id = data.get("pendingRequestId")
    current_revision = data.get("currentRevision")

    corrupted_fix = "Файл памяти проекта повреждён — позови ментора."
    if slug is not None:
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", slug):
            fail(f"Cannot read safe publish state at {path}: invalid project slug.", corrupted_fix)
        state["slug"] = slug
    if edit_token is not None:
        if not isinstance(edit_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", edit_token):
            fail(f"Cannot read safe publish state at {path}: invalid edit capability.", corrupted_fix)
        state["editToken"] = edit_token
    if create_token is not None:
        if not isinstance(create_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", create_token):
            fail(f"Cannot read safe publish state at {path}: invalid create capability.", corrupted_fix)
        state["createToken"] = create_token
    if pending_request_id is not None:
        if not isinstance(pending_request_id, str):
            fail(f"Cannot read safe publish state at {path}: invalid pending request id.", corrupted_fix)
        try:
            parsed_request_id = uuid.UUID(pending_request_id)
        except ValueError:
            fail(f"Cannot read safe publish state at {path}: invalid pending request id.", corrupted_fix)
        if str(parsed_request_id) != pending_request_id.lower():
            fail(f"Cannot read safe publish state at {path}: invalid pending request id.", corrupted_fix)
        state["pendingRequestId"] = pending_request_id
    if current_revision is not None:
        if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision <= 0:
            fail(f"Cannot read safe publish state at {path}: invalid project revision.", corrupted_fix)
        state["currentRevision"] = current_revision
    if "owned" in data and not isinstance(data["owned"], bool):
        fail(f"Cannot read safe publish state at {path}: invalid ownership marker.", corrupted_fix)
    if data.get("owned") is True:
        state["owned"] = True
    if not state:
        fail(
            f"Cannot read safe publish state at {path}: no usable project identity or pending request.",
            "Файл памяти проекта пуст или повреждён — позови ментора.",
        )
    if (state.get("editToken") or state.get("owned") or state.get("currentRevision")) and not state.get("slug"):
        fail(
            f"Cannot read safe publish state at {path}: project identity is incomplete.",
            "Файл памяти проекта неполный — позови ментора.",
        )
    return state


def load_project_state(root_or_file: Path) -> tuple[Path, dict]:
    preferred = state_path(root_or_file)
    if preferred.exists() or root_or_file.is_dir() or root_or_file.name.lower() in {"index.html", "index.htm"}:
        return preferred, load_state(preferred)

    legacy = root_or_file.parent / STATE_FILE_NAME
    if not legacy.exists():
        return preferred, {}

    html_files = sorted(
        path.resolve()
        for path in root_or_file.parent.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in {".html", ".htm"}
    )
    if html_files != [root_or_file.resolve()]:
        fail(
            f"Cannot safely match legacy publish state {legacy} to {root_or_file.name}: this folder contains several HTML files.",
            "В папке несколько HTML-файлов, и непонятно, чья это память публикации. Перенеси нужный проект в отдельную папку или позови ментора.",
        )

    state = load_state(legacy)
    try:
        write_state_atomic(preferred, state)
        legacy.unlink()
    except OSError as error:
        fail(f"Cannot migrate safe publish state from {legacy} to {preferred}: {error}", "Не получилось перенести память проекта. Позови ментора.")
    return preferred, state


def write_state_atomic(path: Path, state: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_state(path: Path, result: dict) -> bool:
    slug = str(result.get("slug") or "")
    edit_token = str(result.get("editToken") or "")
    current_revision = result.get("currentRevision")
    if not slug or not isinstance(current_revision, int) or current_revision <= 0:
        return False
    if not edit_token and not result.get("owned"):
        return False
    state = {
        "slug": slug,
        "url": result.get("url"),
        "updatedAt": result.get("updatedAt"),
        "currentRevision": current_revision,
    }
    if edit_token:
        state["editToken"] = edit_token
    if result.get("owned"):
        state["owned"] = True
    write_state_atomic(path, state)
    return True


def save_pending_state(path: Path, state: dict, request_id: str, create_token: str = "") -> None:
    pending = {
        key: state[key]
        for key in ("slug", "editToken", "owned", "currentRevision")
        if state.get(key) is not None
    }
    pending["pendingRequestId"] = request_id
    if create_token:
        pending["createToken"] = create_token
    write_state_atomic(path, pending)


def clear_pending_state(path: Path, state: dict) -> None:
    cleaned = {
        key: state[key]
        for key in ("slug", "editToken", "owned", "currentRevision")
        if state.get(key) is not None
    }
    if cleaned:
        write_state_atomic(path, cleaned)
    elif path.exists():
        path.unlink()


def project_arg_to_slug(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", raw):
        return raw
    match = re.search(r"(?:^|/)project/([a-z0-9][a-z0-9-]{2,80})(?:[/?#]|$)", raw)
    if match:
        return match.group(1)
    fail(
        "Project must be a wai.school/project/... URL or a project slug.",
        "Передай в --project живую ссылку вида https://wai.school/project/имя-проекта.",
    )


def source_endpoint_for_slug(slug: str) -> str:
    parsed = urllib.parse.urlparse(ENDPOINT)
    path = parsed.path.rstrip("/")
    if path.endswith("/publish"):
        source_path = path[: -len("/publish")] + f"/source/{slug}"
    else:
        source_path = f"/api/projects/source/{slug}"
    return urllib.parse.urlunparse(parsed._replace(path=source_path, params="", query="", fragment=""))


def fetch_source_manifest(slug: str, publish_token: str = "", edit_token: str = "") -> dict:
    if bool(publish_token) == bool(edit_token):
        fail(
            "Restoring an existing project needs exactly one saved edit token or --publish-token.",
            "Для восстановления нужен publish-token из кабинета или сохранённая память проекта. Позови ментора, если ни того ни другого нет.",
        )
    credential = {"publishToken": publish_token} if publish_token else {"editToken": edit_token}
    payload = json.dumps(credential, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        source_endpoint_for_slug(slug),
        data=payload,
        headers={"content-type": "application/json", "user-agent": f"wai-school-publish/{SKILL_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as res:
            body = res.read().decode("utf-8")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        data = {}
        try:
            data = json.loads(body)
            message = data.get("error") or body
        except json.JSONDecodeError:
            message = body or str(e)
        fail(f"Server rejected source restore ({e.code}): {message}")
    except urllib.error.URLError as e:
        fail(f"Could not reach WAI School source server: {e}", NETWORK_FIX_RU)
    except json.JSONDecodeError:
        fail("WAI School source server returned invalid JSON")

    if not data.get("ok") or data.get("sourceManifest") != "wai-school-project-v1":
        fail("WAI School source server did not return a valid project manifest")
    if not isinstance(data.get("revision"), int) or data["revision"] <= 0:
        fail("WAI School source server did not return a valid project revision")
    return data


def normalize_manifest_file_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").replace("\0", "").lstrip("/").strip()
    if not normalized or len(normalized) > 180:
        fail("Project source manifest contains an invalid file path")
    if "?" in normalized or "#" in normalized:
        fail(f"Project source manifest contains an invalid file path: {normalized}")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        fail(f"Project source manifest contains an unsafe file path: {normalized}")
    suffix = Path(normalized).suffix.lower()
    if normalized != "index.html" and suffix not in PROJECT_FILE_EXTENSIONS:
        fail(f"Project source manifest contains an unsupported file type: {normalized}")
    if normalized.endswith(".html") and normalized != "index.html":
        fail(f"Project source manifest contains a secondary HTML file: {normalized}")
    return normalized


def decode_manifest_file(content_base64: str, path: str) -> bytes:
    value = str(content_base64 or "").replace("\n", "").replace("\r", "").strip()
    if not value:
        fail(f"Project source manifest contains an empty file: {path}")
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error:
        fail(f"Project source manifest contains invalid base64: {path}")
    if not data:
        fail(f"Project source manifest contains an empty file: {path}")
    if len(data) > MAX_PROJECT_FILE_BYTES and path != "index.html":
        fail(f"Project source manifest file is too large: {path}")
    return data


def validate_restore_target(target: Path, force: bool) -> None:
    if force and not target.name.startswith("wai-school-project"):
        fail(
            "For safety, --force restore only works with a folder named wai-school-project...",
            "Восстановление с --force работает только в папку с именем, начинающимся на wai-school-project.",
        )
    if target.exists() and target.is_symlink():
        fail("Restore target must be a real folder, not a symlink.")
    if target.exists() and not target.is_dir():
        fail("Restore target must be a folder, not a file.")
    resolved = target.resolve()
    if force:
        home = Path.home().resolve()
        cwd = Path.cwd().resolve()
        if resolved == Path(resolved.anchor) or resolved == home or resolved == cwd or resolved in cwd.parents:
            fail("For safety, --force restore refuses broad folders. Use a clean wai-school-project folder.")
    if target.exists():
        existing = [p for p in target.iterdir() if p.name != STATE_FILE_NAME]
        if existing and not force:
            fail(
                "Restore target folder is not empty. Use a clean folder or pass --force.",
                "Папка для восстановления не пустая. Укажи новую пустую папку.",
            )


def clear_restore_target(target: Path) -> None:
    existing = [p for p in target.iterdir() if p.name != STATE_FILE_NAME]
    for path in existing:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def prepare_manifest_files(manifest: dict) -> tuple[list[tuple[str, bytes]], int]:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        fail("WAI School source manifest does not contain project files")
    if len(files) > MAX_PROJECT_FILES + 1:
        fail(f"WAI School source manifest has too many files; limit is {MAX_PROJECT_FILES + 1}")

    restored: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            fail("WAI School source manifest contains an invalid file entry")
        rel = normalize_manifest_file_path(str(item.get("path") or ""))
        if rel in seen:
            fail(f"WAI School source manifest contains a duplicate file: {rel}")
        data = decode_manifest_file(str(item.get("contentBase64") or ""), rel)
        total_bytes += len(data)
        if total_bytes > MAX_PROJECT_TOTAL_FILE_BYTES + MAX_PROJECT_FILE_BYTES:
            fail("WAI School source manifest is too large")
        seen.add(rel)
        restored.append((rel, data))

    if "index.html" not in seen:
        fail("WAI School source manifest does not contain index.html")

    return restored, total_bytes


def restore_project_source(
    slug: str,
    project_url: str,
    publish_token: str,
    target: Path,
    force: bool = False,
    edit_token: str = "",
) -> dict:
    validate_restore_target(target, force)
    manifest = fetch_source_manifest(slug, publish_token, edit_token)
    restored, total_bytes = prepare_manifest_files(manifest)
    target.mkdir(parents=True, exist_ok=True)
    if force:
        clear_restore_target(target)

    restored_paths = []
    for rel, data in restored:
        out_path = target / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        restored_paths.append(rel)

    restored_state = {
        "slug": slug,
        "url": project_url,
        "updatedAt": manifest.get("updatedAt"),
        "currentRevision": manifest.get("revision"),
    }
    if publish_token:
        restored_state["owned"] = True
    else:
        restored_state["editToken"] = edit_token
    if not save_state(state_path(target), restored_state):
        fail("WAI School restored the files but could not save a safe update state.")
    return {
        "ok": True,
        "restored": True,
        "slug": slug,
        "title": manifest.get("title") or "Мой проект",
        "currentRevision": manifest.get("revision"),
        "dir": str(target),
        "fileCount": len(restored_paths),
        "bytes": total_bytes,
        "projectStateSaved": True,
    }


def restore_live_conflict_copy(
    slug: str,
    project_url: str,
    target: Path,
    revision: int,
    publish_token: str = "",
    edit_token: str = "",
) -> Path:
    project_root = target if target.is_dir() else target.parent
    suffix = f"v{revision}" if revision > 0 else "current"
    live_target = project_root.with_name(f"{project_root.name}-live-{suffix}")
    restore_project_source(slug, project_url, publish_token, live_target, False, edit_token)
    return live_target


def publish(
    html: str,
    title: str,
    state: dict,
    files: list[dict],
    request_id: str,
    publish_token: str = "",
    create_token: str = "",
) -> dict:
    payload_data = {
        "html": html,
        "title": title,
        "files": files,
        "source": "claude-code-publisher",
        "skillVersion": SKILL_VERSION,
        "clientRequestId": request_id,
    }
    if publish_token:
        payload_data["publishToken"] = publish_token
    if state.get("slug"):
        payload_data["slug"] = state["slug"]
    if state.get("editToken"):
        payload_data["editToken"] = state["editToken"]
    if state.get("currentRevision"):
        payload_data["baseRevision"] = state["currentRevision"]
    if create_token:
        payload_data["createToken"] = create_token

    payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"content-type": "application/json", "user-agent": f"wai-school-publish/{SKILL_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as res:
            body = res.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        data = {}
        try:
            data = json.loads(body)
            message = data.get("error") or body
        except json.JSONDecodeError:
            message = body or str(e)
        if re.search(r"host.*not.*allow|allowlist|egress|blocked", message, re.I):
            fail("Claude code environment cannot reach wai.school.", NETWORK_FIX_RU)
        raise ProjectPublishHttpError(e.code, str(data.get("code") or "publish_rejected"), str(message), data)
    except urllib.error.URLError as e:
        message = str(e.reason if hasattr(e, "reason") else e)
        if re.search(r"host.*not.*allow|allowlist|egress|blocked|name or service not known", message, re.I):
            fail("Claude code environment cannot reach wai.school.", NETWORK_FIX_RU)
        fail(f"Could not reach WAI School publish server: {e}", NETWORK_FIX_RU)


def run_doctor(target: Path) -> None:
    """One command a mentor can run to see what works: Python, files, network."""
    report: list[str] = []
    ok = True

    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 8):
        ok = False
        report.append(f"✗ Python {version} — слишком старый, нужен 3.8+. Поставь свежий Python с python.org.")
    else:
        report.append(f"✓ Python {version}")

    report.append(f"✓ Publisher {SKILL_VERSION} ({Path(__file__).resolve()})")

    if target.exists() and target.is_dir():
        state_file = state_path(target)
        if state_file.exists():
            state = load_state(state_file)
            report.append(
                f"✓ Память проекта найдена: ссылка уже есть (slug {state.get('slug')}, версия {state.get('currentRevision')})"
            )
        else:
            report.append("· Памяти проекта в этой папке нет — первая публикация создаст новую ссылку.")

    # The probe must satisfy even the strict legacy quality gate, so the doctor
    # works against older server builds too.
    probe_html = (
        "<!doctype html><html lang=\"ru\"><head><title>Проверка WAI School</title>"
        "<style>body{background:linear-gradient(#20242c,#3b4252);color:#fff;font-family:sans-serif}"
        ".card{padding:24px;transition:transform .2s}</style></head>"
        "<body><main class=\"card\"><h1>Проверка публикации WAI School</h1>"
        "<p>Это тестовая страница доктора: нажми кнопку и посмотри результат выбора.</p>"
        "<button onclick=\"go()\">Сделать выбор</button><div id=\"r\"></div>"
        "<script>function go(){document.getElementById('r').textContent='Результат: победа, проверка пройдена!';}"
        "</script></main></body></html>"
    )
    probe = {
        "html": probe_html,
        "title": "doctor",
        "source": "claude-code-publisher",
        "skillVersion": SKILL_VERSION,
        "validateOnly": True,
    }
    try:
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(probe).encode("utf-8"),
            headers={"content-type": "application/json", "user-agent": f"wai-school-publish/{SKILL_VERSION}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
        if data.get("ok"):
            report.append(f"✓ Сервер wai.school отвечает (проверка без публикации прошла, version {data.get('version')})")
        else:
            ok = False
            report.append(f"✗ Сервер ответил без ok: {data}")
    except urllib.error.HTTPError as error:
        ok = False
        body = error.read().decode("utf-8", errors="replace")[:300]
        report.append(f"✗ Сервер wai.school отклонил тестовую проверку (HTTP {error.code}): {body}")
        report.append("  → Соединение есть, но сервер отвечает ошибкой. Перешли этот вывод в чат школы.")
    except Exception as error:  # noqa: BLE001 — any transport failure reads the same for a mentor
        ok = False
        report.append(f"✗ Нет соединения с wai.school: {error}")
        report.append("  → Проверь интернет. Если Claude пишет про запрет сети — публикуй через wai.school/student.")

    for line in report:
        print(line, file=sys.stderr)
    print(json.dumps({"ok": ok, "doctor": report}, ensure_ascii=False))
    raise SystemExit(0 if ok else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a static project to wai.school")
    parser.add_argument("--dir", default=".", help="Project folder or HTML file to publish")
    parser.add_argument("--publish-token", default="", help="Scoped WAI School child publish token")
    parser.add_argument("--project", default="", help="Existing wai.school/project/... URL or slug to update with a publish token")
    parser.add_argument("--base-revision", type=int, default=0, help="Known current revision of an existing project")
    parser.add_argument("--expect-url", default="", help="Fail unless the server returns this exact public project URL")
    parser.add_argument("--require-updated", action="store_true", help="Fail unless the server confirms updated: true")
    parser.add_argument("--restore", action="store_true", help="Restore an existing owned project into --dir before editing")
    parser.add_argument("--force", action="store_true", help="Allow --restore to replace files in the target folder")
    parser.add_argument("--dry-run", action="store_true", help="Bundle and validate locally, but do not upload")
    parser.add_argument("--doctor", action="store_true", help="Check Python, publisher files, and wai.school connectivity")
    args = parser.parse_args()

    target = Path(args.dir).expanduser().resolve()
    if args.doctor:
        run_doctor(target)
    publish_token = (args.publish_token or os.environ.get("WAI_SCHOOL_PUBLISH_TOKEN", "")).strip()
    explicit_slug = project_arg_to_slug(args.project)
    expected_url = (args.expect_url or "").strip()
    if expected_url:
        project_arg_to_slug(expected_url)
    if args.restore:
        if not explicit_slug:
            fail(
                "Restoring a project needs --project with a wai.school/project/... URL or slug.",
                "Добавь --project https://wai.school/project/... — какую ссылку восстанавливаем.",
            )
        restore_state = load_state(state_path(target)) if target.exists() else {}
        edit_token = str(restore_state.get("editToken") or "") if restore_state.get("slug") == explicit_slug else ""
        result = restore_project_source(
            explicit_slug,
            args.project or f"https://wai.school/project/{explicit_slug}",
            publish_token,
            target,
            args.force,
            edit_token,
        )
        print(json.dumps(result, ensure_ascii=False))
        return

    if not target.exists():
        fail(f"Path does not exist: {target}", "Проверь путь в --dir: такой папки или файла нет.")

    html, title, warnings, files = bundle_project(target)
    state_file, state = load_project_state(target)
    if explicit_slug:
        if state.get("slug") != explicit_slug:
            state = {"slug": explicit_slug}
        else:
            state["slug"] = explicit_slug
    if args.base_revision:
        if args.base_revision <= 0:
            fail("--base-revision must be a positive integer")
        state["currentRevision"] = args.base_revision
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "title": title,
                    "bytes": len(html.encode("utf-8")),
                    "fileCount": len(files),
                    "fileBytes": sum(int(file.get("byteLength") or 0) for file in files),
                    "warnings": warnings,
                    "knownProject": state.get("slug"),
                    "knownRevision": state.get("currentRevision"),
                    "hasPublishToken": bool(publish_token),
                },
                ensure_ascii=False,
            )
        )
        return
    if state.get("slug") and not state.get("editToken") and not publish_token:
        fail(
            "Updating an existing project by slug needs --publish-token or a saved edit token.",
            "Чтобы обновить эту ссылку, нужен publish-token из кабинета ребёнка или память проекта в этой папке. Позови ментора.",
        )
    if state.get("slug") and not state.get("currentRevision"):
        live_target = restore_live_conflict_copy(
            state["slug"],
            args.project or f"https://wai.school/project/{state['slug']}",
            target,
            0,
            publish_token,
            str(state.get("editToken") or ""),
        )
        fail(
            f"This project state predates safe versions. The current live source was restored to {live_target}.",
            f"Живая версия проекта скопирована в папку {live_target}. Сравни её со своей, перенеси нужные изменения туда и публикуй из неё.",
        )

    request_id = str(state.get("pendingRequestId") or uuid.uuid4())
    create_token = ""
    if not state.get("slug"):
        create_token = str(state.get("createToken") or uuid.uuid4().hex)
    try:
        save_pending_state(state_file, state, request_id, create_token)
    except OSError as error:
        fail(f"Cannot save safe publish state at {state_file}: {error}", "Не получилось записать файл памяти проекта. Проверь права на папку.")
    try:
        result = publish(html, title, state, files, request_id, publish_token, create_token)
    except ProjectPublishHttpError as error:
        if error.code == "project_request_reused":
            clear_pending_state(state_file, state)
        if error.code == "project_revision_conflict":
            clear_pending_state(state_file, state)
            revision = error.data.get("currentRevision")
            if isinstance(revision, int) and revision > 0:
                live_target = restore_live_conflict_copy(
                    state["slug"],
                    args.project or f"https://wai.school/project/{state['slug']}",
                    target,
                    revision,
                    publish_token,
                    str(state.get("editToken") or ""),
                )
                fail(
                    f"Server rejected publish ({error.status}): {error.message} "
                    f"The live version {revision} was restored to {live_target}.",
                    f"Проект уже обновили в другом месте. Живая версия скопирована в {live_target} — сравни, перенеси свои изменения туда и публикуй из неё.",
                )
        fail(f"Server rejected publish ({error.status}): {error.message}")
    if result.get("ok"):
        if expected_url and result.get("url") != expected_url:
            fail(f"Publish returned a different URL; expected {expected_url}, got {result.get('url') or ''}")
        if args.require_updated and result.get("updated") is not True:
            fail("Publish did not update an existing project; expected updated: true")
        try:
            state_saved = save_state(state_file, result)
        except OSError as error:
            fail(
                f"Project published at {result.get('url') or 'an unknown URL'}, but its update state could not be saved at "
                f"{state_file}: {error}. Fix access to this file and run the same command again.",
            )
        if not state_saved:
            fail(
                f"Project published at {result.get('url') or 'an unknown URL'}, but the server response did not contain "
                "a safe update token or revision. Run the same command again; do not create another project.",
            )
        result["projectStateSaved"] = True
        if result.get("editToken"):
            del result["editToken"]
    server_warnings = result.get("warnings")
    result["warnings"] = warnings + (server_warnings if isinstance(server_warnings, list) else [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
