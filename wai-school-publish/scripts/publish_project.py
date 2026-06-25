#!/usr/bin/env python3
"""Publish a static HTML project and local assets to wai.school.

No third-party dependencies: this script is meant to run inside Claude.ai code
execution after the WAI School Publish skill is installed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("WAI_SCHOOL_PUBLISH_ENDPOINT", "https://wai.school/api/projects/publish")
SKILL_VERSION = "2026-06-25.2"
MAX_INLINE_ASSET_BYTES = 2_000_000
MAX_PROJECT_FILES = 160
MAX_PROJECT_FILE_BYTES = 10_000_000
MAX_PROJECT_TOTAL_FILE_BYTES = 50_000_000
STATE_FILE_NAME = ".wai-school-project.json"

TEXT_EXTENSIONS = {".html", ".htm", ".css", ".gltf", ".js", ".mjs", ".txt", ".json"}
VISUAL_ASSET_EXTENSIONS = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
AUDIO_ASSET_EXTENSIONS = {".flac", ".m4a", ".mid", ".midi", ".mp3", ".ogg", ".wav"}
MODEL_ASSET_EXTENSIONS = {".bin", ".glb", ".gltf", ".wasm"}
PROJECT_FILE_EXTENSIONS = {
    ".avif",
    ".bin",
    ".bmp",
    ".css",
    ".flac",
    ".gif",
    ".glb",
    ".gltf",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mid",
    ".midi",
    ".mjs",
    ".mp3",
    ".mp4",
    ".ogg",
    ".otf",
    ".png",
    ".ttf",
    ".txt",
    ".wav",
    ".webm",
    ".webmanifest",
    ".webp",
    ".woff",
    ".woff2",
    ".wasm",
}
INLINE_ASSET_EXTENSIONS = (
    "png",
    "jpe?g",
    "gif",
    "webp",
    "avif",
    "bmp",
    "flac",
    "mid",
    "midi",
    "mp3",
    "wav",
    "ogg",
    "m4a",
    "woff2?",
    "ttf",
    "otf",
)
IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
SECRET_FILE_NAMES = {".env", ".env.local", ".env.production"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I),
    re.compile(r"\b(?:OPENAI|ANTHROPIC|GEMINI|NOTION|RESEND|WAIPAY)_[A-Z0-9_]*\s*[:=]", re.I),
]
FORBIDDEN_RUNTIME_ERROR = (
    "Project quality check failed: external network, external assets, browser storage, "
    "service workers, cookies, IndexedDB, and Cache API are not allowed. "
    "Keep every asset local inside the project folder."
)
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
    re.compile(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie|navigator\.serviceWorker|caches)\b", re.I),
]
HTML_LOCAL_REF_RE = re.compile(
    r"""(?<![-:\w])(?:src|href|poster)\s*=\s*(?:(['"])(?P<quoted>[^'"]+)\1|(?P<unquoted>[^\s"'=<>`]+))""",
    re.I,
)
CSS_URL_RE = re.compile(r"""url\(\s*(?P<quote>['"]?)(?P<url>[^)'"]+)(?P=quote)\s*\)""", re.I)
SCRIPT_BLOCK_RE = re.compile(r"""<script\b[^>]*>(?P<js>[\s\S]*?)</script>""", re.I)
STYLE_BLOCK_RE = re.compile(r"""<style\b[^>]*>(?P<css>[\s\S]*?)</style>""", re.I)
HTML_TAG_RE = re.compile(r"""<[^>]+>""")
JS_LOCAL_REF_PATTERNS = [
    re.compile(r"""\bfetch\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*(?:[,)]|$)"""),
    re.compile(r"""\bimport\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*\)"""),
    re.compile(r"""\bnew\s+URL\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*,"""),
    re.compile(r"""\b(?:import|export)\s+(?:[^'"]+\s+from\s+)?(['"])(?P<url>[^'"]+)\1"""),
    re.compile(r"""\bnew\s+(?:Worker|SharedWorker)\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*(?:[,)]|$)"""),
    re.compile(r"""\bnew\s+(?:Audio|Image)\s*\(\s*(['"`])(?P<url>[^'"`]+)\1\s*(?:[,)]|$)"""),
    re.compile(r"""\b(?:src|href)\s*=\s*(['"`])(?P<url>[^'"`]+)\1"""),
    re.compile(r"""\bsetAttribute\s*\(\s*['"](?:src|href|poster)['"]\s*,\s*(['"`])(?P<url>[^'"`]+)\1"""),
]
GLTF_URI_RE = re.compile(r"""["']uri["']\s*:\s*["'](?P<url>[^"']+)["']""", re.I)
ACTION_SIGNAL_RE = re.compile(
    r"""\b(addEventListener|onclick|onpointer|onmouse|ontouch|onkey|onsubmit|onchange)\b|<\s*(button|input|select|textarea)\b""",
    re.I,
)
FEEDBACK_SIGNAL_RE = re.compile(
    r"""\b(textContent|innerHTML|classList|dataset|setAttribute|appendChild|requestAnimationFrame|score|state|result|progress|level|energy|impact|meter|win|won|lose|lost)\b""",
    re.I,
)
VISUAL_SIGNAL_RE = re.compile(
    r"""<\s*canvas\b|\b(radial-gradient|linear-gradient|animation|transition|transform|box-shadow|grid|scene|card|panel|choice|preview|particles?)\b""",
    re.I,
)
GAME_SIGNAL_RE = re.compile(
    r"""<\s*canvas\b|\b(player|enemy|enemies|boss|collision|particle|sprite|tilemap|physics|keydown|keyup|pointerlock)\b""",
    re.I,
)
SPORTS_GAME_SIGNAL_RE = re.compile(
    r"""\b(soccer|football|goal|goals|ball|keeper|striker|stadium|arena|match|scoreboard|field|monster soccer|футбол|гол|мяч|ворот|вратар|стадион|арен|матч|табло|поле)\b""",
    re.I,
)
GAME_INPUT_RE = re.compile(r"""\b(keydown|keyup|pointer|mousemove|touch|click|addEventListener)\b""", re.I)
GOAL_SIGNAL_RE = re.compile(
    r"""\b(goal|mission|target|collect|find|escape|open|finish|objective|quest|keys?|cores?|coins?|цель|миссия|собери|найди|побед|выход)\b""",
    re.I,
)
RISK_SIGNAL_RE = re.compile(
    r"""\b(risk|danger|enemy|hazard|trap|boss|guard|damage|health|energy|timer|timeLeft|lives|lose|lost|опасн|враг|ловуш|босс|охран|урон|жизн|таймер|проигр)\b""",
    re.I,
)
PROGRESS_SIGNAL_RE = re.compile(
    r"""<\s*progress\b|\b(progress|level|stage|scene|score|combo|meter|energy|inventory|unlock|phase|state|result|impact|уров|сч[её]т|прогресс|результат|этап|режим)\b""",
    re.I,
)
END_STATE_SIGNAL_RE = re.compile(
    r"""\b(win|won|lose|lost|complete|completed|finish|final|ending|result|success|fail|game over|try again|restart|побед|финал|конец|готово|проигр|снова)\b""",
    re.I,
)
EFFECT_SIGNAL_RE = re.compile(
    r"""\b(requestAnimationFrame|AudioContext|Oscillator|particle|particles|burst|shake|glow|confetti|animation|transition|transform|parallax|trail|sound|audio|свет|звук|частиц)\b""",
    re.I,
)
GAME_PRESENTATION_RE = re.compile(
    r"""\b(drawImage|createPattern|createLinearGradient|createRadialGradient|shadowBlur|globalCompositeOperation|Path2D|bezierCurveTo|quadraticCurveTo|clip|rotate|scale|translate|camera|viewport|parallax|tilemap|tile|tiles|sprite|spritesheet|atlas|frameIndex|layer|backgroundLayer|foreground|minimap|lighting|screenShake|shake|particle|particles|trail|glow|confetti|texture|biome|prop|props)\b""",
    re.I,
)
GAME_SYSTEM_RE = re.compile(
    r"""\b(enemy|enemies|drone|drones|guard|hazard|boss|phase|wave|waves|spawn|ai|behavior|patrol|path|pathfinding|cooldown|ability|dash|attack|health|damage|shield|inventory|quest|dialog|cutscene|checkpoint|room|level|stage|unlock|timer|objective|mission|collision|physics|camera|parallax|minimap|gate|core|cores|collectible|relic|energy)\b""",
    re.I,
)
PRIMITIVE_SHAPE_SIGNAL_RE = re.compile(
    r"""\b(border-radius\s*:\s*(?:50%|999px)|ctx\.arc|\.arc\s*\(|drawMonster|drawCircle|circle|ellipse|rounded-full|прост(?:ые|ыми)\s+фигур)\b""",
    re.I,
)
SPORTS_ARENA_RE = re.compile(
    r"""\b(stadium|crowd|stands|keeper phases?|goal replay|replay|confetti|charged shots?|ball physics|goal net|announcer|scoreboard pulse|goal camera|slow motion|стадион|трибун|толп|репле[йя]|конфетти|заряженн|физик[аи]\s+мяч|сетка|табло)\b""",
    re.I,
)
MULTIFILE_SIGNAL_RE = re.compile(
    r"""\b(levels?/|data/|assets?/|fetch\s*\(\s*['"`][^'"`]+\.json|import\s+|export\s+|class\s+\w+)\b""",
    re.I,
)
CHOICE_SIGNAL_RE = re.compile(r"""\b(choice|choices|selected|option|quiz|question|answer|card|reveal|filter|выбор|вариант|вопрос|ответ|карточ)\b""", re.I)


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


def validate_no_forbidden_runtime(text: str) -> None:
    for pattern in FORBIDDEN_RUNTIME_PATTERNS:
        if pattern.search(text):
            fail(FORBIDDEN_RUNTIME_ERROR)


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


def resolve_local_reference(base: Path, raw_url: str, project_root: Path, source: Path) -> None:
    value = (raw_url or "").strip()
    if is_external_or_virtual_ref(value):
        return
    if "\0" in value or "${" in value or "{" in value or "}" in value:
        return

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return
    ref_path = urllib.parse.unquote(parsed.path or "").strip()
    if not ref_path:
        return
    if ref_path.startswith("/"):
        fail(f"Use relative paths inside the project; {source.relative_to(project_root)} references {value}")

    candidate = (base / ref_path).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        fail(f"Local project reference escapes the project folder from {source.relative_to(project_root)}: {value}")
    if not candidate.exists() or not candidate.is_file():
        fail(f"Missing local project file referenced from {source.relative_to(project_root)}: {value}")


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


def html_reference_url(match: re.Match[str]) -> str:
    return match.group("quoted") or match.group("unquoted") or ""


def validate_js_references(source: Path, text: str, project_root: Path) -> None:
    for pattern in JS_LOCAL_REF_PATTERNS:
        for match in pattern.finditer(text):
            resolve_local_reference(source.parent, match.group("url"), project_root, source)


def validate_local_references(root_or_file: Path, html_path: Path) -> None:
    project_root = html_path.parent if root_or_file.is_file() else root_or_file

    html = read_text(html_path)
    for match in HTML_LOCAL_REF_RE.finditer(html):
        resolve_local_reference(html_path.parent, html_reference_url(match), project_root, html_path)
    for match in CSS_URL_RE.finditer(html):
        resolve_local_reference(html_path.parent, match.group("url"), project_root, html_path)
    validate_js_references(html_path, html, project_root)

    if root_or_file.is_file():
        referenced: set[Path] = set()
        collect_referenced_project_files(html_path, project_root, referenced, set())
        candidate_files = sorted(referenced)
    else:
        candidate_files = project_files(project_root)

    for path in candidate_files:
        suffix = path.suffix.lower()
        if suffix == ".css":
            text = read_text(path)
            for match in CSS_URL_RE.finditer(text):
                resolve_local_reference(path.parent, match.group("url"), project_root, path)
        elif suffix in {".js", ".mjs"}:
            text = read_text(path)
            validate_js_references(path, text, project_root)


def visible_text_from_html(html: str) -> str:
    text = SCRIPT_BLOCK_RE.sub(" ", html)
    text = STYLE_BLOCK_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"&[a-z0-9#]+;", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def project_quality_text(root_or_file: Path, html_path: Path) -> tuple[str, str]:
    project_root = html_path.parent if root_or_file.is_file() else root_or_file
    html = read_text(html_path)
    chunks = [html]
    if root_or_file.is_file():
        referenced: set[Path] = set()
        collect_referenced_project_files(html_path, project_root, referenced, set())
        candidate_files = sorted(referenced)
    else:
        candidate_files = project_files(project_root)
    for path in candidate_files:
        if path.resolve() == html_path.resolve():
            continue
        if path.suffix.lower() in {".css", ".js", ".mjs", ".json", ".txt"}:
            chunks.append(read_text(path))
    return html, "\n".join(chunks)


def unique_signal_count(pattern: re.Pattern[str], text: str) -> int:
    return len({match.group(0).lower() for match in pattern.finditer(text)})


def project_asset_counts(root_or_file: Path, html_path: Path) -> dict[str, int]:
    project_root = html_path.parent if root_or_file.is_file() else root_or_file
    referenced: set[Path] = set()
    collect_referenced_project_files(html_path, project_root, referenced, set())
    candidate_files = sorted(referenced)

    counts = {"visual": 0, "audio": 0, "model": 0}
    for path in candidate_files:
        suffix = path.suffix.lower()
        if suffix in VISUAL_ASSET_EXTENSIONS:
            counts["visual"] += 1
        elif suffix in AUDIO_ASSET_EXTENSIONS:
            counts["audio"] += 1
        elif suffix in MODEL_ASSET_EXTENSIONS:
            counts["model"] += 1
    return counts


def validate_project_quality(root_or_file: Path, html_path: Path) -> None:
    html, all_text = project_quality_text(root_or_file, html_path)
    asset_counts = project_asset_counts(root_or_file, html_path)
    validate_no_forbidden_runtime(all_text)
    visible_text = visible_text_from_html(html)
    has_canvas = bool(re.search(r"<\s*canvas\b", html, flags=re.I))
    has_action = bool(ACTION_SIGNAL_RE.search(all_text))
    has_feedback = bool(FEEDBACK_SIGNAL_RE.search(all_text))
    has_visual = bool(VISUAL_SIGNAL_RE.search(all_text))
    looks_like_game = bool(GAME_SIGNAL_RE.search(all_text))
    looks_like_sports_game = bool(SPORTS_GAME_SIGNAL_RE.search(all_text)) and (
        looks_like_game or has_canvas or "requestAnimationFrame" in all_text
    )
    weak_alert_demo = bool(re.search(r"\balert\s*\(", all_text)) and not (
        re.search(r"\b(textContent|innerHTML|classList|requestAnimationFrame)\b", all_text)
    )
    score_only_demo = bool(re.search(r"\b(score|points?|сч[её]т|очки)\b", all_text, flags=re.I)) and not (
        GOAL_SIGNAL_RE.search(all_text) or RISK_SIGNAL_RE.search(all_text) or END_STATE_SIGNAL_RE.search(all_text)
    )

    if len(visible_text) < 24 and not has_canvas:
        fail("Project quality check failed: add a real first screen with clear text, action, and result before publishing.")
    if weak_alert_demo:
        fail("Project quality check failed: replace alert-only demo with visible on-page feedback before publishing.")
    if score_only_demo:
        fail(
            "Project quality check failed: games need more than a primitive demo. "
            "Add a goal, risk or challenge, progress/state, effects, and a win/loss/result moment before publishing."
        )
    if not has_action:
        fail("Project quality check failed: add a user action such as a button, click, keyboard, pointer, input, or choice before publishing.")
    if not has_feedback:
        fail("Project quality check failed: add visible feedback, state, progress, score, result, or scene change before publishing.")
    if not has_visual:
        fail("Project quality check failed: add a stronger visual surface: canvas, scene, cards, panels, grid, animation, transition, or styled result.")
    if (looks_like_game or has_canvas or looks_like_sports_game) and (has_canvas or "requestAnimationFrame" in all_text):
        if "requestAnimationFrame" not in all_text:
            fail("Project quality check failed: canvas games need a requestAnimationFrame game loop before publishing.")
        if not GAME_INPUT_RE.search(all_text):
            fail("Project quality check failed: games need keyboard, pointer, click, or touch input before publishing.")

    quality_layers = {
        "goal": bool(GOAL_SIGNAL_RE.search(all_text)),
        "risk": bool(RISK_SIGNAL_RE.search(all_text)),
        "progress": bool(PROGRESS_SIGNAL_RE.search(all_text)),
        "end": bool(END_STATE_SIGNAL_RE.search(all_text)),
        "effect": bool(EFFECT_SIGNAL_RE.search(all_text)),
        "multi_file": bool(MULTIFILE_SIGNAL_RE.search(all_text)),
        "choice": bool(CHOICE_SIGNAL_RE.search(all_text)),
    }
    layer_count = sum(quality_layers.values())
    if looks_like_game or has_canvas or looks_like_sports_game:
        if layer_count < 3:
            fail(
                "Project quality check failed: games need more than a primitive demo. "
                "Add a goal, risk or challenge, progress/state, effects, and a win/loss/result moment before publishing."
            )
        system_depth = unique_signal_count(GAME_SYSTEM_RE, all_text)
        presentation_depth = unique_signal_count(GAME_PRESENTATION_RE, all_text)
        rich_asset_count = asset_counts["visual"] + asset_counts["audio"] + asset_counts["model"]
        if system_depth < 5:
            fail(
                "Project quality check failed: games need deeper mechanics. "
                "Add systems such as enemies, hazards, phases, abilities, camera, waves, levels, health, cooldowns, or objectives."
            )
        if presentation_depth < 6 and rich_asset_count < 2:
            fail(
                "Project quality check failed: game visuals are too flat. "
                "Add camera/layers, procedural sprites or image assets, lighting, parallax, tiles/props, animation frames, shadows, and stronger effects before publishing."
            )
        if (
            looks_like_sports_game
            and PRIMITIVE_SHAPE_SIGNAL_RE.search(all_text)
            and unique_signal_count(SPORTS_ARENA_RE, all_text) < 2
            and rich_asset_count < 2
        ):
            fail(
                "Project quality check failed: sports and arena games need more than primitive shapes. "
                "Add stadium/crowd, ball or shot physics, keeper phases, replay/confetti, and stronger arena feedback before publishing."
            )
    elif layer_count < 2:
        fail(
            "Project quality check failed: pages and mini apps need at least two meaningful layers, "
            "such as choices plus result, sections plus interaction, or input plus feedback before publishing."
        )


def inline_text_assets(html: str, html_path: Path) -> tuple[str, list[str]]:
    base = html_path.parent
    warnings: list[str] = []

    def inline_css_urls(css: str, css_base: Path) -> str:
        def url_repl(match: re.Match[str]) -> str:
            quote = match.group("quote") or ""
            raw_url = match.group("url").strip()
            asset = local_asset_path(css_base, raw_url)
            if not asset:
                return match.group(0)
            size = asset.stat().st_size
            if size > MAX_INLINE_ASSET_BYTES:
                warnings.append(f"CSS asset too large to inline: {asset.name}")
                return match.group(0)
            mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
            return f"url({quote}data:{mime};base64,{encoded}{quote})"

        asset_ext_pattern = "|".join(INLINE_ASSET_EXTENSIONS)
        return re.sub(
            rf"url\(\s*(?P<quote>['\"]?)(?P<url>[^)'\"]+\.({asset_ext_pattern})(?:[?#][^'\")]*)?)(?P=quote)\s*\)",
            url_repl,
            css,
            flags=re.I,
        )

    def style_repl(match: re.Match[str]) -> str:
        href = match.group("href")
        asset = local_asset_path(base, href)
        if not asset:
            warnings.append(f"CSS not inlined: {href}")
            return match.group(0)
        return f"<style>\n{inline_css_urls(read_text(asset), asset.parent)}\n</style>"

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

    def inline_style_block(match: re.Match[str]) -> str:
        return f"{match.group('open')}{inline_css_urls(match.group('css'), base)}{match.group('close')}"

    html = re.sub(
        r"(?P<open><style\b[^>]*>)(?P<css>[\s\S]*?)(?P<close></style>)",
        inline_style_block,
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

    asset_ext_pattern = "|".join(INLINE_ASSET_EXTENSIONS + ("mp4", "webm"))
    html = re.sub(
        rf"(?P<prefix>\b(?:src|href|poster)=['\"])(?P<src>[^'\"]+\.(?:{asset_ext_pattern})(?:[?#][^'\"]*)?)(?P<suffix>['\"])",
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


def safe_rel_path(path: Path, base: Path) -> str:
    rel = path.relative_to(base).as_posix()
    parts = rel.split("/")
    if (
        not rel
        or rel.startswith("/")
        or len(rel) > 180
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
    ):
        fail(f"Refusing unsafe project file path: {rel}")
    return rel


def add_referenced_project_file(base: Path, raw_url: str, project_root: Path, out: set[Path], seen: set[Path]) -> None:
    candidate = local_reference_candidate(base, raw_url, project_root)
    if not candidate:
        return
    out.add(candidate)
    collect_referenced_project_files(candidate, project_root, out, seen)


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
        for match in HTML_LOCAL_REF_RE.finditer(text):
            add_referenced_project_file(source.parent, html_reference_url(match), project_root, out, seen)
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
        for pattern in JS_LOCAL_REF_PATTERNS:
            for match in pattern.finditer(js_text):
                add_referenced_project_file(source.parent, match.group("url"), project_root, out, seen)


def project_file_manifest(root_or_file: Path, html_path: Path) -> list[dict]:
    base = html_path.parent
    files: list[dict] = []
    total_bytes = 0
    if root_or_file.is_file():
        referenced: set[Path] = set()
        collect_referenced_project_files(html_path, base, referenced, set())
        candidate_files = sorted(referenced)
    else:
        candidate_files = project_files(base)

    for path in candidate_files:
        if path.resolve() == html_path.resolve():
            continue
        suffix = path.suffix.lower()
        if suffix not in PROJECT_FILE_EXTENSIONS:
            fail(f"Unsupported project file type: {safe_rel_path(path, base)}")
        size = path.stat().st_size
        if size <= 0:
            continue
        if size > MAX_PROJECT_FILE_BYTES:
            fail(f"Project file is too large: {safe_rel_path(path, base)}")
        total_bytes += size
        if total_bytes > MAX_PROJECT_TOTAL_FILE_BYTES:
            fail(f"Project files are too large; limit is {MAX_PROJECT_TOTAL_FILE_BYTES} bytes")
        rel = safe_rel_path(path, base)
        files.append(
            {
                "path": rel,
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "contentBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "byteLength": size,
            }
        )
        if len(files) > MAX_PROJECT_FILES:
            fail(f"Project has too many files; limit is {MAX_PROJECT_FILES}")
    return sorted(files, key=lambda file: file["path"])


def bundle_project(root_or_file: Path) -> tuple[str, str, list[str], list[dict]]:
    html_path = choose_html(root_or_file)
    root = html_path.parent if root_or_file.is_file() else root_or_file
    scan_for_secrets(root)
    validate_local_references(root_or_file, html_path)
    files = project_file_manifest(root_or_file, html_path)
    validate_project_quality(root_or_file, html_path)

    html = read_text(html_path)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else html_path.stem
    return html, title[:80] or "Мой проект", [], files


def state_path(root_or_file: Path) -> Path:
    return (root_or_file if root_or_file.is_dir() else root_or_file.parent) / STATE_FILE_NAME


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    slug = str(data.get("slug") or "")
    edit_token = str(data.get("editToken") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", slug):
        return {}
    if re.fullmatch(r"[A-Za-z0-9_-]{24,128}", edit_token):
        return {"slug": slug, "editToken": edit_token}
    return {"slug": slug}


def save_state(path: Path, result: dict) -> bool:
    slug = str(result.get("slug") or "")
    edit_token = str(result.get("editToken") or "")
    if not slug:
        return False
    if not edit_token and not result.get("owned"):
        return False
    state = {
        "slug": slug,
        "url": result.get("url"),
        "updatedAt": result.get("updatedAt"),
    }
    if edit_token:
        state["editToken"] = edit_token
    if result.get("owned"):
        state["owned"] = True
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


def project_arg_to_slug(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", raw):
        return raw
    match = re.search(r"(?:^|/)project/([a-z0-9][a-z0-9-]{2,80})(?:[/?#]|$)", raw)
    if match:
        return match.group(1)
    fail("Project must be a wai.school/project/... URL or a project slug.")


def source_endpoint_for_slug(slug: str) -> str:
    parsed = urllib.parse.urlparse(ENDPOINT)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/projects/publish"):
        source_path = path[: -len("/publish")] + f"/source/{slug}"
    elif path.endswith("/publish"):
        source_path = path[: -len("/publish")] + f"/source/{slug}"
    else:
        source_path = f"/api/projects/source/{slug}"
    return urllib.parse.urlunparse(parsed._replace(path=source_path, params="", query="", fragment=""))


def fetch_source_manifest(slug: str, publish_token: str) -> dict:
    if not publish_token:
        fail("Restoring an existing project needs --publish-token.")
    payload = json.dumps({"publishToken": publish_token}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        source_endpoint_for_slug(slug),
        data=payload,
        headers={"content-type": "application/json", "user-agent": f"wai-school-publish/{SKILL_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read().decode("utf-8")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            message = data.get("error") or body
        except json.JSONDecodeError:
            message = body or str(e)
        fail(f"Server rejected source restore ({e.code}): {message}")
    except urllib.error.URLError as e:
        fail(f"Could not reach WAI School source server: {e}")
    except json.JSONDecodeError:
        fail("WAI School source server returned invalid JSON")

    if not data.get("ok") or data.get("sourceManifest") != "wai-school-project-v1":
        fail("WAI School source server did not return a valid project manifest")
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
        fail("For safety, --force restore only works with a folder named wai-school-project...")
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
            fail("Restore target folder is not empty. Use a clean folder or pass --force.")


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


def restore_project_source(slug: str, project_url: str, publish_token: str, target: Path, force: bool = False) -> dict:
    validate_restore_target(target, force)
    manifest = fetch_source_manifest(slug, publish_token)
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

    save_state(
        state_path(target),
        {
            "slug": slug,
            "url": project_url,
            "updatedAt": manifest.get("updatedAt"),
            "owned": True,
        },
    )
    return {
        "ok": True,
        "restored": True,
        "slug": slug,
        "title": manifest.get("title") or "Мой проект",
        "dir": str(target),
        "fileCount": len(restored_paths),
        "bytes": total_bytes,
        "projectStateSaved": True,
    }


def publish(html: str, title: str, state: dict, files: list[dict], publish_token: str = "") -> dict:
    payload_data = {
        "html": html,
        "title": title,
        "files": files,
        "source": "claude-ai-publisher",
        "skillVersion": SKILL_VERSION,
    }
    if publish_token:
        payload_data["publishToken"] = publish_token
    if state.get("slug"):
        payload_data["slug"] = state["slug"]
    if state.get("editToken"):
        payload_data["editToken"] = state["editToken"]

    payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
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
    parser.add_argument("--publish-token", default="", help="Scoped WAI School child publish token")
    parser.add_argument("--project", default="", help="Existing wai.school/project/... URL or slug to update with a publish token")
    parser.add_argument("--expect-url", default="", help="Fail unless the server returns this exact public project URL")
    parser.add_argument("--require-updated", action="store_true", help="Fail unless the server confirms updated: true")
    parser.add_argument("--restore", action="store_true", help="Restore an existing owned project into --dir before editing")
    parser.add_argument("--force", action="store_true", help="Allow --restore to replace files in the target folder")
    parser.add_argument("--dry-run", action="store_true", help="Bundle and validate locally, but do not upload")
    args = parser.parse_args()

    target = Path(args.dir).expanduser().resolve()
    publish_token = (args.publish_token or os.environ.get("WAI_SCHOOL_PUBLISH_TOKEN", "")).strip()
    explicit_slug = project_arg_to_slug(args.project)
    expected_url = (args.expect_url or "").strip()
    if expected_url:
        project_arg_to_slug(expected_url)
    if args.restore:
        if not explicit_slug:
            fail("Restoring a project needs --project with a wai.school/project/... URL or slug.")
        result = restore_project_source(explicit_slug, args.project or explicit_slug, publish_token, target, args.force)
        print(json.dumps(result, ensure_ascii=False))
        return

    if not target.exists():
        fail(f"Path does not exist: {target}")

    html, title, warnings, files = bundle_project(target)
    state_file = state_path(target)
    state = load_state(state_file)
    if explicit_slug:
        if state.get("slug") != explicit_slug:
            state = {"slug": explicit_slug}
        else:
            state["slug"] = explicit_slug
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
                    "hasPublishToken": bool(publish_token),
                },
                ensure_ascii=False,
            )
        )
        return
    if state.get("slug") and not state.get("editToken") and not publish_token:
        fail("Updating an existing project by slug needs --publish-token or a saved edit token.")

    result = publish(html, title, state, files, publish_token)
    if result.get("ok"):
        if expected_url and result.get("url") != expected_url:
            fail(f"Publish returned a different URL; expected {expected_url}, got {result.get('url') or ''}")
        if args.require_updated and result.get("updated") is not True:
            fail("Publish did not update an existing project; expected updated: true")
        if save_state(state_file, result):
            result["projectStateSaved"] = True
        if result.get("editToken"):
            del result["editToken"]
    result["warnings"] = warnings
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
