"""Deterministic runtime classification for analysed files.

Every file retrieved by MCP gets an ordered, non-empty list of runtimes
(``browser``, ``server``, ``mobile``, ``infra``, ``test``, ``config``,
``unknown``). Experts select files by intersecting that list with their own
runtime set, so a classification mistake can only *add* experts, never remove
them: ambiguous files receive every runtime they show signals for, and files
without signals fall back to ``unknown``, which every code expert analyses.

Classification is a pure function of ``(path, content, project_profile)``.
No LLM is involved so the result is reproducible across scans.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from dataclasses import dataclass
from enum import Enum

LOGGER = logging.getLogger(__name__)


class Runtime(str, Enum):
    BROWSER = "browser"
    SERVER = "server"
    MOBILE = "mobile"
    INFRA = "infra"
    TEST = "test"
    CONFIG = "config"
    UNKNOWN = "unknown"


ALL_RUNTIMES: frozenset[Runtime] = frozenset(Runtime)

# Canonical ordering when a file carries several runtimes.
_RUNTIME_ORDER = {
    Runtime.MOBILE: 0,
    Runtime.BROWSER: 1,
    Runtime.SERVER: 2,
    Runtime.INFRA: 3,
    Runtime.TEST: 4,
    Runtime.CONFIG: 5,
    Runtime.UNKNOWN: 6,
}


@dataclass(frozen=True)
class ProjectProfile:
    """Repository-level signals that widen per-file classification."""

    mobile_js: bool = False
    frontend_js: bool = False
    backend_js: bool = False
    flutter: bool = False
    ios: bool = False
    android: bool = False

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.mobile_js,
                self.frontend_js,
                self.backend_js,
                self.flutter,
                self.ios,
                self.android,
            )
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "mobile_js": self.mobile_js,
            "frontend_js": self.frontend_js,
            "backend_js": self.backend_js,
            "flutter": self.flutter,
            "ios": self.ios,
            "android": self.android,
        }


EMPTY_PROFILE = ProjectProfile()

# ---------------------------------------------------------------------------
# Path / name based signals
# ---------------------------------------------------------------------------

_TEST_DIRS = {
    "test",
    "tests",
    "__tests__",
    "spec",
    "specs",
    "e2e",
    "cypress",
    "__mocks__",
    "fixtures",
    "testing",
}
_TEST_FILE_RE = re.compile(
    r"(?:^|[./_-])(?:test|tests|spec|specs)(?:[._-]|$)|^conftest\.py$|_test\.go$",
    re.IGNORECASE,
)

_INFRA_DIRS = {
    ".github",
    ".gitlab",
    ".circleci",
    "k8s",
    "kubernetes",
    "helm",
    "terraform",
    "terragrunt",
    "cloudformation",
    "infra",
    "infrastructure",
    "deploy",
    "deployment",
    "ansible",
    "docker",
    ".devcontainer",
}
_INFRA_EXTENSIONS = {
    ".tf",
    ".tfvars",
    ".hcl",
    ".yml",
    ".yaml",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
}
_INFRA_NAME_RE = re.compile(
    r"^(?:Dockerfile.*|docker-compose.*|Jenkinsfile.*|Makefile|Vagrantfile|Procfile"
    r"|\.gitlab-ci\.yml|\.dockerignore)$"
)

_CONFIG_EXTENSIONS = {
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".xml",
    ".lock",
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
    ".csv",
    ".env",
}
_ENV_FILE_RE = re.compile(r"^\.env(?:\..+)?$")

_MOBILE_NAME_RE = re.compile(
    r"^(?:AndroidManifest\.xml|network_security_config\.xml|Info\.plist|Podfile"
    r"|Podfile\.lock|Package\.swift|proguard-rules\.pro|pubspec\.yaml|pubspec\.lock)$"
)
_MOBILE_CONFIG_NAME_RE = re.compile(
    r"^(?:app\.json|eas\.json|app\.config\..+|metro\.config\..+|react-native\.config\..+"
    r"|build\.gradle(?:\.kts)?|settings\.gradle(?:\.kts)?|gradle\.properties|.+\.xcconfig)$"
)
_MOBILE_EXTENSIONS = {".swift", ".m", ".mm", ".dart", ".entitlements"}

_BROWSER_EXTENSIONS = {
    ".html",
    ".htm",
    ".vue",
    ".svelte",
    ".astro",
    ".css",
    ".scss",
    ".sass",
    ".less",
}
_TEMPLATE_EXTENSIONS = {
    ".ejs",
    ".hbs",
    ".handlebars",
    ".pug",
    ".jade",
    ".jinja",
    ".jinja2",
    ".j2",
    ".twig",
    ".erb",
    ".mustache",
}

_SERVER_ONLY_EXTENSIONS = {".sql", ".graphql", ".gql", ".proto"}

_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
_SERVER_LANG_EXTENSIONS = {".py", ".rb", ".php", ".go", ".cs", ".java", ".kt", ".kts"}
_CODE_EXTENSIONS = _JS_EXTENSIONS | _SERVER_LANG_EXTENSIONS

_BROWSER_DIRS = {
    "frontend",
    "front",
    "web",
    "client",
    "ui",
    "components",
    "pages",
    "views",
    "public",
    "static",
    "assets",
    "layouts",
    "hooks",
    "stores",
}
_SERVER_DIRS = {
    "backend",
    "back",
    "server",
    "api",
    "services",
    "lambda",
    "lambdas",
    "handlers",
    "functions",
    "controllers",
    "routes",
    "middleware",
    "middlewares",
    "repositories",
    "workers",
}

# ---------------------------------------------------------------------------
# Content based signals
# ---------------------------------------------------------------------------

_MOBILE_CONTENT_RE = re.compile(
    r"""['"](?:react-native|@react-native[\w/-]*|expo|expo-[\w-]+|@expo/[\w-]+"""
    r"""|@react-navigation/[\w-]+)['"]|\bNativeModules\b""",
)
_BROWSER_CONTENT_RE = re.compile(
    r"""['"](?:react|react-dom|react-dom/[\w-]+|vue|svelte|preact|solid-js|lit"""
    r"""|jquery|@angular/[\w-]+|next/[\w-]+|nuxt|#app)['"]"""
    r"""|\bwindow\.|\bdocument\.|\blocalStorage\b|\bsessionStorage\b|\bnavigator\."""
    r"""|\bimport\.meta\.env\b|process\.env\.(?:REACT_APP_|NEXT_PUBLIC_|VUE_APP_|VITE_)"""
    r"""|['"]use client['"]|\bXMLHttpRequest\b""",
)
_SERVER_CONTENT_RE = re.compile(
    r"""['"](?:express|fastify|koa|hono|@hapi/hapi|@nestjs/[\w-]+|serverless-http"""
    r"""|aws-lambda|@aws-sdk/[\w-]+|aws-sdk|mongoose|@prisma/client|typeorm|sequelize"""
    r"""|knex|pg|mysql2?|ioredis|redis)['"]"""
    r"""|\bhttps?\.createServer\b|\bexports\.handler\b|\bAPIGatewayProxy\w*"""
    # Python
    r"""|^\s*(?:from|import)\s+(?:flask|fastapi|django|starlette|sanic|tornado"""
    r"""|aiohttp|boto3|botocore|sqlalchemy|psycopg2?|pymongo|celery)\b"""
    # Ruby / PHP
    r"""|\bActionController\b|\bActiveRecord\b|\bSinatra\b|\bRails\b"""
    r"""|\bIlluminate\\|\bSymfony\\|\$_(?:GET|POST|SERVER|REQUEST|COOKIE)\b"""
    # Go
    r"""|"net/http"|"github\.com/gin-gonic/gin"|"github\.com/labstack/echo"""
    r"""|"github\.com/gofiber/fiber|"github\.com/go-chi/chi|"github\.com/gorilla/mux"""
    r"""|"database/sql\""""
    # C# / Java / Kotlin
    r"""|\bMicrosoft\.AspNetCore\b|\bSystem\.Web\b|\[ApiController\]"""
    r"""|\borg\.springframework\b|\bjakarta\.\w+|\bjavax\.servlet\b|\bjavax\.ws\.rs\b"""
    r"""|\bio\.ktor\b|\bio\.micronaut\b|\bio\.quarkus\b""",
    re.MULTILINE,
)
_ANDROID_CONTENT_RE = re.compile(r"\bandroidx?\.\w+|\bcom\.android\b")

_PACKAGE_JSON_MOBILE = {"react-native", "expo", "@react-native-community/cli"}
_PACKAGE_JSON_FRONTEND = {
    "react",
    "react-dom",
    "vue",
    "svelte",
    "@angular/core",
    "next",
    "nuxt",
    "vite",
    "preact",
    "solid-js",
    "@sveltejs/kit",
}
_PACKAGE_JSON_SERVER = {
    "express",
    "fastify",
    "@nestjs/core",
    "koa",
    "@hapi/hapi",
    "hono",
    "serverless-http",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_project_profile(files: list[dict]) -> ProjectProfile:
    """Derive repository-level signals from the retrieved files.

    Signals only ever *add* runtimes to files. When no manifest is present
    (typical for commit scans) the profile is empty and classification relies
    on per-file signals alone.
    """
    mobile_js = frontend = server = False
    flutter = ios = android = False

    for file in files:
        name = posixpath.basename(file.get("path", ""))
        content = file.get("content", "") or ""
        if name == "package.json":
            deps = _package_json_dependencies(content)
            if deps & _PACKAGE_JSON_MOBILE:
                mobile_js = True
            if deps & _PACKAGE_JSON_FRONTEND:
                frontend = True
            if deps & _PACKAGE_JSON_SERVER:
                server = True
        elif name == "pubspec.yaml":
            flutter = True
        elif name in ("Podfile", "Info.plist", "Package.swift"):
            ios = True
        elif name == "AndroidManifest.xml":
            android = True
        elif name.startswith("build.gradle") and _ANDROID_CONTENT_RE.search(content):
            android = True

    if mobile_js:
        # React Native always pulls `react`; it is not a web frontend.
        frontend = False

    return ProjectProfile(
        mobile_js=mobile_js,
        frontend_js=frontend and not server,
        backend_js=server and not frontend,
        flutter=flutter,
        ios=ios,
        android=android,
    )


def classify(
    path: str,
    content: str,
    profile: ProjectProfile = EMPTY_PROFILE,
) -> list[Runtime]:
    """Return the ordered, non-empty runtime list for a file."""
    norm_path = path.replace("\\", "/").strip("/")
    directories = [d.lower() for d in norm_path.split("/")[:-1]]
    name = posixpath.basename(norm_path)
    lower_name = name.lower()
    ext = _extension(lower_name)
    content = content or ""

    # 1. Path-level signals.
    if any(d in _TEST_DIRS for d in directories) or _TEST_FILE_RE.search(lower_name):
        return [Runtime.TEST]
    if any(d in _INFRA_DIRS for d in directories):
        return [Runtime.INFRA]

    # 2. Unequivocal names / extensions.
    if _MOBILE_NAME_RE.match(name):
        return [Runtime.MOBILE]
    if _MOBILE_CONFIG_NAME_RE.match(name):
        return _ordered({Runtime.MOBILE, Runtime.CONFIG})
    if _INFRA_NAME_RE.match(name) or ext in _INFRA_EXTENSIONS:
        return [Runtime.INFRA]
    if _ENV_FILE_RE.match(lower_name) or ext in _CONFIG_EXTENSIONS:
        return [Runtime.CONFIG]
    if ext in _MOBILE_EXTENSIONS:
        return [Runtime.MOBILE]
    if ext in _BROWSER_EXTENSIONS:
        return [Runtime.BROWSER]
    if ext in _TEMPLATE_EXTENSIONS:
        return _ordered({Runtime.BROWSER, Runtime.SERVER})
    if ext in _SERVER_ONLY_EXTENSIONS:
        return [Runtime.SERVER]

    if ext not in _CODE_EXTENSIONS:
        return [Runtime.UNKNOWN]

    # 3. Content signals (may yield several runtimes).
    found: set[Runtime] = set()
    if _MOBILE_CONTENT_RE.search(content):
        found.add(Runtime.MOBILE)
    if ext in (".kt", ".kts", ".java") and _ANDROID_CONTENT_RE.search(content):
        found.add(Runtime.MOBILE)
    if _BROWSER_CONTENT_RE.search(content):
        found.add(Runtime.BROWSER)
    if _SERVER_CONTENT_RE.search(content):
        found.add(Runtime.SERVER)

    # 4. Project profile widens, never narrows.
    if ext in _JS_EXTENSIONS:
        if profile.mobile_js:
            found.add(Runtime.MOBILE)
        if not found:
            if profile.frontend_js:
                found.update({Runtime.BROWSER, Runtime.UNKNOWN})
            elif profile.backend_js:
                found.update({Runtime.SERVER, Runtime.UNKNOWN})
    elif ext in (".kt", ".kts", ".java") and not found and profile.android:
        found.add(Runtime.MOBILE)

    if found:
        return _ordered(found)

    # Non-JS languages never run in a browser: server is the natural default.
    if ext in _SERVER_LANG_EXTENSIONS:
        return [Runtime.SERVER]

    # 5. Directory hints as a last tie-break for JS/TS.
    if any(d in _BROWSER_DIRS for d in directories):
        return [Runtime.BROWSER]
    if any(d in _SERVER_DIRS for d in directories):
        return [Runtime.SERVER]

    # 6. No signal at all.
    return [Runtime.UNKNOWN]


def classify_values(
    path: str,
    content: str,
    profile: ProjectProfile = EMPTY_PROFILE,
) -> list[str]:
    """Same as :func:`classify` but returns plain strings for state storage."""
    return [runtime.value for runtime in classify(path, content, profile)]


def primary_runtime(runtimes: list[str] | None) -> str:
    """Return the primary (first) runtime of a stored runtime list."""
    if runtimes:
        return str(runtimes[0])
    return Runtime.UNKNOWN.value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extension(lower_name: str) -> str:
    if lower_name.endswith(".gradle.kts"):
        return ".kts"
    _, ext = posixpath.splitext(lower_name)
    return ext


def _ordered(runtimes: set[Runtime]) -> list[Runtime]:
    return sorted(runtimes, key=lambda r: _RUNTIME_ORDER[r])


def _package_json_dependencies(content: str) -> set[str]:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(str(k) for k in section)
    return deps
