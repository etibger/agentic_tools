# Agentic Tools

Reusable workflows for bug fixes, feature changes and new products. Choose the work, select assurance by risk, and use shared contracts for evidence, decisions and accountable handoffs.

**[Open the generated documentation](https://etibger.github.io/agentic_tools/)** · [Choose a workflow](docs/quick-start.md) · [Download the Confluence workflow PDF](docs/assets/coordinated-multi-agent-workflow.pdf)

The link opens the live GitHub-hosted HTML guide from any device. Both the guide and this source repository are public. Validated main-branch changes deploy automatically. [Documentation builds](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) also retain a downloadable mkdocs-site artifact (sign in, open a successful run, and download it).

## Choose a workflow

| Work | Entry point |
| --- | --- |
| Repair behavior that violates an intended contract | [Bug-fix kickoff](templates/bug-fix-kickoff.md) |
| Add or change an existing product capability | [Feature-change kickoff](templates/feature-change-kickoff.md) |
| Discover and deliver a first usable product | [New-product kickoff](templates/new-product-kickoff.md) |

1. Read the [toolbox specification](docs/toolbox.md) and use the [worked examples](docs/worked-examples.md) to choose an entry point.
2. Provide the completed [shared work contract](templates/work-contract.md) alongside the selected kickoff. The coordinator records lightweight, standard or elevated assurance from effects and uncertainty.
3. Show the project structure and proposed team/model settings, prepare a proportionate DI, and obtain Concept Approval before implementation. Confirm the actual team, then execute useful increments within the approved authority using native project commands.
4. Accept the agreed user outcome with actual evidence, limits and receiving ownership. The coordinator consumes every completion and dispatches the next ready authorized assignment.

The new entry points are pilot manual contracts. External publication, dashboards, recurring monitoring and separate narrative roles are explicit choices. The current gate checker supports the extended feature record only; automatic execution of arbitrary toolbox contracts is not implemented. The separately bounded AOS runner implements approved bug/feature handoffs. Copying a template does not start agents or services.

The approved [AOS package](docs/aos-concept.md) has independently accepted code and a bounded local desktop journey at candidate `d527ea5`: saved HTTPS decisions, supported commands, a real two-agent Python fixture, automatic handoff, pause/resume, worker replay, actual app close/reopen and reversible local service adoption. The component 0.2.4 source candidate adds generic evidence dispatch, separate reusable pytest/native-build/Cargo adapters, durable product allowances and return navigation after saved decisions. **Herdr execution is not ready:** this candidate still requires independent acceptance, the exact approved native operating proof and receiving-chat verification. The [operator guide](docs/aos-operator.md) identifies the supported route and explicit binding requirements. MCP workflow controls, automatic chat wake-up and general installation/update remain unavailable or deferred. [Qualification boundaries](docs/aos-validation.md) preserve those limits.

## Extended multi-agent feature recipe

The existing [feature-kickoff.md](templates/feature-kickoff.md) remains available for projects explicitly selecting its full package. Its defaults include a live dashboard, five-minute audits, verified Confluence DI before implementation/testing, generated product specification, authorized intermediate checkpoints, lessons publication and a post-retrospective actions milestone. See the [extended quick start](docs/extended-feature-start.md).

These obligations belong to that recipe and existing agreed projects; they are not inherited by the new entry points. Selecting elevated assurance alone does not select this package. Git authority remains explicit in every workflow.

## Build and preview the guide

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then run:

~~~sh
uv sync --locked
uv run mkdocs build --strict
uv run mkdocs serve --dev-addr 127.0.0.1:8079
~~~

Generated HTML lives in site/ and stays outside Git. The build copies canonical templates into downloadable files. The GitHub Actions workflow checks and builds the same site, retains a downloadable artifact, and deploys successful main-branch builds to GitHub Pages once hosting is configured and the PAGES_ENABLED repository variable is set to true. Pull requests are checked without deployment.

## Repository contents

| Path | Purpose |
| --- | --- |
| templates/ | Shared contract, three toolbox kickoffs, extended feature recipe and supporting evidence templates |
| docs/ | Toolbox specification, worked examples, extended guide, communication figure and synthetic dashboard |
| scripts/ | Documentation build hook, gate-record, dashboard and publication checks |
| src/agentic_tools/aos/, src/agentic_tools/check_adapters/ and tests/ | Generic bounded AOS coordination, separate reusable evidence interpreters and regression checks |
| .github/workflows/ | Ubuntu documentation checks/Pages publication and macOS AOS regressions |
| uv.lock | Locked documentation, AOS runtime and development dependencies |

## Publication boundaries

This kit contains generic workflow guidance, reviewed diagrams, synthetic dashboard data and the original exported Confluence workflow PDF. The PDF was explicitly approved for public inclusion and retains its original figures, historical project states and reference links. Other company-source archives and raw page captures are excluded.

Before sharing new content, review its provenance and audience. Keep other company references in an approved company system unless their publication is explicitly authorized. The publication check catches known archive paths, internal-service links and common credential formats, but does not replace a content review.

The public AOS implementation and synthetic fixtures are included. Private project source, conversations, credentials and implementation evidence archives are excluded.
