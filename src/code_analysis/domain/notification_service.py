from typing import Any, Dict

from typing_extensions import List

from code_analysis.domain.dto.bitbucket_dto import BitbucketCodeInsightsInputDto
from code_analysis.domain.dto.result_dto import AnalysisStatus, IssueDto, ResultDto
from code_analysis.domain.entities.task_entity import TaskSource
from code_analysis.domain.ports.bitbucket_repository import IBitbucketRepository
from code_analysis.domain.ports.github_repository import IGitHubRepository
from code_analysis.domain.ports.report_repository import IReportRepository


class NotificationService:
    def __init__(
        self,
        bitbucket_repository: IBitbucketRepository,
        github_repository: IGitHubRepository,
        report_repository: IReportRepository,
    ):
        self.bitbucket_repository = bitbucket_repository
        self.github_repository = github_repository
        self.report_repository = report_repository

    def __normalize_issues(
        self, commit_hash: str, issues: List[IssueDto]
    ) -> List[IssueDto]:
        normalized_issues = []
        for issue in issues:
            normalized_issues.append(
                IssueDto(
                    path=issue.get("path").replace(f"{commit_hash}/", ""),
                    line=issue.get("line"),
                    title=issue.get("title"),
                    description=issue.get("description"),
                    severity=issue.get("severity"),
                    type=issue.get("type"),
                    code=issue.get("code"),
                    summary=issue.get("summary"),
                    recommendation=issue.get("recommendation"),
                )
            )
        return normalized_issues

    def send_notifications(self, result_dto: ResultDto) -> Dict[str, Any]:
        if result_dto.status == AnalysisStatus.COMPLETED.value:
            return None
        notifications_results = {}
        issues = self.__normalize_issues(result_dto.commit_hash, result_dto.issues)
        report_result = self.report_repository.create_report(
            ResultDto(
                status=result_dto.status,
                issues=issues,
                source=result_dto.source,
                args=result_dto.args,
                commit_hash=result_dto.commit_hash,
                scaned_files=result_dto.scaned_files,
            )
        )
        notifications_results["report_url"] = report_result["reportURL"]
        if result_dto.source == TaskSource.BITBUCKET.value:
            bitbucket_code_insights_input_dto = BitbucketCodeInsightsInputDto(
                reportURL=report_result["reportURL"],
                workspaceId=result_dto.args.get("bitbucket_workspace"),
                commitHash=result_dto.args.get("bitbucket_commit"),
                repoSlug=result_dto.args.get("bitbucket_repo_slug"),
                status=result_dto.status,
                annotations=issues,
            )
            bitbucket_result = self.bitbucket_repository.create_code_insights_report(
                bitbucket_code_insights_input_dto
            )
            notifications_results["code_insights_url"] = bitbucket_result[
                "codeInsightsURL"
            ]
        elif result_dto.source == TaskSource.GITHUB.value:
            github_result = self.github_repository.create_github_issue(
                ResultDto(
                    status=result_dto.status,
                    issues=issues,
                    source=result_dto.source,
                    args=result_dto.args,
                    commit_hash=result_dto.commit_hash,
                    scaned_files=result_dto.scaned_files,
                )
            )
            notifications_results["html_url"] = github_result["htmlURL"]
        return notifications_results
