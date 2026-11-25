"""Repository-reviewed policy; never consume model-produced acceptance claims."""

import re
from datetime import date, datetime, timezone

import yaml

POLICY_PATH = ".titvoignore.yml"


class PolicyLoader(yaml.SafeLoader):
    """Reject aliases and duplicate mapping keys rather than resolving ambiguity."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Duplicate or non-string mapping key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_policy(text: str | None, *, today: date | None = None) -> list[dict]:
    """Validate the entire policy before filtering expired entries (UTC inclusive)."""
    if text is None:
        return []
    try:
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Policy exceeds 1 MiB")
        data = yaml.load(text, Loader=PolicyLoader)
        if not isinstance(data, dict) or set(data) != {"version", "exceptions"}:
            raise ValueError("Expected version and exceptions")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("Expected version 1")
        if not isinstance(data["exceptions"], list):
            raise ValueError("Expected exceptions list")
        fields = {"path", "line", "category", "reason", "accepted_by", "expires_on"}
        seen = set()
        active = []
        for entry in data["exceptions"]:
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ValueError("Invalid exception fields")
            if any(
                not isinstance(entry[k], str) or not entry[k].strip()
                for k in fields - {"line"}
            ):
                raise ValueError("Expected nonempty strings; quote expires_on")
            path = entry["path"]
            if (
                path.startswith("/")
                or "\\" in path
                or any(p in {"", ".", ".."} for p in path.split("/"))
                or any(c in path for c in "*?[]")
            ):
                raise ValueError("Expected exact repository-relative POSIX path")
            if type(entry["line"]) is not int or entry["line"] <= 0:
                raise ValueError("Expected positive integer line")
            expiry = entry["expires_on"]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
                raise ValueError("Expected quoted YYYY-MM-DD")
            expires = date.fromisoformat(expiry)
            selector = (path, entry["line"], entry["category"])
            if selector in seen:
                raise ValueError("Duplicate exception selector")
            seen.add(selector)
            if expires >= (today or datetime.now(timezone.utc).date()):
                active.append(entry)
        return active
    except (ValueError, TypeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"Invalid .titvoignore.yml: {exc}") from exc


def apply_policy(issues, text, commit_hash=""):
    """Run only after consolidation; preserve evidence and replace untrusted claims."""
    entries = parse_policy(text)
    selectors = {(e["path"], e["line"], e["category"]): e for e in entries}
    for issue in issues:
        issue.metadata.pop("acceptance", None)
        path = issue.path
        if commit_hash and path.startswith(f"{commit_hash}/"):
            path = path[len(commit_hash) + 1 :]
        entry = selectors.get((path, issue.line, issue.category))
        if entry:
            issue.metadata["acceptance"] = {
                k: entry[k] for k in ("reason", "accepted_by", "expires_on")
            }
    return [i for i in issues if "acceptance" not in i.metadata]


def separate_policy(files):
    """Separate already integrity-validated transport files from model inputs."""
    policies = [f for f in files if f["path"] == POLICY_PATH]
    if len(policies) > 1:
        raise ValueError("Invalid .titvoignore.yml: duplicate policy")
    text = policies[0]["content"] if policies else None
    parse_policy(text)
    return [f for f in files if f["path"] != POLICY_PATH], text
