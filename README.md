# Agentic Tools

Reusable templates for evidence-led, multi-agent software development. Refined through a completed software implementation and its retrospective, with safeguards for stalled handoffs, weak test oracles and misleading dashboard state.

**[Open the generated documentation](http://127.0.0.1:8079/)** · [Kickoff template](templates/feature-kickoff.md)

The documentation link above opens the locally generated site on the originating workstation. The repository remains private. Public Pages deployment is off. [Download the generated site from a successful documentation build](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) (sign in, open a successful run, and download the mkdocs-site artifact).

## Start a feature

1. Copy [feature-kickoff.md](templates/feature-kickoff.md) into your task context and fill the project brief.
2. Give the completed prompt to your coordinator. Check available agent capacity and repository policies.
3. Review the concrete investigation, design and ordered gates before authorizing implementation.
4. Follow the dashboard and concise decision requests. The coordinator owns every completion-to-next-task handoff.

The template is a prompt and a set of contracts, not an autonomous orchestration service. Copying it does not start agents, timers, publication or recurring automations.

## Build and preview the guide

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then run:

~~~sh
uv sync --locked
uv run mkdocs build --strict
uv run mkdocs serve --dev-addr 127.0.0.1:8079
~~~

Generated HTML lives in site/ and stays outside Git. The build copies canonical templates into downloadable files. The GitHub Actions workflow builds the same site and retains a downloadable artifact.

## Repository contents

| Path | Purpose |
| --- | --- |
| templates/ | Kickoff, design investigation, assignments, gates, retrospective and example evidence |
| docs/ | MkDocs guide, communication figure and interactive synthetic dashboard |
| scripts/ | Documentation build hook, gate-record, dashboard and publication checks |
| .github/workflows/ | Strict documentation build and opt-in Pages publication |
| uv.lock | Reproducible documentation dependencies |

## Publication boundaries

This kit contains generic workflow guidance, original generic diagrams and synthetic dashboard data. Company-source documents, internal screenshots, captured page metadata and private links are excluded.

Before sharing new content, review its provenance and audience. Keep company references in an approved company system; do not copy them into this repository or generated-site artifacts. The publication check catches known archive paths, internal-service links and common credential formats, but does not replace a content review.

No product source or private implementation archive is included.
