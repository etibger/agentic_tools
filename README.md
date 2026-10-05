# Agentic Tools

Reusable templates for evidence-led, multi-agent software development. Refined through a completed software implementation and its retrospective, with safeguards for stalled handoffs, weak test oracles and misleading dashboard state.

**[Open the generated documentation](https://etibger.github.io/agentic_tools/)** · [Download the Confluence workflow PDF](docs/assets/coordinated-multi-agent-workflow.pdf) · [Kickoff template](templates/feature-kickoff.md)

The link opens the live GitHub-hosted HTML guide from any device. Both the guide and this source repository are public. Validated main-branch changes deploy automatically. [Documentation builds](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) also retain a downloadable mkdocs-site artifact (sign in, open a successful run, and download it).

## Start a feature

1. Copy [feature-kickoff.md](templates/feature-kickoff.md) into your task context and fill the project brief.
2. Supply the Confluence parent page and audience, then give the completed prompt to your coordinator. Submission authorizes the default five-minute watchdog and the named DI/lessons outputs; check available tools and repository policies.
3. Review the concrete investigation, design and ordered gates before authorizing implementation.
4. Follow the live dashboard, including “Approvals needed from you”, and concise decision requests. The coordinator owns every completion-to-next-task handoff.

The defaults include a served live dashboard, a five-minute recurring watchdog, a verified Confluence DI page before implementation/testing and a lessons-learned child page under the DI at closure. The template is a prompt and a set of contracts, not an autonomous orchestration service. Copying it does not start agents, timers, publication or recurring automations.

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
| templates/ | Kickoff, design investigation, assignments, gates, retrospective and example evidence |
| docs/ | MkDocs guide, communication figure and interactive synthetic dashboard |
| scripts/ | Documentation build hook, gate-record, dashboard and publication checks |
| .github/workflows/ | Strict documentation build and main-branch Pages publication |
| uv.lock | Reproducible documentation dependencies |

## Publication boundaries

This kit contains generic workflow guidance, reviewed diagrams, synthetic dashboard data and the original exported Confluence workflow PDF. The PDF was explicitly approved for public inclusion and retains its original figures, historical project states and reference links. Other company-source archives and raw page captures are excluded.

Before sharing new content, review its provenance and audience. Keep other company references in an approved company system unless their publication is explicitly authorized. The publication check catches known archive paths, internal-service links and common credential formats, but does not replace a content review.

No product source or private implementation archive is included.
