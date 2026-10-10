# PetClinic chaos engineering prototype

This repository contains a local Spring PetClinic microservices environment for controlled chaos-engineering experiments. The application, Prometheus, Grafana, Zipkin, and optionally cAdvisor run through Docker Compose. The optional GenAI service is disabled by default, so an OpenAI or Azure OpenAI API key is not required.

## Requirements

- Docker Desktop with at least 8 GB of memory
- Docker Compose
- JDK 17 or newer, to build the PetClinic images from source
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

The sample data should return owner `6` with four visits.

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

