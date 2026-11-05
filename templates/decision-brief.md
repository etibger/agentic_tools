# Decision brief — [requested action]

Use this short brief for a decision the user actually needs to make. Fill every field in plain language; use “none” or “unknown” with a reason when appropriate. Explain project terms. Keep material limits visible. Reading or preparing this template grants no authority.

Before preparing a new request, read the user's existing authorization and the actual blocker. If the proposed action is already covered by the accepted scope, permissions and remaining bounds, record that basis and continue. For a necessary follow-up, state the prior approval, exactly what changed, and cite the rule requiring a new answer. A tool failure alone is not a new decision. Required AOS saved decisions and exhausted allowances remain enforced. Explain the reason and provide the usable action in the final response as well as progress updates; an unchanged pending decision keeps its existing identity.

- **Requested action:** [One sentence describing the exact choice and affected scope.]
- **Observed problem:** [What happens today; say whether this was observed in real use, in a simulation, or is a concern yet to be tested.]
- **Practical example:** [One concrete before/after example the decision owner can recognize.]
- **Recommendation:** [The smallest useful option and why.]
- **Alternative:** [A realistic alternative, including keeping the current behavior where relevant.]
- **Consequence:** [What approval permits and what declining leaves unchanged.]
- **Additional effort and limits:** [Time estimate/range and uncertainty; separately state the hard time/resource cap and any cost estimate or unknown cost. Do not present a cap as an estimate.]
- **Dependency impact:** [What must finish first, what this blocks or enables, and any effect on other work.]
- **Evidence needed:** [The observable result required, existing evidence and its limits, who checks it, and any required human check.]
- **Stopping point:** [The useful result to return with, and the concrete condition that stops work or requires a new decision.]

For an AOS concept request, use `body` for the requested action and the existing `recommendation`, `alternative` and `consequence` fields. Put the other six fields in `decision_context` as `problem`, `example`, `effort`, `dependencies`, `evidence` and `stopping_point`. All six are required together and each accepts up to 4,096 characters. Keep acceptance criteria and execution bounds in their existing fields; this brief explains them and does not replace them. The saved package binds the complete brief to its approval.

<details>
<summary>Technical references and exact record</summary>

[Package revision, exact source identity, requirement/check IDs, detailed evidence links, implementation notes and authority record. Do not put material risks or limits only here.]

</details>

Before treating the reusable wording as qualified, ask a decision owner to review a representative proposal: can they explain the choice, effort, consequence and stopping point without opening the technical details? Record confusing wording and revise it. A completed template or a passing renderer test is not that review.
