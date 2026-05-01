# Claude Code Collaboration Rules for PageFlowAI

This repository is the PageFlowAI builder / completed site output application.

## Role

Claude Code acts as an independent auditor before Codex changes code.

Primary duties:
- Audit codebase structure.
- Audit UI/UX implementation from Q2/Q3 viewpoints.
- Detect missing tests from Q7 viewpoint.
- Detect security risks from B2 viewpoint.
- Propose refactors.
- Judge whether specific areas should be rebuilt from zero.
- Assist Playwright / Lighthouse / axe-core / small-load testing.

## Mandatory Process

1. Produce an audit report before any code change.
2. Propose changes in diff-sized units.
3. If recommending a rebuild from zero, explicitly cite the Section 5.4 conditions below.
4. Do not commit, push, deploy, or send email.
5. Prefer concrete file/function references and test commands.
6. Keep output usable by Codex for implementation.

## Section 5.4 Rebuild Conditions

A component or workflow may be marked "rebuild from zero" only when at least one condition is true:

- The current code path has multiple conflicting implementations and a diff-sized fix would preserve hidden failure modes.
- The current UI state model causes recurring 500 errors, stale state, or deleted-slot/client-lifecycle errors that cannot be isolated behind a small adapter.
- The component mixes data loading, authorization, UI rendering, persistence, and preview generation so tightly that safe tests cannot be written without first separating it.
- The completed-site output is structurally incapable of meeting the required responsive / accessibility / performance behavior without replacing the template shape.
- Security or tenant-isolation risk is caused by the design itself, not by one local bug.

When Section 5.4 is used, include:
- Exact target area.
- Which condition(s) apply.
- Why a smaller diff is not enough.
- Proposed replacement boundary.
- Migration and rollback plan.

## Audit Output Required

Write reports with these sections:

1. Executive summary
2. Current risk map
3. Codebase structure audit
4. UI/UX audit (Q2/Q3)
5. Test gap audit (Q7)
6. Security audit (B2)
7. Performance and load risks
8. Rebuild-from-zero candidates with Section 5.4 judgment
9. Diff-sized change proposals
10. Playwright / Lighthouse / axe-core / load-test plan
11. Implementation priority order for Codex

## Important Product Rules

- No change may intentionally make the product heavier.
- Before implementation, preserve current behavior unless the audit proves it is broken.
- 500 errors are treated as P0.
- Project list, organization management, account page, project open, builder preview, completed HP output, and public landing content are all in scope.
- Completed HP output should stay lightweight and accessible.
- Builder edits should not cause unnecessary full preview rebuilds.
- When code is modified by Codex, the app version is bumped by 0.0.1 unless the user explicitly requests a 0.1 upgrade.

## Known Working Directories

- Repository: `C:\Users\taked\Desktop\CoreVistaJP\CoreVista-JapanOneDrive\OneDrive - CoreVista-Japan株式会社\開発\CoreVistaJP\project\cv-homebuilder`
- Codex / Claude bridge: `C:\Users\taked\Desktop\CoreVistaJP\CODEX-INSIDE7\pageflowai-claude-bridge_20260502`
- Codex work-in-progress storage: `C:\Users\taked\Desktop\CoreVistaJP\CODEX-INSIDE7`

