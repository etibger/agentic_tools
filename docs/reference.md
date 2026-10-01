# Documentation delivery and publication boundaries

The guide contains generic workflow templates, communication diagrams and a synthetic dashboard example. The user-provided agent relationship figure was reviewed for generic workflow content, cropped to the diagram and re-encoded without metadata. Its exact file hash is allowed by the publication check. The original exported workflow PDF is included with explicit publication approval. Other source archives and raw page captures remain excluded.

## Confluence workflow reference

[Download the original exported workflow page (PDF, 16 pages)](assets/coordinated-multi-agent-workflow.pdf).

This is the original version 5 snapshot, retaining its figures, historical project states and reference links. Some links inside it require access to the original company systems. It is locally typeset from the captured Confluence HTML, as stated on its first page.

The user explicitly approved publishing this PDF. Its exact content hash is allowed by the publication checker; the raw page capture and other imported source files remain excluded.

## Build and preview

~~~sh
uv sync --locked
uv run mkdocs build --strict
uv run mkdocs serve --dev-addr 127.0.0.1:8079
~~~

The site uses local theme assets and system fonts. Generated site/ stays outside Git. The build creates downloads from the canonical templates.

The [GitHub-hosted documentation](https://etibger.github.io/agentic_tools/) is live and publicly readable. The source repository is public at [etibger/agentic_tools](https://github.com/etibger/agentic_tools). The README links directly to the hosted HTML guide. [Successful documentation runs](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) provide generated-site artifacts to signed-in GitHub readers.

## Check new content before publication

~~~sh
uv run python scripts/check_publication.py
~~~

Review all source files, diagrams, screenshots and downloadable artifacts for credentials, proprietary information and internal references. Other company-source documents stay in an approved company system unless their publication is explicitly authorized.

The automated check blocks known imported-archive paths, internal-service URLs and common credential formats. Binary files require a separately reviewed path and content hash; changing an approved diagram or PDF requires another review. It cannot establish that every piece of prose is safe or detect every secret format; semantic review remains necessary. Synthetic examples must be labelled.

## Repository and site access are separate

Pages is configured to use GitHub Actions, and the PAGES_ENABLED repository variable is true. To disable deployment while retaining build checks, set that variable to false. When enabled, successful pushes to main publish automatically after all documentation and publication checks pass. Pull requests run the checks without publishing. A manual workflow dispatch on main can redeploy the guide. While the variable is absent or false, builds retain downloadable artifacts without attempting deployment.

Both this repository and the Pages guide are public. A private repository does not automatically make its Pages site private. [Private Pages publication requires an Enterprise Cloud organization](https://docs.github.com/en/enterprise-cloud@latest/pages/getting-started-with-github-pages/changing-the-visibility-of-your-github-pages-site).

Configure Pages to use GitHub Actions as its publishing source before enabling deployment. The workflow uploads only the generated site/ directory and records the deployed URL on the github-pages environment. The MkDocs site_url and README use the same hosted address. [Material's publication guide](https://squidfunk.github.io/mkdocs-material/publishing-your-site/) explains MkDocs hosting.

For private material, use an approved access-controlled destination, or preview locally and share the generated artifact only with authorized readers. The hosted guide and its source-repository access are separate.
