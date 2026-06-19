"""Confluence -> Markdown / Obsidian migration tool.

A free-tool orchestrator that wraps `confluence-markdown-exporter` (cme) for the
Markdown conversion and adds the parts cme does not handle:

* space enumeration (incl. archived / personal)
* attachment inventory + bulk download via the Confluence REST API
* draw.io ``mxfile`` -> editable ``.drawio.svg`` conversion (draw.io Desktop CLI)
* internal-link / page-id rewriting
* count reconciliation + migration report
* Git + Git LFS vault scaffolding

See ``docs/plan.md`` for the full technical design.
"""

__version__ = "0.1.0"
