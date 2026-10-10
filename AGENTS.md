# Project brief: Agent-driven chaos engineering for Spring PetClinic

We are building a two-day hackathon prototype. An AI planner should choose and run controlled failure experiments against a **local sandbox application**. A separate analysis agent should explain the observed impact and recommend remedies. The system must automatically stop or reverse an experiment when its time limit or safety threshold is reached.

## Application under test

Public repo: https://github.com/spring-petclinic/spring-petclinic-microservices

Use its Docker Compose deployment.

A valuable request path is `GET /api/gateway/owners/{ownerId}`. The Gateway gets owner data from Customers, then visit records from Visits. Its circuit breaker can return an owner with **empty visits** when Visits fails. Therefore, measure response correctness as well as HTTP status and latency: a `200` response can still represent degraded service.

## Change history and Git commits

Use `history.md` as the human-readable companion to the Git history.

- Update `history.md` for every meaningful code, configuration, documentation, or operational change.
- Add the history entry in the same commit as the change it describes.
- Keep new entries under `## Unreleased`, grouped under `Added`, `Changed`, `Fixed`, `Removed`, or `Security`.
- Write entries as concise, user-facing outcomes rather than implementation logs. One entry may summarize several tightly related files in a single commit.
- Start the commit subject with the same category in lowercase (for example, `added:`, `changed:`, or `fixed:`) and keep its wording aligned with the history entry.
- Do not add entries for merge commits, formatting-only changes, or edits that only maintain `history.md` itself.
- When preparing a release or demo checkpoint, rename `Unreleased` to a dated heading in `YYYY-MM-DD` format and create a fresh `Unreleased` section above it.
- Treat Git as the detailed record of who, when, and which lines changed; do not duplicate commit hashes or file-by-file details in `history.md`.

