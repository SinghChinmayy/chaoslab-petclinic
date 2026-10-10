# Change History

This file records meaningful, user-facing changes to the project. Detailed implementation history remains in Git.

## Unreleased

### Added

- Established a project change history and commit-tracking convention.
- Added cAdvisor-backed per-container CPU, memory, and network metrics to Prometheus, with setup and verification guidance.
- Added a project quick-start guide covering Docker Desktop setup, service startup, and monitoring verification.
- Added a root Makefile for building, starting, and stopping the sandbox.

### Changed

- Integrated the Spring PetClinic microservices source tree into the main repository for unified version control.
- Made the GenAI service optional so PetClinic runs without OpenAI or Azure credentials.
- PetClinic services now run images built from this repository's source instead of pulling published images.
- cAdvisor is now optional (`make up PROFILES=cadvisor`), so the sandbox also starts on Docker setups where it cannot read containers.
- Tracing and monitoring containers restart automatically; application services keep no restart policy so experiments can expose it.

### Fixed

- cAdvisor failed to start on newer Docker engines because of how the Docker socket was mounted.
- Zipkin ran out of memory after about 30 minutes; its in-memory trace storage is now capped.

### Removed

### Security
