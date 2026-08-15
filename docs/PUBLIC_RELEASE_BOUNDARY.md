# Public release boundary

This repository is a competition-safe edition built from a source snapshot. It does not share Git history with the private development repository.

## Allowed content

- Application source required to run the competition edition.
- Public API contracts and user-interface code.
- Synthetic project, organization, meeting, task, risk, milestone, and acceptance data.
- A small, manually reviewed set of generic industry knowledge cards.
- Tests that use temporary or synthetic fixtures only.
- Required third-party copyright and license notices.

## Content that must remain private

- Customer names, employee names, contact details, account identifiers, and credentials.
- Real project documents, meeting transcripts, minutes, workbooks, mind maps, uploads, reports, databases, logs, and generated output.
- Project-specific facts, evidence excerpts, source mappings, parameter values, and internal operating rules.
- Proprietary knowledge-distillation material and private advanced orchestration components.

## Release rules

1. Only files on the public allowlist may enter the repository.
2. Runtime data is generated locally and is ignored by Git.
3. Model credentials are accepted locally at runtime, are never returned by the API, and are not stored in project files or SQLite.
4. Public demo data must be generated from fictional scenarios, not transformed row-by-row from customer data.
5. A fresh clone, secret scan, sensitive-term scan, test run, and frontend build are required before publication.

