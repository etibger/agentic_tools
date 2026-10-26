# Bug-fix kickoff — toolbox v0.2

Use with the completed `work-contract.md`. Begin from the user's existing authority; this template does not add publication, scheduling, Git or installation permissions.

## Defect brief

- Symptom / expected behavior and its source:
- Smallest known input / affected version and environment:
- Actual observation / evidence versus hypothesis:
- Known-good behavior / affected consumers:
- Data or services that must remain untouched:

## Execute

Before investigation, show the shared contract's project structure, initial team, provisional execution team, proposed models/effort and investigation bounds. This introduction does not launch implementation work.

1. Confirm the defect against the intended contract. Reproduce with the smallest relevant observation, or identify why reproduction is blocked. Inspect source and native recipes; distinguish a product defect from data, fixture, oracle and environment errors. Do not invent a failing run.
2. Trace root cause and direct consumers. State the smallest repair and relevant compatibility boundary. If intended behavior is unresolved or the repair changes the product contract, present that choice and route the accepted change through feature-change kickoff. Preserve the investigation and existing authority.
3. Present the Detailed Investigation and request Concept Approval for its exact revision: observed cause/uncertainty, proposed repair and alternatives, tests/acceptance, ordered tasks, assurance, team/model settings, resources, estimates and stop conditions. Implementation waits for the user's explicit approval or task-specific waiver. A broad repair request does not waive this gate. Preserve existing approval for the exact package; do useful independent preparation while a dependent decision waits. After approval, confirm the actual execution team.
4. Implement the bounded repair and a meaningful regression where warranted. Demonstrate failing-before/passing-after behavior when feasible; retain an explicit limitation if the baseline cannot execute. Avoid unrelated cleanup and do not rewrite the real data to satisfy a test.
5. Run cheap normal checks promptly, freeze the candidate and obtain the profile's independent assessment. For a shared-contract finding, inspect direct consumers and immediate ordering/error boundaries. Correct confirmed defects through the product writer and reassess affected evidence.
6. Deliver the agreed local/release boundary with observed correction, regression results, final identity, affected documentation, unresolved limits and maintenance owner. Record a short learning note and any selected actions. Do not manufacture a full team meeting for a small fix.

Stop or replan when reproduction is unreliable, cause remains speculative beyond the agreed investigation budget, effects exceed scope or a required resource/check is unavailable. Report concrete options and preserve the failing evidence. A blocked required check cannot become a pass.
