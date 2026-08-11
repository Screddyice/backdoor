---
okf_version: "0.1"
---

# Claude Harness Memory

OKF v0.1 knowledge bundle. One markdown concept file per memory entry; every
concept declares a `type` in its YAML frontmatter. Reserved files: index.md
(directory listing) and log.md (chronological history). Conformance check:
`python3 scripts/check-okf.py .claude-harness/memory` (from the plugin root).

* [Decisions](/decisions/index.md) - Recent significant decisions (episodic layer, type: Decision)
* [Failures](/failures/index.md) - Failed approaches to avoid (procedural layer, type: Failure)
* [Successes](/successes/index.md) - Approaches that worked (procedural layer, type: Success)
* [Patterns](/patterns/index.md) - Reusable patterns and conventions (procedural layer, type: Pattern)
* [Rules](/rules/index.md) - Learned rules from user corrections (type: Rule)

Runtime and semantic state remain JSON (not part of the bundle contract):
semantic/*.json, features/, sessions/, agents/, config.json, claude-progress.json.
