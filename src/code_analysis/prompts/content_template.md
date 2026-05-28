Analyze the following code files for security vulnerabilities.

<<<UNTRUSTED_INPUT>>>
Repository: {repository_url}
Branch: {branch}
Commit: {commit_hash}

{rag_context}

Additional parameters:
{args}

Files to analyze:
{files_content}
<<<END_UNTRUSTED_INPUT>>>

Execute the security analysis following your instructions.
Do NOT skip any verification steps. Do NOT fabricate findings.
Return ONLY the final JSON object.
