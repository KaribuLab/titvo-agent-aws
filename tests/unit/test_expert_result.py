"""Tests for ExpertResult domain entities."""

from code_analysis.domain.entities.expert_result import (
    ExpertIssue,
    ExpertResult,
    FileContent,
)


class TestFileContent:
    """Tests for FileContent entity."""

    def test_creation(self):
        """FileContent should store path and content."""
        fc = FileContent(path="src/main.py", content="print('hello')")
        assert fc.path == "src/main.py"
        assert fc.content == "print('hello')"

    def test_to_dict(self):
        """to_dict should return correct structure."""
        fc = FileContent(path="test.py", content="x = 1")
        d = fc.to_dict()
        assert d == {"path": "test.py", "content": "x = 1"}


class TestExpertIssue:
    """Tests for ExpertIssue entity."""

    def test_creation_with_all_fields(self):
        """ExpertIssue should accept all required fields."""
        issue = ExpertIssue(
            title="SQL Injection",
            description="Vulnerable to SQL injection",
            severity="CRITICAL",
            category="Injection",
            path="src/db.py",
            line=42,
            summary="SQLi en consulta",
            code="query = f'SELECT * FROM users WHERE id = {user_id}'",
            recommendation="Usar consultas parametrizadas",
        )
        assert issue.title == "SQL Injection"
        assert issue.severity == "CRITICAL"
        assert issue.line == 42

    def test_severity_normalization(self):
        """Severity should be normalized to uppercase."""
        issue = ExpertIssue(
            title="Test",
            description="Test",
            severity="critical",  # lowercase
            category="Test",
            path="test.py",
            line=1,
            summary="Test",
            code="test",
            recommendation="Fix it",
        )
        assert issue.severity == "CRITICAL"

    def test_invalid_severity_defaults_to_medium(self):
        """Invalid severity should default to MEDIUM."""
        issue = ExpertIssue(
            title="Test",
            description="Test",
            severity="INVALID",
            category="Test",
            path="test.py",
            line=1,
            summary="Test",
            code="test",
            recommendation="Fix it",
        )
        assert issue.severity == "MEDIUM"

    def test_to_dict(self):
        """to_dict should include all fields."""
        issue = ExpertIssue(
            title="XSS",
            description="Cross-site scripting",
            severity="HIGH",
            category="OWASP Web",
            path="src/app.js",
            line=10,
            summary="XSS reflejado",
            code="element.innerHTML = userInput",
            recommendation="Usar textContent en lugar de innerHTML",
            metadata={"confidence": 0.9},
        )
        d = issue.to_dict()
        assert d["title"] == "XSS"
        assert d["severity"] == "HIGH"
        assert d["confidence"] == 0.9  # From metadata

    def test_from_dict(self):
        """from_dict should parse dictionary correctly."""
        data = {
            "title": "Hardcoded Secret",
            "description": "API key exposed",
            "severity": "CRITICAL",
            "category": "Secrets",
            "path": "config.py",
            "line": 5,
            "summary": "Secreto hardcodeado",
            "code": "API_KEY = 'sk-12345'",
            "recommendation": "Usar variables de entorno",
            "extra_field": "ignored",  # Should go to metadata
        }
        issue = ExpertIssue.from_dict(data)
        assert issue.title == "Hardcoded Secret"
        assert issue.metadata.get("extra_field") == "ignored"

    def test_get_dedup_key(self):
        """Deduplication key should be (path, line, category)."""
        issue = ExpertIssue(
            title="Test",
            description="Test",
            severity="HIGH",
            category="Injection",
            path="src/app.py",
            line=42,
            summary="Test",
            code="test",
            recommendation="Fix",
        )
        assert issue.get_dedup_key() == ("src/app.py", 42, "Injection")


class TestExpertResult:
    """Tests for ExpertResult entity."""

    def test_creation(self):
        """ExpertResult should store expert results."""
        result = ExpertResult(
            expert_name="owasp_api",
            issues=[],
            files_analyzed=10,
        )
        assert result.expert_name == "owasp_api"
        assert result.files_analyzed == 10
        assert result.is_success()
        assert not result.has_findings()

    def test_with_issues(self):
        """Result with issues should report findings."""
        issue = ExpertIssue(
            title="Test",
            description="Test",
            severity="MEDIUM",
            category="Test",
            path="test.py",
            line=1,
            summary="Test",
            code="test",
            recommendation="Fix",
        )
        result = ExpertResult(
            expert_name="test_expert",
            issues=[issue],
        )
        assert result.has_findings()
        assert result.get_max_severity() == "MEDIUM"

    def test_with_error(self):
        """Result with error should not be success."""
        result = ExpertResult(
            expert_name="failed_expert",
            issues=[],
            error="Connection timeout",
        )
        assert not result.is_success()
        assert result.error == "Connection timeout"

    def test_max_severity_critical(self):
        """Max severity should return highest."""
        issues = [
            ExpertIssue(
                title="Low",
                description="Low",
                severity="LOW",
                category="X",
                path="a.py",
                line=1,
                summary="X",
                code="x",
                recommendation="Y",
            ),
            ExpertIssue(
                title="Critical",
                description="Critical",
                severity="CRITICAL",
                category="Y",
                path="b.py",
                line=2,
                summary="Y",
                code="y",
                recommendation="Z",
            ),
        ]
        result = ExpertResult(
            expert_name="test",
            issues=issues,
        )
        assert result.get_max_severity() == "CRITICAL"

    def test_to_dict(self):
        """to_dict should serialize all fields."""
        result = ExpertResult(
            expert_name="owasp_web",
            issues=[],
            files_analyzed=5,
        )
        d = result.to_dict()
        assert d["expert_name"] == "owasp_web"
        assert d["files_analyzed"] == 5
        assert d["issues"] == []
