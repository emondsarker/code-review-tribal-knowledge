---
name: code-review
description: "Review code for convention violations and issues. Checks for P0 (critical), P1 (high), and P2 (medium) priority issues using parallel subagents. Triggers on: code review, review code, review my changes, check for issues, review commits."
---

# Code Review

Review code for convention violations and issues.

**IMPORTANT - DO NOT SKIP ANY STEPS:**
- You MUST follow every step in this skill sequentially, exactly as written. Under no circumstances can you deviate from, reorder, skip, or substitute any step. Do not improvise alternatives or combine steps.
- You MUST spawn subagents as described in Step 3. Do not attempt to analyze the code yourself without using subagents.

---

## Step 1: Find and read repo instructions file

Look for the following files in the project repository root (in order of precedence):
- AGENTS.md
- CLAUDE.md
- GEMINI.md

Read the first file found to understand the repo's rules, conventions, do-s, and don't-s.

## Step 2: Get information about changes

- Get the current branch name and compare with main/develop branch
- Get the list of unstaged files (`git diff --name-only`)
- Get the list of staged files (`git diff --cached --name-only`)
- If `$ARGUMENTS` contains a number N, review only the last N commits. Otherwise, review all commits in the current branch since it diverged from main/develop.
- Determine the appropriate diff command for subagents:
  - If reviewing commits: `git diff <base>..HEAD`
  - If reviewing staged changes: `git diff --cached`
  - If reviewing unstaged changes: `git diff`

Save the diff command and the list of changed files — subagents will use these.

## Step 2.5: Query CRTK for Team Tribal Knowledge

If the CRTK MCP server is available, query it for relevant past review feedback. If CRTK is not available, skip this step and continue without tribal knowledge context.

1. Call `list_tags` to get all available review categories.
2. Look at the changed files and diff content. Pick 3-7 tags that are most relevant to the nature of the changes (e.g. if touching a repository file, pick `database`, `mikroorm`; if adding an endpoint, pick `api-design`, `request-validation`).
3. Call `search_conventions` with:
   - `file_paths`: the list of changed files
   - `diff`: the first 4000 characters of the diff output
   - `pr_title`: the most recent commit message or branch name as a summary
   - `tags`: the tags you picked
   - `limit`: 15
4. Save the CRTK results as `{tribal_knowledge}`. This will be injected into every subagent prompt.

If CRTK returns no results or is unavailable, set `{tribal_knowledge}` to empty and proceed normally.

## Step 3: Analyze Code for Issues Using Subagents

Spawn subagents to analyze the code for different types of issues. Run all subagents in parallel to speed up the review.

### Shared Subagent Prompt Template

Launch 6 subagents in parallel, one per category below. Each subagent receives this prompt (fill in placeholders from the category table):

---
You are reviewing local code changes. Analyze the changed files for **{CATEGORY}**.

{If category is "Convention Violations", include: Conventions from AGENTS.md/CLAUDE.md/GEMINI.md: {conventions_content}}

{If tribal_knowledge is not empty, include the following block:}
### Team Tribal Knowledge (from past code reviews)
The following are real review comments left by team members on similar code in the past. Use these to inform your analysis — if the team has flagged a pattern before, flag it again if you see it in this diff.

{tribal_knowledge}

Changed files: {files_list}

Fetch the diff with `{diff_command}`, then read full file content for context.

For each issue found, return a JSON array:
```json
[{"file": "src/file.ts", "line": 42, "issue": "Description", "severity": "P0|P1|P2"}]
```
Return an empty array if no issues. {SEVERITY_RULE}

**Focus on:**
{FOCUS_ITEMS}

---

### Categories

1. **Convention Violations** — Severity: P1 or P2
   - Naming convention violations
   - Folder structure issues
   - File organization problems
   - Code style inconsistencies

2. **Missing Tests** — Severity: P1
   - Backend endpoints that need E2E tests
   - Backend helper functions that need unit tests
   - Frontend helper functions that need unit tests
   - Frontend components: NO test requirement (skip)

3. **Performance Issues** — Severity: P0 for critical issues, otherwise P2
   - N+1 queries (database calls in loops)
   - Nested loops O(n²) or worse on large datasets
   - Wrong data structures (array vs Set/Map)
   - Too many useEffect hooks in React
   - Race conditions (missing await, unsynchronized async)
   - Memory-intensive ops on large data

4. **Security Issues** — Severity: P0
   - Exposed secrets/credentials
   - Injection vulnerabilities (SQL, command, XSS)
   - Insecure deserialization
   - Missing auth/authz checks
   - Hardcoded test credentials
   - Debug artifacts (console.log, System.out.println, @ToString on sensitive entities)

5. **Logic and Flow** — Severity: P0 or P1
   - State machine gaps
   - Trust boundaries: unverified user input
   - Cross-file interactions: new enums/statuses not handled downstream
   - Edge cases: null values, unexpected states
   - Information disclosure: internal IDs, PII
   - Attack chains: combined endpoint abuse
   - Race conditions in concurrent operations
   - Regression risks: removed guards

6. **Magic Strings / Hardcoded Values** — Severity: P2
   - Hardcoded colors that should use design tokens
   - Magic strings that should be constants/enums
   - Hardcoded URLs, API paths, config values
   - Repeated string literals

### Step 3.7: Deduplicate Findings Using Subagent

After all 6 analysis subagents return, combine their JSON arrays into a single array and spawn a **deduplication subagent** with this prompt:

---
You receive a JSON array of code review findings from multiple analyzers. Your job is to deduplicate and consolidate them.

Input findings:
\`\`\`json
{combined_findings_json}
\`\`\`

Rules:
1. Remove exact duplicates: same `file` + `line` + substantially similar `issue` text.
2. Merge overlapping findings: if two entries reference the same file and line (or lines within 2 of each other) for related reasons, combine them into one entry. Keep the higher severity (P0 > P1 > P2). Merge the issue descriptions into one coherent sentence.
3. Preserve the JSON format exactly: `[{"file": "...", "line": ..., "issue": "...", "severity": "P0|P1|P2"}]`
4. Do not invent new findings. Do not remove findings unless they are duplicates or merged.
5. Return the deduplicated JSON array and nothing else.
---

### Step 3.8: Fact-Check Findings Using Subagent

After the deduplication subagent returns, spawn a **fact-checker subagent** with this prompt:

---
You are a fact-checker for code review findings. Verify each finding against the actual code and diff.

Deduplicated findings:
\`\`\`json
{deduplicated_findings_json}
\`\`\`

Changed files: {files_list}
{If conventions were loaded: Conventions from AGENTS.md/CLAUDE.md/GEMINI.md: {conventions_content}}

For each finding, do ALL of the following:
1. Run `{diff_command}` and read the relevant file to check the line number. Confirm the line number references the code described in the issue. If the line is off, check nearby lines (within 5 lines). If found nearby, correct the line number. If the described code doesn't exist near that line, remove the finding.
2. Read the code at that line and verify the issue description is actually true. If the claim is wrong (e.g., says a variable is unsanitized but it is sanitized upstream, or says a test is missing but the test exists), remove the finding.
3. For convention-related issues, verify the claim against the actual conventions text provided above. If the convention doesn't exist or the code actually complies, remove the finding.

Return a JSON object:
\`\`\`json
{
  "verified": [{"file": "...", "line": ..., "issue": "...", "severity": "P0|P1|P2"}],
  "removed": [{"file": "...", "line": ..., "issue": "...", "reason": "Why it was removed"}]
}
\`\`\`

Be strict: only keep findings you can confirm by reading the code. When in doubt, keep the finding but correct any inaccuracies in the issue text or line number.
---

Use the `verified` array as the final findings for the review report. If any findings were removed, include them in a collapsed section at the end of the report.

## Step 4: Generate review report

Use the **verified findings** from Step 3.8 as the source for the report below.

Output the review in the following format:

```
# Code Review Report

## Summary
- Branch: [branch-name]
- Commits reviewed: [N or all]
- Files changed: [count]
- Lines added/removed: [count]

## CRTK Context
[If CRTK was used, briefly list: how many past review comments were found, which tags were searched, and any key conventions that informed the review. If CRTK was not available, state "CRTK tribal knowledge was not available for this review."]

## P0 - Critical Issues
1. [File: path/to/file]
   - [Description of issue]
   - Suggested fix: [How to fix]

## P1 - High Priority Issues
1. [File: path/to/file]
   - [Description of issue]
   - Suggested fix: [How to fix]

## P2 - Medium Priority Issues
1. [File: path/to/file]
   - [Description of issue]
   - Suggested fix: [How to fix]

## Approved Files
[List files that follow all conventions]

## Removed Findings (Fact-Check)
[If any findings were removed by the fact-checker, list them here with reasons. If none were removed, omit this section.]
```

If no issues are found, output: "Code review passed. No convention violations found."

## Step 5: Recommendations

Provide 2-3 actionable recommendations for improving code quality based on the patterns observed.
