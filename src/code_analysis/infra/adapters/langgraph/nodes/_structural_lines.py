"""Utilities for detecting structural/definition lines in source code.

Used by RagRetrievalNode (embedding query construction) and BaseExpertNode
(structure-aware file truncation for the LLM prompt).

Design notes
------------
- Detection is intentionally **permissive**: a false positive (including a
  non-structural line) costs a few extra tokens; a false negative (missing a
  function signature) can hide a vulnerability from the expert.
- Language-agnostic: a single regex covers Python, JS/TS, Java, Kotlin, Scala,
  Go, Rust, C#, Ruby, PHP, Swift, C/C++, SQL, Dockerfile, Terraform/HCL, Shell.
- Line order is always preserved; we never reorder or deduplicate.
"""
import re

# ---------------------------------------------------------------------------
# Core regex
# ---------------------------------------------------------------------------
# Each alternative is anchored to the start of the (stripped) line.
# Optional leading whitespace is consumed by `^\s*`.
_STRUCTURAL_RE = re.compile(
    r"^\s*(?:"

    # ── 1. Access/visibility modifiers ──────────────────────────────────────
    # Java, C#, Kotlin, PHP, Scala, Swift, Dart.
    # One or more modifiers can precede a definition keyword.
    r"(?:(?:public|private|protected|internal|static|abstract|sealed|final|"
    r"override|open|companion|inline|external|suspend|operator|infix|"
    r"readonly|virtual|native|synchronized|volatile|transient)\s+)+"
    r"|"

    # ── 2. Function / method definitions ────────────────────────────────────
    r"\basync\s+def\s+"                          # Python async def
    r"|\bdef\s+"                                 # Python, Ruby
    r"|\bfn\s+"                                  # Rust (pub fn handled by rule 1)
    r"|\bfunc\s+"                                # Go, Swift
    r"|\bfun\s+"                                 # Kotlin
    r"|\basync\s+function\s+"                    # JS async function
    r"|\bfunction\s+"                            # JS, PHP
    r"|\bsub\s+"                                 # Perl / VB subroutine
    r"|"

    # ── 3. Type / class / interface definitions ──────────────────────────────
    r"\bclass\s+"                                # Python, Java, JS, PHP, Ruby, C#…
    r"|\binterface\s+"                           # Java, TS, C#, PHP, Go
    r"|\btrait\s+"                               # Rust, PHP, Scala
    r"|\bstruct\s+"                              # Go, Rust, C, Swift
    r"|\benum\s+"                                # Rust, Java, TS, Kotlin, C++
    r"|\bprotocol\s+"                            # Swift
    r"|\bextension\s+"                           # Swift, Kotlin
    r"|\brecord\s+"                              # Java 16+, Kotlin
    r"|\bobject\s+"                              # Kotlin object, Scala object
    r"|\bimpl\s+"                                # Rust (impl Trait for Type)
    r"|\btype\s+\w+\s*[={<]"                    # Go `type X struct{…}`, TS `type X =`
    r"|\btypedef\s+"                             # C/C++
    r"|\balias\s+"                               # Ruby alias, Swift typealias
    r"|"

    # ── 4. Module / namespace / package declarations ─────────────────────────
    r"\bnamespace\s+"                            # C#, PHP
    r"|\bpackage\s+"                             # Go, Java, Kotlin, Scala
    r"|\bmodule\s+"                              # Ruby, Python module-level, Node.js
    r"|\bmod\s+"                                 # Rust mod
    r"|\bpragma\s+"                              # Ada, Rust crate-level
    r"|"

    # ── 5. Import / include / require ────────────────────────────────────────
    r"\bimport\s+"                               # Python, Java, Go, JS/TS, Dart, Swift
    r"|\bfrom\s+\S+\s+import\s+"                # Python `from x import y`
    r"|\buse\s+"                                 # Rust `use`, PHP `use`
    r"|\busing\s+"                               # C#, C++ `using`
    r"|\brequire\s+"                             # Ruby, Node.js
    r"|\brequire_relative\s+"                    # Ruby
    r"|\brequire_once\s+"                        # PHP
    r"|\binclude\s+"                             # Ruby, PHP, C (via preprocessor rule)
    r"|\binclude_once\s+"                        # PHP
    r"|\b#\s*include\b"                          # C/C++ #include
    r"|\b#\s*define\b"                           # C/C++ #define
    r"|\b#\s*pragma\b"                           # C/C++ #pragma
    r"|\b#\s*if(?:n?def)?\b"                    # C/C++ conditional compilation
    r"|"

    # ── 6. JS/TS-specific ────────────────────────────────────────────────────
    r"\bexport\s+(?:default\s+|async\s+)?"
    r"(?:class|function|interface|type|enum|const|let|var|abstract)?\s*\w"
    r"|\bdeclare\s+"                             # TS `declare module`, `declare const`
    r"|\bconst\s+\w+\s*[=:]"                    # const with assignment/type
    r"|\blet\s+\w+\s*[=:]"                      # let with assignment
    r"|\bvar\s+\w+\s*[=:]"                      # var (JS, PHP, Go var block)
    r"|"

    # ── 7. Decorators and annotations ────────────────────────────────────────
    r"@\w+"                                      # Python, Java, TS, Kotlin, Dart
    r"|"

    # ── 8. Infrastructure-as-Code (Terraform, Pulumi, CDK, Ansible) ──────────
    r"\b(?:resource|provider|data|output|variable|locals|terraform|backend|"
    r"required_providers|moved|check)\s+"
    r"|"

    # ── 9. Dockerfile instructions ────────────────────────────────────────────
    r"^(?:FROM|RUN|CMD|ENTRYPOINT|EXPOSE|ENV|ARG|WORKDIR|COPY|ADD|"
    r"LABEL|HEALTHCHECK|VOLUME|USER|STOPSIGNAL|ONBUILD|SHELL)\s"
    r"|"

    # ── 10. SQL DDL ───────────────────────────────────────────────────────────
    r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:TABLE|VIEW|INDEX|FUNCTION|"
    r"PROCEDURE|TRIGGER|DATABASE|SCHEMA|SEQUENCE)\b"
    r"|"

    # ── 11. Shell function definitions ────────────────────────────────────────
    r"\w+\s*\(\s*\)\s*\{"                        # name() {
    r"|\bfunction\s+\w+\s*(?:\(\s*\))?\s*\{"    # function name() {

    r")",
    re.VERBOSE | re.IGNORECASE,
)


def is_structural(line: str) -> bool:
    """Return True if *line* is a definition, declaration, or import statement."""
    return bool(_STRUCTURAL_RE.match(line))


def extract_structural_lines(content: str, max_lines: int = 40) -> list[str]:
    """Return up to *max_lines* structural lines from *content*, in original order."""
    result: list[str] = []
    for line in content.splitlines():
        if line.strip() and is_structural(line):
            result.append(line.rstrip())
            if len(result) >= max_lines:
                break
    return result
