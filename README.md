# PetClinic chaos engineering prototype

This repository contains a local Spring PetClinic microservices environment for controlled chaos-engineering experiments. The application, Prometheus, Grafana, and cAdvisor run through Docker Compose. The optional GenAI service is disabled by default, so an OpenAI or Azure OpenAI API key is not required.

## Requirements

- Docker Desktop
- Docker Compose
- `curl` and `jq` for the verification commands

### Docker Desktop setting for cAdvisor

In **Docker Desktop > Settings > General**, turn off **Use containerd for pulling and storing images**, then restart Docker Desktop.

cAdvisor needs Docker Desktop's classic `overlay2` image storage to discover individual PetClinic containers. It may log a warning that it cannot access the containerd socket; this is harmless in this setup because cAdvisor successfully uses the Docker socket instead.

Changing the image-store setting can make containers and images from the other store temporarily disappear. Recreate the stack, but do not delete Docker volumes.

## Start the application

```bash
cd spring-petclinic-microservices
docker compose up -d --build
```

The default stack does not start `genai-service`. To start it explicitly, provide the required credentials and enable its profile:

```bash
docker compose --profile genai up -d
```

## Useful endpoints

| Component | URL |
| --- | --- |
| API Gateway | <http://localhost:8080> |
| Eureka | <http://localhost:8761> |
| Prometheus | <http://localhost:9091> |
| Grafana | <http://localhost:3030> |
| cAdvisor | <http://localhost:8085> |

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

`up{job="cadvisor"}` should return `1`, and the PetClinic scrape targets should report `up`.

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
