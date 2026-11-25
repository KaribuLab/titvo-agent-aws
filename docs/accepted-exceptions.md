# Accepted vulnerability exceptions

Repository review governs acceptance; `accepted_by` is a declared responsible party,
not an authenticated approval. No administration UI is provided.

The transport must include root `.titvoignore.yml` independently of selected source
files, from the scan revision (Git) or working tree (CLI). CLI archives still undergo
all existing path, manifest and hash validation before policy separation. The policy
is never sent to experts or consolidation prompts. CLI retrieval separates and
validates the exact root policy before applying `focus_files`; nested files named
`.titvoignore.yml` remain ordinary source and follow the existing focus rules.
This retrieval boundary does not establish exclusion from the external RAG index.

```yaml
version: 1
exceptions:
  - path: src/legacy.py
    line: 42
    category: Cryptography
    reason: Security accepts MD5 for an external legacy integration
    accepted_by: Customer security team
    expires_on: "2027-06-30"
```

All fields are mandatory. Selectors match exact repository-relative POSIX path,
positive integer line and case-sensitive category after final consolidation. No
wildcards, duplicate selectors/keys, aliases, unknown fields or unsafe YAML tags
are accepted. Dates must be quoted YYYY-MM-DD and remain valid through that UTC
date inclusively. Invalid present policies fail the scan explicitly; expired
entries accept nothing. Absence preserves existing finding behavior.

Accepted findings retain their evidence and severity and add optional `acceptance`
with `reason`, `accepted_by`, and `expires_on`. Only active HIGH/CRITICAL findings
block; accepted-only complete scans report COMPLETED and still generate reports.
Infrastructure errors and incomplete scans retain their existing outcomes.
Persisted `issues_count` includes every finding; `active_issues_count` and
`accepted_issues_count` distinguish blocking evidence from accepted risk.
