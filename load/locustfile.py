"""Steady background traffic for the PetClinic sandbox.

Each simulated user behaves like clinic staff: browsing owners and vets, opening
owner details, and occasionally recording a visit. This file only defines what
one user does; the AI agent decides how many users run (normal load vs spike)
through Locust's web API.
"""

import random
from datetime import date

from locust import HttpUser, between, task

# Seed data shipped with the app: owner id -> that owner's pet ids.
OWNER_PETS = {
    1: [1], 2: [2], 3: [3, 4], 4: [5], 5: [6],
    6: [7, 8], 7: [9], 8: [10], 9: [11], 10: [12, 13],
}
# Owner 6 is the fixture the correctness probe checks (chaoslab/semantic_probe.py).
# Never write to it, so its visits change only when an experiment writes a canary
# or data is really lost.
PROBE_OWNER = 6
WRITABLE_OWNERS = [owner for owner in OWNER_PETS if owner != PROBE_OWNER]


class ClinicStaff(HttpUser):
    wait_time = between(1, 3)

    @task(4)
    def list_owners(self):
        self.client.get("/api/customer/owners", name="list owners")

    @task(4)
    def owner_details(self):
        # Touches customers-service and visits-service through the gateway.
        owner_id = random.choice(list(OWNER_PETS))
        self.client.get(f"/api/gateway/owners/{owner_id}", name="owner details")

    @task(3)
    def list_vets(self):
        self.client.get("/api/vet/vets", name="list vets")

    @task(1)
    def pet_types(self):
        self.client.get("/api/customer/petTypes", name="pet types")

    @task(1)
    def add_visit(self):
        owner_id = random.choice(WRITABLE_OWNERS)
        pet_id = random.choice(OWNER_PETS[owner_id])
        self.client.post(
            f"/api/visit/owners/{owner_id}/pets/{pet_id}/visits",
            json={"date": date.today().isoformat(), "description": "Routine check-up"},
            name="add visit",
        )
