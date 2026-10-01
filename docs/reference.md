# Documentation delivery and publication boundaries

The guide contains generic workflow templates, communication diagrams and a synthetic dashboard example. The user-provided agent relationship figure was reviewed for generic workflow content, cropped to the diagram and re-encoded without metadata. Its exact file hash is allowed by the publication check. Company-source documents, internal screenshots, captured metadata and private-service links remain excluded.

## Build and preview

~~~sh
uv sync --locked
uv run mkdocs build --strict
uv run mkdocs serve --dev-addr 127.0.0.1:8079
~~~

The site uses local theme assets and system fonts. Generated site/ stays outside Git. The build creates downloads from the canonical templates.

The intended [GitHub Pages address](https://etibger.github.io/agentic_tools/) is configured, but publication is pending and the site is not live yet. The source repository is public at [etibger/agentic_tools](https://github.com/etibger/agentic_tools). The README labels the hosting status explicitly. [Successful documentation runs](https://github.com/etibger/agentic_tools/actions/workflows/docs.yml) provide generated-site artifacts to signed-in GitHub readers.

## Check new content before publication

~~~sh
uv run python scripts/check_publication.py
~~~

Review all source files, diagrams, screenshots and downloadable artifacts for credentials, proprietary information and internal references. Company-source documents belong in an approved company system, outside this repository and its generated artifacts.

The automated check blocks known imported-archive paths, internal-service URLs and common credential formats. Binary files require a separately reviewed path and content hash; changing the approved diagram requires another review. It cannot establish that every piece of prose is safe or detect every secret format; semantic review remains necessary. Synthetic examples must be labelled.

## Repository and site access are separate

After hosting setup is complete, set the PAGES_ENABLED repository variable to true to enable deployment. Successful pushes to main then publish automatically after all documentation and publication checks pass. Pull requests run the checks without publishing. A manual workflow dispatch on main can redeploy the guide. While the variable is absent or false, builds retain downloadable artifacts without attempting deployment.

The intended Pages guide will be public once activated. A private repository does not automatically make its Pages site private. [Private Pages publication requires an Enterprise Cloud organization](https://docs.github.com/en/enterprise-cloud@latest/pages/getting-started-with-github-pages/changing-the-visibility-of-your-github-pages-site).

Configure Pages to use GitHub Actions as its publishing source before enabling deployment. The workflow uploads only the generated site/ directory and records the deployed URL on the github-pages environment. The MkDocs site_url and README use the same hosted address. [Material's publication guide](https://squidfunk.github.io/mkdocs-material/publishing-your-site/) explains MkDocs hosting.

For private material, use an approved access-controlled destination, or preview locally and share the generated artifact only with authorized readers. The hosted guide and its source-repository access are separate.
