# Change History

This file records meaningful, user-facing changes to the project. Detailed implementation history remains in Git.

## Unreleased

### Added

- Added a shared, evidence-backed dependency snapshot and qualitative impact view for the CLI and report, with runtime and startup predictions, scenario filters, and fallback assumptions.
- Added owner-details correctness monitoring and prediction-versus-observation results to guarded experiments and the report, including recovery checks and accurate handling of unsampled actions.
- Added explicit uncertainty for owner-details datastore paths until the active datasource is verified.
- Added an evidence-based plan for shared dependency mapping and qualitative blast-radius analysis, including runtime versus restart impact and semantic validation.
- Established a project change history and commit-tracking convention.
- Added cAdvisor-backed per-container CPU, memory, and network metrics to Prometheus, with setup and verification guidance.
- Added a project quick-start guide covering Docker Desktop setup, service startup, and monitoring verification.
- Added a chaos controller with six failure types (crash, freeze, network delay, packet loss, CPU starvation, traffic spike), parameter limits, and an experiment flow that checks steady state, injects one fault, watches users every second, reverts, and measures recovery.
- Added safety controls: a target allowlist, a watchdog that aborts on error rate, latency, or throughput limits, a kill switch, and a dead-man's switch that reverts faults even if the controller dies.
- Added an automatic dependency map built from Docker Compose, Docker, Eureka, Zipkin traces, and live traffic, with manual corrections for paths the traces cannot see.
- Added a Locust load generator for steady simulated user traffic, and health checks for the gateway and business services.
- Added a root Makefile for building, starting, and stopping the sandbox.

### Changed

- Integrated the Spring PetClinic microservices source tree into the main repository for unified version control.
- Made the GenAI service optional so PetClinic runs without OpenAI or Azure credentials.
- PetClinic services now run images built from this repository's source instead of pulling published images.
- cAdvisor is now optional (`make up PROFILES=cadvisor`), so the sandbox also starts on Docker setups where it cannot read containers.
- Tracing and monitoring containers restart automatically; application services keep no restart policy so experiments can expose it.

### Fixed

- The owner-details correctness check now uses owner 6 as a fixture the load generator no longer writes to, writes a canary visit before each fault, and reports a service that recovers with lost data as recovered with data loss instead of never recovering.
- Prediction checks now count requests that hang during a fault and fail after it is undone, and leave actions with fewer than three finished requests unobserved instead of contradicted; experiment summaries print the comparison.
- The dependency map report no longer fails when traces show an endpoint outside Compose, and each service's database calls now map to its own embedded HSQLDB, verified by JDBC trace tags, instead of one shared "public" endpoint.
- cAdvisor failed to start on newer Docker engines because of how the Docker socket was mounted.
- Zipkin ran out of memory after about 30 minutes; its in-memory trace storage is now capped.

### Removed

### Security
