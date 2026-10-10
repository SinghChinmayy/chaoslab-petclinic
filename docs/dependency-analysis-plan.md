# Service dependency and blast-radius plan

Status: proposed for the local Spring PetClinic Compose sandbox, 2026-10-10.

## Findings from this repository

The existing Python `chaoslab` package is the natural home for this subsystem. `chaoslab/depmap.py` already builds `reports/dependency-map.json`, Markdown, and HTML from Compose, Docker instance counts, Eureka registrations, Zipkin dependencies and successful traces, Locust request counts, and `map-overrides.json`. `chaoslab/experiment.py` reads the saved action paths and compares per-action HTTP failure rates with baseline; it writes `runs/<id>/result.json` and `events.jsonl`. Fault injection and safety controls are separate modules. There is no agent implementation or dedicated dashboard directory in the current checkout; the present visual outputs are the static dependency-map HTML and Grafana monitoring dashboard.

| Finding | Evidence | Consequence |
| --- | --- | --- |
| Gateway routes `/api/customer/**`, `/api/visit/**`, and `/api/vet/**` to the three business services through `lb://` names. GenAI is an optional Compose profile. | `spring-petclinic-microservices/spring-petclinic-api-gateway/src/main/resources/application.yml`; `spring-petclinic-microservices/docker-compose.yml` | Route dependencies are known from source even if a route has no recent trace. Keep disabled profile services in the catalog but mark them inactive. |
| `GET /api/gateway/owners/{ownerId}` first fetches Customers, then Visits. Only the Visits call is wrapped in a circuit breaker whose fallback supplies empty visits. | `ApiGatewayController.java`, `CustomersServiceClient.java`, `VisitsServiceClient.java` under `spring-petclinic-api-gateway/src/main/java/.../api/` | Customers failure can prevent owner details; Visits failure can produce HTTP 200 with incomplete data. These are different outcomes for the same feature. |
| Gateway's generic route circuit breaker forwards to `/fallback`, which returns 503. | Gateway `application.yml`; `FallbackController.java` | Do not transfer the empty-visits behavior to ordinary proxied routes. |
| Compose waits for Config and Discovery health before starting application services; Docker-profile application configuration imports Config Server without `optional:`. | `docker-compose.yml`; each service's `application.yml` | Config Server has a strong startup/restart dependency. A running service's immediate response to its loss is not established by that fact. Compose `depends_on` alone is only orchestration evidence, not proof of runtime calls. |
| Gateway and business services use Eureka clients or load-balanced routes. | Maven dependencies, gateway routes, Compose, saved Eureka registrations | Discovery affects registration and lookup, but the effect on already cached service addresses needs a controlled observation. |
| Vets has `@Cacheable("vets")` with caching enabled only for the `production` profile. The Docker image sets `SPRING_PROFILES_ACTIVE=docker`, so live cache activation needs checking. | `CacheConfig.java`, `VetResource.java`, vets `application.yml`, `docker/Dockerfile` | Warm and cold cache outcomes may differ; do not claim a cache protects Docker-profile vets requests without verification. |
| The saved map names `customers-db`, `visits-db`, and `vets-db` as embedded datastores based on trace-derived synthetic nodes. There are no separate DB Compose services. | `chaoslab/depmap.py`; `docker-compose.yml`; `reports/dependency-map.json` | Model these as in-process resources only after checking the active datasource configuration. Do not offer them as independently faultable containers. |
| Current Compose inspection showed the core services and monitoring containers running, while `load-generator` was absent. The saved map describes an earlier 15-minute Zipkin window and Locust traffic sample. | Read-only `docker compose ps --all` on 2026-10-10; `reports/dependency-map.json` | Every runtime observation needs `observed_at`, source availability, and staleness. Recheck Locust before using request shares or validating predictions. |

The existing map is a useful discovery input, but it does not yet express dependency purpose, failure mode, fallback scope, evidence confidence, or separate runtime and restart impact. Its `critical` flag is based on request share >= 50% or two startup dependents. That flag is a traffic/reach hint, not a failure-severity conclusion. Also, a trace can show a call without proving it is required, and a missing trace cannot disprove a source-defined call. `map-overrides.json` currently corrects an action's service *set*; a set is not enough to explain call order or fallback placement.

## Discovery and verification

1. Build a static catalog from active Compose services and profiles, gateway routes, the two owner-details WebClient calls, config imports, Eureka client configuration, and the known Vets cache. Retain file and symbol references for each assertion. Keep the repo's existing manual override mechanism for facts static parsing misses, with a reason and review date.
2. Reconcile live Docker state, Eureka registration, actuator health where available, Zipkin edges/traces, Prometheus target health, and Locust action counts. Treat these as time-bounded observations. Zipkin's sampled or missing calls are corroboration, not a complete inventory. Never create a dependency merely because two services share a network or Prometheus scrapes both.
3. Give each relationship a status: `verified` (direct source/config proof or an observed call consistent with a declared relationship), `inferred` (plausible but not proven, including synthesized datastore edges), or `unknown` (a feature needs a component whose relationship or behavior is unresolved). A relationship can have multiple evidence records. Source and runtime evidence should be shown separately. Contradictions should produce a warning, not silent overwriting.
4. Test selected behavior in the local sandbox only after the normal chaos preflight and safety controls. Record the run as validation evidence, never as a replacement for static discovery.

## Representation choice

| Approach | Fit here | Decision |
| --- | --- | --- |
| Extend the current flat node/edge/action JSON with typed dependency and feature records | Reuses the Python CLI, report generation, and experiment results; small enough for a hackathon | **Choose for V1.** A versioned JSON snapshot is the common contract. Python dataclasses or typed dictionaries with validation are sufficient; no graph database is needed. |
| Infer impact directly from Zipkin trace paths | Low implementation cost, but trace gaps, sampling, and fallbacks hide important calls | Use traces as evidence and validation, not as the sole model. |
| Add a graph database and generic scoring engine | Offers queries and flexible storage but adds setup, synchronization, and arbitrary scoring pressure | Defer until graph size or query volume demands it. |

Recommended files: keep `chaoslab/depmap.py` as collection and rendering entry point; add a small schema/validation module and a pure `chaoslab/blast_radius.py` rule evaluator. Move static semantics to a reviewed JSON manifest beside `map-overrides.json` instead of embedding PetClinic rules in the evaluator. Preserve the current `python3 -m chaoslab map` workflow; add `python3 -m chaoslab impact <target> --failure <kind>` that reads a snapshot without injecting a fault. One versioned JSON output supplies the CLI, later agent, and dashboard. The existing HTML report can consume it immediately; a future dashboard may read a static export or a small read-only local API over the same schema.

### V1 contract

Use stable service IDs matching Compose names. Snapshot metadata includes `schema_version`, generation time, environment/profile, source status and collection time, and warnings. Nodes have kind (`service`, `platform`, `in_process_resource`, `external`), active/running/health/registration observations, and evidence. Dependencies point **consumer -> provider** and include:

- `purpose`: `request_data`, `routing`, `service_discovery`, `configuration`, `persistence`, `observability`, or `other`;
- `phase`: `runtime`, `startup`, `restart`, or a combination, with an explicit `unknown` where needed;
- `interaction`: synchronous call, discovery lookup/registration, config fetch, local resource, or unknown;
- `requirement`: required, optional, or unknown **for a named feature and phase**, rather than one global edge weight;
- `on_failure`: fail request, serve partial response, serve cached result, delay/timeout, block startup, or unknown, scoped to a failure mode;
- `evidence`: source type, locator, observation time, detail, and verification status.

Features/actions are separate records with route, entry point, ordered call steps or small expression trees (`all_of`, `one_of`, `fallback`), expected response fields, and a correctness oracle. For owner details: Customers is required for owner data; Visits enriches the result; the Visits circuit breaker may replace Visits with an empty list. The oracle must distinguish a real zero-visit owner from lost visit data. Use a known fixture such as owner 6 with a baseline visit count, or compare against direct Visits data just before injection; keep changing visit counts and probe timing in mind. Unknown dependencies remain explicit rather than becoming absent edges.

An illustrative edge is:

```json
{
  "consumer": "api-gateway",
  "provider": "visits-service",
  "feature": "owner details",
  "purpose": "request_data",
  "phase": ["runtime"],
  "interaction": "synchronous_http",
  "requirement": "optional_for_response_required_for_complete_data",
  "on_failure": {"unavailable": "empty_visits_fallback", "slow": "unknown_until_timeout_test"},
  "verification": "verified",
  "evidence": [{"kind": "source", "locator": "ApiGatewayController.getOwnerDetails", "detail": "Circuit breaker returns empty Visits"}]
}
```

The illustrative enum value can be normalized during implementation; the key requirement is to preserve the distinction between response availability and data completeness. Use JSON Schema or equivalent validation and document migrations when changing `schema_version`.

## Qualitative impact rules

Input is `(snapshot_id, target, failure_type, phase, optional feature)`. Normalize failure types from the existing controller: kill, pause, delay, packet loss, CPU limit, and traffic spike. Model `kill` as unavailable; pause as unresponsive; delay/loss/CPU/spike as potential latency or intermittent failure unless measured thresholds justify a stronger outcome. A scenario must not imply that a configured fallback always activates: timeout settings, circuit state, and actual fault direction matter.

1. Start with the target's directly affected capabilities. Traverse reverse dependency edges, but evaluate only edges whose phase and failure mode apply. A reached service is a candidate, not an automatic failure.
2. Evaluate feature expressions in order. A required synchronous call without a usable fallback predicts `unavailable` for that feature. A fallback that returns incomplete data predicts `degraded` with HTTP status possibly 200. A proven warm cache may predict `preserved` for a specific read, while cold-cache behavior remains separate. For an uncertain edge or unspecified fault behavior, report `uncertain` and the missing fact.
3. Propagate a service outage to downstream features only where those features need that service. Do not mark the dependent process dead unless there is evidence for process failure. Keep `runtime` and `startup_restart` result trees separate.
4. Return each affected feature/component, outcome (`unavailable`, `degraded`, `preserved`, `uncertain`), expected status/data/latency where known, dependency path, rule, evidence IDs, assumptions, and uncertainties. Deduplicate multiple paths while retaining alternative explanations. Detect cycles and bound traversal. Traffic share is optional context for prioritization, never a numeric severity weight.

Example: Visits unavailable predicts `owner details` degraded with empty visits and possibly HTTP 200; direct `/api/visit/**` requests unavailable through Gateway's 503 fallback. Customers unavailable predicts owner details unavailable because owner lookup has no local fallback. Config Server unavailable predicts possible restart/startup failure for Docker-profile clients; running request impact is uncertain until observed. Discovery Server unavailable predicts registration/recovery trouble and uncertain immediate routing impact because existing clients may retain addresses. These are predictions, not verified experiment outcomes.

## Consumer and feedback flow

The planner reads the same snapshot and impact API as the dashboard. It may use qualitative impact to choose a meaningful, allowed fault, but the existing `chaoslab.safety` preflight, time limit, watchdog, kill switch, and dead-man's switch remain the only authority for execution. Discovery and impact evaluation are read-only. The analysis agent receives the scenario prediction plus `result.json`, raw events, and probe data and explains observed status, latency, **correctness**, and recovery. The dashboard renders service state, dependency semantics/evidence, a runtime/restart impact switch, uncertainty, and prediction-versus-observation after a run. It must label stale telemetry and inactive profiles.

Save each prediction with schema/snapshot ID, scenario, timestamp, and evaluator version beside the run result. Comparison should map each predicted feature/outcome to observed HTTP status, semantic probe result, latency, and recovery, using `matched`, `contradicted`, or `unobserved` rather than tuning weights. Record an evidence-backed correction in the manifest after reviewing contradictions. A lack of sampled requests is `unobserved`, not success.

## Two-day implementation sequence

| Milestone | Work and test | Acceptance criteria |
| --- | --- | --- |
| 1. Contract and evidence (day 1 morning) | Define schema and static manifest; convert existing Compose/source/override facts; add fixtures for absent Zipkin, Eureka, and Locust. | Versioned snapshot validates; every declared edge has evidence/status; unknown and disabled-profile relationships remain visible; no edge is inferred from shared networking. |
| 2. Rule evaluator (day 1 afternoon) | Add pure, deterministic blast-radius evaluator and CLI; test Visits, Customers, Config, Discovery, direct route fallback, cycles, and unknown edges. | Outputs separate runtime/restart results with paths, behavior, evidence, and uncertainties; Visits owner details is degraded, not unavailable; no arbitrary weights or injection. |
| 3. Runtime reconciliation and shared view (day 2 morning) | Add source freshness and live state reconciliation; render semantic edges and impact in existing HTML; expose the same JSON to agent/dashboard consumers. | Map works when telemetry is down, marks data stale, and shows one consistent contract in CLI and view. Live checks compare Docker/Eureka/Prometheus without treating missing observations as no dependency. |
| 4. Validation loop (day 2 afternoon) | Add a semantic owner-details probe and prediction capture/comparison to existing runs; execute one short, allowed Visits experiment with existing guardrails and a clean baseline. | `result.json` links prediction, observed status/correctness/latency/recovery, and match state; fault is reverted on timeout/threshold; an observed 200 with missing visits is reported as degradation. |

## Risks and explicit limits

- **Topology freshness:** The saved map has no per-source TTL and current Locust is absent. Use collection times and degrade to static facts; never present old traffic percentages as live.
- **Trace completeness:** The owner-details call loses trace context in the current sample; source-backed corrections are required. Span-derived datastore names are inferred, and the active datasource should be checked from Config Server before calling them verified.
- **Failure semantics:** Gateway fallback, client caching, Eureka address retention, and startup imports vary by route, profile, and fault. Test the exact scenario before promoting a prediction from inferred to verified.
- **Correctness baseline:** Visit counts can change while Locust writes visits. Use a controlled fixture or just-in-time comparison and record the baseline; status-only Locust metrics cannot detect partial data.
- **Scope:** V1 covers this local Compose deployment and five existing load actions. It does not promise exhaustive Java call graph extraction, full distributed tracing, or generalized probabilistic risk scores.
