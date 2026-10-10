# Two-minute ChaosLab demo

The committed [dashboard](../reports/dashboard.html) and [recorded run](../demo/visits-failure.json) work without Docker. They describe a local experiment recorded on **2026-10-10**, before canary visits and explicit data-loss classification were added. The displayed dependency map was generated later, so its snapshot ID differs from the one captured with the run; the dashboard explicitly labels this.

## Walkthrough

1. **Show the finding (30 seconds).** Open the dashboard. Point to the `GET /api/gateway/owners/{ownerId}` path: Gateway uses Customers and Visits. The recorded fault probe returned HTTP `200` with six baseline visits missing. Explain why status-only monitoring would miss that result.
2. **Show safety and impact (45 seconds).** Scroll to the experiment. The Visits crash lasted 9.3 seconds before the watchdog aborted it following low throughput. The timeline separates baseline, fault, and recovery. `add visit` was predicted and observed unavailable; `owner details` was predicted and observed degraded.
3. **Show honest recovery (25 seconds).** After the fault was reverted, the final correctness probe still missed two baseline visits. The dashboard says recovery was not confirmed, despite normalized HTTP errors and latency.
4. **Show the engineering boundary (20 seconds).** Point to timestamped evidence and guardrails. The dashboard is read-only; fault execution stays in the CLI. The AI planner and analysis agent are planned work.

## Recreate the export

From the repository root:

```bash
python3.11 -m chaoslab dashboard --demo
```

Open `reports/dashboard.html`. To use the latest finished local run instead, execute `python3.11 -m chaoslab dashboard`. A fresh clone falls back to the committed recording. The separate [operations guide](operations.md) covers starting Docker, generating a fresh map, and running a guarded experiment.

## Screenshot provenance

The README screenshots were captured from the committed demo export at a desktop viewport. They show **historical run data**, not live services. `dashboard-overview.png` highlights the status-versus-correctness finding; `dashboard-experiment.png` shows the timeline and prediction comparison. The demo JSON is the evidence source for every number in those images.
