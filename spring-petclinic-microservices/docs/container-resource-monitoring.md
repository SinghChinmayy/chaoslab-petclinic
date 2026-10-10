# Container resource monitoring

cAdvisor runs on the same Compose network as Prometheus and exports CPU, memory, and network metrics for the PetClinic containers. Prometheus reaches it at `cadvisor:8080`; its optional UI is exposed only on the local machine at <http://localhost:8085> so it does not conflict with the API Gateway on port 8080.

The checked-in configuration is verified with Docker Desktop 29.5.2 on Apple Silicon using its classic `overlay2` image store. In Docker Desktop **Settings > General**, turn off **Use containerd for pulling and storing images**, then restart Docker Desktop. Switching image stores can temporarily hide the images and containers in the other store, so the stack may need to be pulled and recreated. Do not remove volumes.

The configuration pins the official multi-architecture cAdvisor `v0.49.1` image. Newer tested releases require Docker Desktop's containerd socket, which is not exposed to containers, while `v0.49.1` can discover containers through the mounted Docker socket and classic image layout. The service has privileged, read-only access to the Docker Desktop Linux VM system paths. Filesystem metrics are disabled because they are not needed for this integration.

Confirm the active environment before starting:

```bash
docker context show
docker info --format 'OS={{.OperatingSystem}} driver={{.Driver}} root={{.DockerRootDir}}'
```

For the verified Docker Desktop setup, the driver must report `overlay2`. Native Linux and Colima also expose a Linux Docker VM to containers, so the same standard cAdvisor mounts can be used when their active Docker context reports an accessible `/var/lib/docker` root.

## Start or update monitoring

From `spring-petclinic-microservices`:

```bash
docker compose up -d --build --no-deps cadvisor prometheus-server
```

This recreates only cAdvisor and Prometheus. Prometheus must be rebuilt because `prometheus.yml` is added to its image by `docker/prometheus/Dockerfile` rather than mounted at runtime.

If Docker Desktop has just switched image stores and the application containers are not visible, recreate the whole stack (the optional GenAI profile remains disabled):

```bash
docker compose up -d --build
```

## Verify

```bash
docker compose config --quiet
docker exec prometheus-server promtool check config /etc/prometheus/prometheus.yml
curl -fsS http://localhost:8085/metrics | head
curl -fsS 'http://localhost:9091/api/v1/query?query=up%7Bjob%3D%22cadvisor%22%7D' | jq
curl -fsS 'http://localhost:9091/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv'
curl -fsSG 'http://localhost:9091/api/v1/query' \
  --data-urlencode 'query=count by (container_label_com_docker_compose_service) (container_memory_working_set_bytes{container_label_com_docker_compose_project="spring-petclinic-microservices",container_label_com_docker_compose_service!=""})' \
  | jq -r '.data.result[].metric.container_label_com_docker_compose_service'
```

`up{job="cadvisor"}` should return `1`. The target list should continue to show the existing PetClinic jobs as `up`, and the final command should list individual Compose services rather than only the host or cAdvisor.

## PromQL examples

The Compose service is available in the `container_label_com_docker_compose_service` label. These queries exclude cAdvisor itself and group results by the individual PetClinic service.

CPU usage in cores, averaged over one minute:

```promql
sum by (container_label_com_docker_compose_service) (
  rate(container_cpu_usage_seconds_total{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor"
  }[1m])
)
```

CPU is measured in cores: `1` means one full CPU core, `0.25` means one quarter of a core, and `2` means two cores. Multiply by 100 to express usage as a percentage of one core.

Memory working set in bytes:

```promql
sum by (container_label_com_docker_compose_service) (
  container_memory_working_set_bytes{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor"
  }
)
```

Network receive rate in bytes per second:

```promql
sum by (container_label_com_docker_compose_service) (
  rate(container_network_receive_bytes_total{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor",
    interface="eth0"
  }[1m])
)
```

Network transmit rate in bytes per second:

```promql
sum by (container_label_com_docker_compose_service) (
  rate(container_network_transmit_bytes_total{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor",
    interface="eth0"
  }[1m])
)
```
