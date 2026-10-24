# Start with the right workflow

Choose by the work's purpose, then let the coordinator recommend assurance from effects and uncertainty. The [toolbox specification](toolbox.md) defines the shared rules; the [worked examples](worked-examples.md) show seven routing and acceptance walkthroughs.

## 1. Choose one entry point

| Your request | Download | First output |
| --- | --- | --- |
| Something violates its intended behavior | [Bug-fix kickoff](downloads/bug-fix-kickoff.txt) | Reproduction or precise limitation and bounded repair plan |
| An existing product needs changed behavior | [Feature-change kickoff](downloads/feature-change-kickoff.txt) | Impact/design and first useful increment |
| A new product needs discovery and feasibility | [New-product kickoff](downloads/new-product-kickoff.txt) | Bounded discovery/feasibility plan and first-release decision |

For a task already using the full feature package, use the [extended recipe](extended-feature-start.md). Do not silently replace an active project's acceptance or authority with a different workflow.

## 2. Fill the shared brief

Download the [shared work contract](downloads/work-contract.txt) and provide it together with the chosen kickoff. Fill the outcome, source baseline, acceptance, delivery boundary and existing authority. Include applicable target-repository rules and native check recipes where known. The coordinator can discover routine implementation details and ask concise consequential questions.

Choose local edits, Git actions, installation/deployment and external publication separately. Optional outputs and recurring monitoring are off unless authorized. Required affected documentation still follows project rules. No internal source archive or private project data belongs in a public example.

## 3. Record assurance and decisions

The coordinator records lightweight, standard or elevated assurance and why. Roles are responsibilities; separate workers exist for useful independent work, not a fixed crew size. A settled clear repair instruction does not require a second approval. An unresolved behavior, material change or missing authority needs a concrete decision.

## 4. Execute and accept a useful increment

Use early safe builds/checks, one product writer and explicit shared-resource ownership. Identify the candidate and assess it under the selected profile. Record actual behavior evidence, findings and limits. Consume completion and dispatch ready authorized work; show dependencies instead of stale activity labels.

The new profiles are manually reviewed contracts. The current `check_gate.py` remains a structural check for the [extended format](gates.md#validate-the-example-record); it is not a validator for these contracts or a proof of behavioral correctness.

## 5. Deliver and learn

Demonstrate the agreed user journey and state the final candidate, actual checks, coverage gaps, maintained documentation and receiving owner. Distinguish local acceptance, commit, install and release. Record proportionate learning and selected follow-up actions. Start with one bounded bug-fix pilot before expanding orchestration automation.
