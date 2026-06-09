"""Tests for FindingsMerger service."""

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult
from code_analysis.domain.services.findings_merger import FindingsMerger


class TestFindingsMerger:
    """Tests for FindingsMerger deduplication logic."""

    def test_empty_merge(self):
        """Empty merge should return COMPLETED."""
        merger = FindingsMerger()
        result = merger.to_dict(scaned_files=0)
        assert result["status"] == "COMPLETED"
        assert result["issues"] == []

    def test_single_expert_result(self):
        """Single expert result should be preserved."""
        merger = FindingsMerger()
        issue = ExpertIssue(
            title="SQL Injection",
            description="SQLi found",
            severity="CRITICAL",
            category="Injection",
            path="src/db.py",
            line=10,
            summary="SQLi",
            code="query = 'SELECT *'",
            recommendation="Parametrize",
        )
        result = ExpertResult(
            expert_name="code_vulnerabilities",
            issues=[issue],
        )
        merger.add_expert_result(result)

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].title == "SQL Injection"

    def test_deduplication_same_issue(self):
        """Same issue from multiple experts should be deduplicated."""
        merger = FindingsMerger()

        # Same issue from two experts
        issue1 = ExpertIssue(
            title="XSS",
            description="XSS in template",
            severity="HIGH",
            category="OWASP Web",
            path="src/template.html",
            line=20,
            summary="XSS",
            code="{{ user_input }}",
            recommendation="Escape output",
        )
        issue2 = ExpertIssue(
            title="XSS Found",  # Different title, same location
            description="Cross-site scripting",
            severity="HIGH",
            category="OWASP Web",  # Same category
            path="src/template.html",  # Same path
            line=20,  # Same line
            summary="XSS issue",
            code="{{ user_input }}",
            recommendation="Use auto-escaping",
        )

        merger.add_expert_result(ExpertResult("owasp_web", [issue1]))
        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue2]))

        merged = merger.get_merged_issues()
        assert len(merged) == 1  # Deduplicated

    def test_conservative_severity_merge(self):
        """Conflict should keep lower severity."""
        merger = FindingsMerger()

        issue1 = ExpertIssue(
            title="Weak Hash",
            description="MD5 used",
            severity="HIGH",
            category="Crypto",
            path="src/auth.py",
            line=15,
            summary="MD5 hash",
            code="hashlib.md5(password)",
            recommendation="Use bcrypt",
        )
        issue2 = ExpertIssue(
            title="Weak Hash",
            description="MD5 used",
            severity="MEDIUM",  # Lower severity
            category="Crypto",
            path="src/auth.py",
            line=15,
            summary="MD5 hash",
            code="hashlib.md5(password)",
            recommendation="Use bcrypt",
        )

        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue1]))
        merger.add_expert_result(ExpertResult("devsecops", [issue2]))

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].severity == "MEDIUM"  # Conservative: lower severity kept

    def test_different_locations_not_deduplicated(self):
        """Issues at different locations should not be deduplicated."""
        merger = FindingsMerger()

        issue1 = ExpertIssue(
            title="SQL Injection",
            description="SQLi",
            severity="CRITICAL",
            category="Injection",
            path="src/users.py",
            line=10,
            summary="SQLi",
            code="query = f'SELECT'",
            recommendation="Fix",
        )
        issue2 = ExpertIssue(
            title="SQL Injection",
            description="SQLi",
            severity="CRITICAL",
            category="Injection",
            path="src/orders.py",  # Different path
            line=10,
            summary="SQLi",
            code="query = f'SELECT'",
            recommendation="Fix",
        )

        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue1, issue2]))

        merged = merger.get_merged_issues()
        assert len(merged) == 2  # Not deduplicated

    def test_same_code_evidence_deduplicated_across_categories(self):
        """Same code evidence should deduplicate across categories."""
        merger = FindingsMerger()

        issue1 = ExpertIssue(
            title="Path Traversal",
            description="Path traversal",
            severity="HIGH",
            category="Path Traversal",
            path="src/files.py",
            line=25,
            summary="Path traversal",
            code="open(path)",
            recommendation="Validate path",
        )
        issue2 = ExpertIssue(
            title="Arbitrary File Read",  # Same issue, different categorization
            description="Arbitrary file read",
            severity="HIGH",
            category="File Operations",  # Different category
            path="src/files.py",
            line=25,
            summary="File read",
            code="open(path)",
            recommendation="Validate path",
        )

        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue1]))
        merger.add_expert_result(ExpertResult("owasp_web", [issue2]))

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].title == "Path Traversal"

    def test_duplicate_prefers_issue_with_code(self):
        """When only one duplicate has code, the code-bearing issue wins."""
        merger = FindingsMerger()

        issue_without_code = ExpertIssue(
            title="Token storage risk",
            description="Tokens are persisted in browser storage",
            severity="HIGH",
            category="Auth Storage",
            path="services/auth/tokenStorage.ts",
            line=16,
            summary="Tokens in localStorage",
            code="",
            recommendation="Avoid browser storage for tokens",
        )
        issue_with_code = ExpertIssue(
            title="Almacenamiento inseguro de tokens en localStorage",
            description="Tokens are stored in localStorage",
            severity="HIGH",
            category="Insecure Storage",
            path="services/auth/tokenStorage.ts",
            line=16,
            summary="Tokens persisted in localStorage",
            code="window.localStorage.setItem(KEYS.ACCESS, tokens.accessToken);",
            recommendation="Use HttpOnly Secure SameSite cookies",
        )

        merger.add_expert_result(ExpertResult("auth_expert", [issue_without_code]))
        merger.add_expert_result(ExpertResult("web_expert", [issue_with_code]))

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].title == issue_with_code.title
        assert merged[0].code == issue_with_code.code
        assert merged[0].category == issue_with_code.category

    def test_duplicate_prefers_longer_containing_code_snippet(self):
        """Subset code snippets should deduplicate and keep more evidence."""
        merger = FindingsMerger()

        short_issue = ExpertIssue(
            title="Almacenamiento inseguro de tokens en localStorage",
            description="Access token is stored in localStorage",
            severity="HIGH",
            category="Insecure Storage",
            path="services/auth/tokenStorage.ts",
            line=16,
            summary="Access token persisted in localStorage",
            code="window.localStorage.setItem(KEYS.ACCESS, tokens.accessToken);",
            recommendation="Use HttpOnly Secure SameSite cookies",
        )
        long_issue = ExpertIssue(
            title="Almacenamiento inseguro de tokens en localStorage (web)",
            description="Access, refresh and ID tokens are stored in localStorage",
            severity="HIGH",
            category="Auth Token Storage",
            path="services/auth/tokenStorage.ts",
            line=16,
            summary="Sensitive tokens persisted in localStorage",
            code=(
                "window.localStorage.setItem(KEYS.ACCESS, tokens.accessToken); "
                "window.localStorage.setItem(KEYS.REFRESH, tokens.refreshToken); "
                "window.localStorage.setItem(KEYS.ID, tokens.idToken);"
            ),
            recommendation="Use backend-managed secure cookies",
        )

        merger.add_expert_result(ExpertResult("web_expert", [short_issue]))
        merger.add_expert_result(ExpertResult("auth_expert", [long_issue]))

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].title == long_issue.title
        assert merged[0].code == long_issue.code
        assert merged[0].category == long_issue.category

    def test_status_failed_with_critical(self):
        """Status should be FAILED with CRITICAL issues."""
        merger = FindingsMerger()
        issue = ExpertIssue(
            title="RCE",
            description="Remote code execution",
            severity="CRITICAL",
            category="RCE",
            path="src/exec.py",
            line=1,
            summary="RCE",
            code="eval(user_input)",
            recommendation="Remove eval",
        )
        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue]))

        assert merger.get_final_status() == "FAILED"

    def test_status_warning_with_medium(self):
        """Status should be WARNING with only MEDIUM issues."""
        merger = FindingsMerger()
        issue = ExpertIssue(
            title="Verbose Error",
            description="Information disclosure",
            severity="MEDIUM",
            category="Info Disclosure",
            path="src/errors.py",
            line=10,
            summary="Verbose error",
            code="return str(e)",
            recommendation="Use generic messages",
        )
        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue]))

        assert merger.get_final_status() == "WARNING"

    def test_static_merge_results(self):
        """Static merge_results should work correctly."""
        issues = [
            ExpertIssue(
                title="Test",
                description="Test",
                severity="LOW",
                category="Test",
                path="test.py",
                line=1,
                summary="Test",
                code="test",
                recommendation="Fix",
            )
        ]
        results = [
            ExpertResult("expert1", issues),
        ]

        result = FindingsMerger.merge_results(results, scaned_files=5)
        assert result["status"] == "WARNING"
        assert result["scaned_files"] == 5
        assert len(result["issues"]) == 1

    def test_error_result_skipped(self):
        """Expert results with errors should be skipped."""
        merger = FindingsMerger()

        good_issue = ExpertIssue(
            title="Good Finding",
            description="Valid issue",
            severity="HIGH",
            category="Test",
            path="good.py",
            line=1,
            summary="Good",
            code="code",
            recommendation="Fix",
        )

        merger.add_expert_result(ExpertResult("good_expert", [good_issue]))
        merger.add_expert_result(
            ExpertResult("bad_expert", [], error="Connection failed")
        )

        merged = merger.get_merged_issues()
        assert len(merged) == 1
        assert merged[0].title == "Good Finding"
