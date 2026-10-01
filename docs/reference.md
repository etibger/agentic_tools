# Documentation delivery and publication boundaries

The guide contains generic workflow templates, communication diagrams and a synthetic dashboard example. The user-provided agent relationship figure was reviewed for generic workflow content, cropped to the diagram and re-encoded without metadata. Its exact file hash is allowed by the publication check. Company-source documents, internal screenshots, captured metadata and private-service links remain excluded.

## Build and preview

~~~sh
uv sync --locked
uv run mkdocs build --strict
uv run mkdocs serve --dev-addr 127.0.0.1:8079
~~~

The site uses local theme assets and system fonts. Generated site/ stays outside Git. The build creates downloads from the canonical templates.

This repository is private at [etibger/agentic_tools](https://github.com/etibger/agentic_tools). The README opens the guide locally. [Successful documentation runs](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) provide generated-site artifacts to authenticated repository readers.

## Check new content before publication

~~~sh
uv run python scripts/check_publication.py
~~~

Review all source files, diagrams, screenshots and downloadable artifacts for credentials, proprietary information and internal references. Company-source documents belong in an approved company system, outside this repository and its generated artifacts.

The automated check blocks known imported-archive paths, internal-service URLs and common credential formats. Binary files require a separately reviewed path and content hash; changing the approved diagram requires another review. It cannot establish that every piece of prose is safe or detect every secret format; semantic review remains necessary. Synthetic examples must be labelled.

## Repository and site access are separate

Public Pages publication is off. The workflow builds on push; deployment is an explicit opt-in dispatch after destination and audience approval.

A private repository does not automatically make a Pages site private. [Private Pages publication requires an Enterprise Cloud organization](https://docs.github.com/en/enterprise-cloud@latest/pages/getting-started-with-github-pages/changing-the-visibility-of-your-github-pages-site).

For an authorized site, use GitHub Pages settings with GitHub Actions as the source, then dispatch the documentation workflow with publication enabled. Replace the README link with the actual verified deployment URL. [Material's publication guide](https://squidfunk.github.io/mkdocs-material/publishing-your-site/) explains MkDocs hosting.

For private use on a personal account, preview locally or download the authenticated build artifact. Neither is represented as an access-controlled hosted Pages site.
