"""Fixed settings for the chaos controller. Everything points at the local sandbox."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
STATE_FILE = RUNS_DIR / "active-faults.json"
REPORTS_DIR = ROOT / "reports"
MAP_FILE = REPORTS_DIR / "dependency-map.json"
MAP_OVERRIDES_FILE = ROOT / "map-overrides.json"
SEMANTICS_FILE = ROOT / "dependency-semantics.json"
APP_DIR = ROOT / "spring-petclinic-microservices"
COMPOSE_FILE = APP_DIR / "docker-compose.yml"

# Compose names the project after the folder holding docker-compose.yml.
COMPOSE_PROJECT = "spring-petclinic-microservices"
# Only containers carrying this label may ever be faulted. Monitoring and the
# load generator deliberately do not have it: we never break our own eyes.
TARGET_LABEL = "chaoslab.target"

LOCUST_URL = "http://localhost:8089"
LOAD_TARGET_URL = "http://api-gateway:8080"   # where the simulated users send traffic
ZIPKIN_URL = "http://localhost:9411"
EUREKA_URL = "http://localhost:8761"
PROMETHEUS_URL = "http://localhost:9091"
NETTOOLS_IMAGE = "chaoslab/nettools"

# The user actions defined in load/locustfile.py and the URLs they call. The
# dependency map uses these to find which services each action passes through.
ACTION_ROUTES = {
    "list owners": r"/api/customer/owners",
    "owner details": r"/api/gateway/owners/\d+",
    "list vets": r"/api/vet/vets",
    "pet types": r"/api/customer/petTypes",
    "add visit": r"/api/visit/owners/\d+/pets/\d+/visits",
}
