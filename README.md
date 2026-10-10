# PetClinic chaos engineering prototype

This repository contains a local Spring PetClinic microservices environment for controlled chaos-engineering experiments. The application, Prometheus, Grafana, Zipkin, a Locust load generator, and optionally cAdvisor run through Docker Compose. The optional GenAI service is disabled by default, so an OpenAI or Azure OpenAI API key is not required.

The chaos controller (`chaoslab`) maps the application's dependencies and injects controlled failures behind several safety layers. An AI planner and analysis agent build on it next; see [AGENTS.md](AGENTS.md).

## Requirements

- Docker Desktop with at least 8 GB of memory
- Docker Compose
- JDK 17 or newer, to build the PetClinic images from source
- Python 3.11 or newer, for the chaos controller
- `curl` and `jq` for the verification commands
- Internet access at startup: the config server reads its settings from GitHub

### Docker Desktop setting for cAdvisor

Only needed for per-container metrics. In **Docker Desktop > Settings > General**, turn off **Use containerd for pulling and storing images**, then restart Docker Desktop.

cAdvisor needs Docker Desktop's classic `overlay2` image storage to discover individual PetClinic containers. It may log a warning that it cannot access the containerd socket; this is harmless in this setup because cAdvisor successfully uses the Docker socket instead. With the containerd store, start the stack without cAdvisor; everything else works the same.

Changing the image-store setting can make containers and images from the other store temporarily disappear. Rebuild the images and recreate the stack, but do not delete Docker volumes.

## Start the application

```bash
make build                  # build the PetClinic images from this repository's source (~3 min)
make up PROFILES=cadvisor   # start everything, including cAdvisor, and wait until healthy
```

Use `make up` instead to start without cAdvisor. `make status` shows container health and `make down` stops everything. The application images are always built from this repository's source; Compose never pulls the published PetClinic images. The Gateway needs about a minute after startup before it routes traffic.

The default stack does not start `genai-service`. To start it explicitly, build it, provide the required credentials, and enable its profile:

```bash
cd spring-petclinic-microservices
docker compose --profile genai up -d
```

## Useful endpoints

| Component | URL |
| --- | --- |
| API Gateway | <http://localhost:8080> |
| Eureka | <http://localhost:8761> |
| Zipkin | <http://localhost:9411> |
| Prometheus | <http://localhost:9091> |
| Grafana | <http://localhost:3030> |
| Spring Boot Admin | <http://localhost:9090> |
| Locust (load generator) | <http://localhost:8089> |
| cAdvisor (optional) | <http://localhost:8085> |

## Verify monitoring

```bash
cd spring-petclinic-microservices
docker compose config --quiet
docker compose ps
docker exec prometheus-server promtool check config /etc/prometheus/prometheus.yml
curl -fsS 'http://localhost:9091/api/v1/query?query=up%7Bjob%3D%22cadvisor%22%7D' | jq
curl -fsS 'http://localhost:9091/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv'
```

When started with the cAdvisor profile, `up{job="cadvisor"}` should return `1`, and the PetClinic scrape targets should report `up`.

Verify that the Gateway returns complete visit data rather than only a successful status code:

```bash
curl -fsS http://localhost:8080/api/gateway/owners/6 \
  | jq '{id, name: (.firstName + " " + .lastName), visits: [.pets[].visits[]?] | length}'
```

The sample data gives owner `6` four visits. The load generator never writes to owner `6`, but each chaos experiment adds one canary visit, so expect at least four.

## Container metrics

Example CPU usage in cores, averaged over one minute:

```promql
sum by (container_label_com_docker_compose_service) (
  rate(container_cpu_usage_seconds_total{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor"
  }[1m])
)
```

A value of `1` means one full CPU core, while `0.25` means one quarter of a core.

See the [container resource monitoring guide](spring-petclinic-microservices/docs/container-resource-monitoring.md) for memory and network queries. See the [PetClinic README](spring-petclinic-microservices/README.md) for complete application documentation.

## Dependency map

```bash
python3 -m chaoslab map     # writes reports/dependency-map.{json,md,html}
```

The versioned JSON map is shared by the CLI, future agents, and the HTML report. It combines source-backed feature rules in [dependency-semantics.json](dependency-semantics.json) with Docker Compose, Docker, Eureka, Prometheus, Zipkin, and Locust observations. Dependencies record their purpose, runtime or startup phase, evidence, verification status, and known fallback behavior. Missing or stale telemetry stays visible; it does not erase source-backed dependencies. [map-overrides.json](map-overrides.json) still documents trace gaps. Request share is exposure context, not a dependency weight or failure-severity score.

```bash
python3 -m chaoslab impact visits-service --failure kill         # qualitative prediction
python3 -m chaoslab impact config-server --failure kill --json   # machine-readable contract
python3 -m chaoslab impact visits-service --phase runtime --feature 'owner details'
```

The impact command is read-only. It reports running-service effects separately from startup/restart effects, with dependency paths, fallback assumptions, and uncertainty. Open `reports/dependency-map.html` to inspect the same semantics and switch target, failure, and phase. After a guarded run, the report also shows its prediction-versus-observation comparison when the run used the current snapshot. The [implementation plan](docs/dependency-analysis-plan.md) documents the V1 model, evidence rules, and limits.

## Chaos experiments

```bash
python3 -m chaoslab faults                                    # the failure menu and its limits
python3 -m chaoslab targets                                   # containers that may be faulted
python3 -m chaoslab run kill customers-service                # crash
python3 -m chaoslab run pause vets-service                    # freeze (hung process)
python3 -m chaoslab run delay visits-service --delay-ms 600   # slow network
python3 -m chaoslab run loss visits-service --loss-pct 30     # lossy network (100 = cut off)
python3 -m chaoslab run cpu customers-service --cpus 0.1      # CPU starvation
python3 -m chaoslab run spike api-gateway --multiplier 5      # traffic spike
python3 -m chaoslab status                                    # active faults
python3 -m chaoslab stop                                      # kill switch (or: make kill-switch)
```

Each run measures a baseline, refuses to start unless the system is in a steady state, injects one fault, watches users' error rate, p95 latency, and throughput every second, reverts the fault, and measures recovery. Results go to `runs/<run>/result.json`, and every action is logged in `runs/<run>/events.jsonl`.

Runs also check owner `6` against visit IDs seen in the healthy baseline, after writing one canary visit that must survive the fault. This catches an HTTP `200` with missing visit data: no visits at all is the gateway fallback (`degraded`), while some visits missing after recovery is `data_lost`, reported as recovery with data loss. The correctness check contributes to the watchdog and recovery decision; predictions and `matched`/`contradicted`/`unobserved` comparisons are stored in `result.json` and printed in the summary. Action comparisons count requests from injection until users get normal service back, because requests that hang during a fault often fail after it is undone; an action with fewer than three such requests remains unobserved. A checkpoint is written as soon as a fault is reverted, and the recovery window can be bounded with `--recovery-timeout`.

In one local Visits crash test, the Gateway returned `200` with missing visits. HTTP errors and latency later normalized, but two baseline visit IDs were still absent, so the experiment correctly reported that full recovery had not occurred. The active datastore configuration and durability need verification before treating a service restart as data recovery.

### Safety controls

Any one of these stops a fault:

1. **Allowlist**: only services labelled `chaoslab.target=true` can be faulted; monitoring and the load generator never can. Every failure type has hard parameter limits.
2. **Watchdog**: aborts after 3 s above 25% errors, 2 s p95 latency, or below 30% of normal throughput.
3. **Time limit**: every fault is reverted at its planned end (at most 300 s).
4. **Kill switch**: `python3 -m chaoslab stop` from any terminal reverts everything and restores the sandbox.
5. **Dead-man's switch**: a detached process reverts the fault 30 s after its planned end, even if the controller crashed.
