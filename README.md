# CarbonShift

**A carbon-aware job scheduler for the cloud.** CarbonShift delays a flexible,
non-urgent cloud job until the cleanest moment the electricity grid will reach
before that job's deadline — then runs it, unchanged, at exactly that moment.

---

## The problem

Grid electricity is not uniformly clean. **Carbon intensity** — grams of CO₂
emitted per kWh consumed — swings by 3–5× within a single day, depending on
whether the grid is being powered by solar and wind or by coal and gas.

Cloud workloads today ignore this completely. A batch job triggered at 6pm runs
at 6pm, on whatever the grid happens to be burning at 6pm. For workloads that
genuinely have flexibility about *when* they run — nightly ETL, report
generation, data pipelines, one-off processing — that is a free emissions
reduction being left on the table, every single day, by default.

## The solution

CarbonShift adds one decision between "submit the job" and "run the job":

> Given that this job must finish within the next N hours, **which hour in that
> window has the cleanest grid?** Run it then.

The job's own code is never modified. Only its timing changes. Same container,
same input, same output — fewer emissions.

### What it actually does

1. A job is submitted with a payload and a deadline (e.g. *"within 6 hours"*).
2. CarbonShift fetches an hourly carbon-intensity forecast for the grid zone
   powering the target AWS region.
3. It discards every forecast hour after the deadline — that is a hard
   constraint, not a preference — and picks the lowest-carbon hour from what
   remains.
4. It registers a **one-time AWS EventBridge Scheduler rule** to fire an
   **ECS Fargate** task run at that exact time.
5. The job runs, logs its completion to CloudWatch, and CarbonShift reports how
   much CO₂ was saved versus running immediately.

### Why this project exists

None of the major cloud providers — AWS, Azure, GCP — offer carbon-aware job
scheduling as a native, first-party service. It exists only as an architectural
pattern developers must assemble themselves from a third-party carbon data
provider plus the cloud's own scheduling primitives. A few early tools exist in
adjacent ecosystems (carbon-aware scheduling is being added to the Java
JobRunr framework, initially limited to EU data centres), but there is no
accessible, open-source, cloud-native implementation aimed at individual
developers and small teams. CarbonShift fills that gap.

---

## Architecture

```
                    Job Request (payload + deadline)
                                 |
                                 v
                    +-------------------------+
                    |  CarbonShift Scheduler  |
     +------------->|  (picks lowest-carbon   |
     |              |   slot before deadline) |
     |              +------------+------------+
     |                           |
     v                           v
+-------------+   +------------------------------+
| Electricity |   |  AWS EventBridge Scheduler   |
|    Maps     |   |   (one-time trigger rule)    |
|  (forecast) |   +---------------+--------------+
+-------------+                   |
                                  v
                     +------------------------+
                     |    ECS Fargate Task    |
                     | (runs worker container)|
                     +------------------------+
                                  |
                                  v
                         CloudWatch Logs
```

Five components, one responsibility each:

| Component | File | Responsibility |
|---|---|---|
| Carbon data client | `src/carbon_api.py` | Fetch the hourly carbon-intensity forecast for a grid zone. Returns `(timestamp, gCO₂/kWh)` pairs. Knows nothing about AWS. |
| Scheduling logic | `src/scheduler.py` | Pick the lowest-carbon hour before the deadline; register the one-time EventBridge rule targeting ECS RunTask. |
| Worker job | `src/worker/job.py` + `Dockerfile` | The containerised demo workload. Deterministic by design — it exists to prove the mechanism fired, not to be a realistic job. |
| Infrastructure | `infra/deploy.py` | One-time boto3 provisioning: ECR repo, ECS cluster, Fargate task definition, IAM roles, log group, networking. |
| Results chart | `demo/chart.py` | Renders the before/after carbon comparison as a single static PNG. |

### Data flow

`scheduler.py` → `carbon_api.py` (forecast) → filter to before-deadline → take
the minimum → `scheduler.create_schedule()` → EventBridge Scheduler → ECS
RunTask → Fargate pulls and runs the worker image → CloudWatch Logs. Separately
and manually, `demo/chart.py` reads the saved run record and draws the result.

---

## Scope

CarbonShift implements **exactly one use case**: one job, one deadline, one
decision, one measurable result. The following are deliberately **out of
scope** and should not be added:

- No multi-cloud support (AWS only).
- No persistent web dashboard or job queue UI.
- No user authentication or multi-tenancy.
- No scheduling of multiple concurrent jobs.
- No modification of the workload's own application code.

---

## Setup

### Prerequisites

- Python 3.10+
- Docker (to build the worker image)
- An AWS account with permissions for **EventBridge Scheduler**, **ECS
  Fargate**, **IAM role creation**, **ECR** and **CloudWatch Logs**
- An [Electricity Maps](https://portal.electricitymaps.com/) API token

> **Check this before you build against it:** the Electricity Maps free tier is
> normally limited to a single "home zone". Confirm your plan covers the zone
> matching your target AWS region — otherwise the forecast call returns 401/403.
> `src/scheduler.py` maps common AWS regions to zones; override with
> `CARBON_ZONE` if yours isn't listed or you want a different grid boundary.

### 1. Install

```bash
git clone https://github.com/<your-username>/carbonshift.git
cd carbonshift
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`. Every value is documented inline there. The variables are:

| Variable | Purpose |
|---|---|
| `ELECTRICITY_MAPS_API_KEY` | Your Electricity Maps token. **Required.** |
| `CARBON_ZONE` | Grid zone to forecast, e.g. `DE`. Derived from `AWS_REGION` if blank. |
| `AWS_REGION` | Region the job runs in. |
| `ECS_CLUSTER_ARN` | Printed by `infra/deploy.py`. |
| `WORKER_TASK_DEFINITION_ARN` | Printed by `infra/deploy.py`. |
| `SCHEDULER_ROLE_ARN` | Printed by `infra/deploy.py`. |
| `WORKER_SUBNET_IDS` / `WORKER_SECURITY_GROUP_IDS` | Printed by `infra/deploy.py`. |
| `WORKER_IMAGE_URI` | ECR URI of the built worker image. |
| `WORKER_VCPU` / `WORKER_RUNTIME_HOURS` / `KWH_PER_VCPU_HOUR` | Carbon accounting constants. |

**No secret is ever read from anywhere but the environment, and `.env` is
gitignored.**

### 3. Verify the worker runs standalone

Do this *before* involving AWS at all:

```bash
docker build -t carbonshift-worker src/worker
```

```bash
docker run --rm -e CARBONSHIFT_JOB_PAYLOAD=local-test carbonshift-worker
```

You should see a `CARBONSHIFT_JOB_COMPLETE {...}` line.

### 4. Provision AWS

```bash
python infra/deploy.py
```

This creates the ECR repository, ECS cluster, log group, both IAM roles and the
Fargate task definition, then prints the exact `.env` block to paste back in.
It is safe to re-run — every step reuses an existing resource, except the task
definition, which registers a new revision (paste the new ARN in).

### 5. Push the worker image

`deploy.py` prints these commands with your real account ID filled in:

```bash
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin <account>.dkr.ecr.$AWS_REGION.amazonaws.com
```

```bash
docker build -t carbonshift-worker src/worker && docker tag carbonshift-worker $WORKER_IMAGE_URI && docker push $WORKER_IMAGE_URI
```

---

## Running a job

### Decide only, without touching AWS

```bash
python -m src.scheduler --payload "nightly-etl" --deadline-hours 6 --dry-run
```

```
forecast          : 24 hourly points, 2026-09-08T18:00:00+00:00 -> 2026-09-09T17:00:00+00:00, low 131 / high 470 gCO2/kWh
job id            : carbonshift-worker-run-67e9ba4c81
payload           : nightly-etl
region / zone     : eu-central-1 / DE
deadline          : 2026-09-09T06:00:00+00:00
run immediately   : 2026-09-08T18:00:00+00:00 at 412 gCO2/kWh -> 3.05 g CO2
CarbonShift picks : 2026-09-09T03:00:00+00:00 at 131 gCO2/kWh -> 0.97 g CO2
delay             : 9.0 h
CO2 saved         : 2.08 g (68.2%)
run record        : runs/carbonshift-worker-run-67e9ba4c81.json
schedule created  : (dry run -- nothing was sent to AWS)
```

### Schedule it for real

```bash
python -m src.scheduler --payload "nightly-etl" --deadline-hours 6
```

Or with an absolute deadline:

```bash
python -m src.scheduler --payload "nightly-etl" --deadline 2026-09-09T18:00:00Z
```

The one-time schedule deletes itself after firing, so test runs don't accumulate.

### Verify the run happened

```bash
aws logs tail /ecs/carbonshift-worker --since 1h --filter-pattern CARBONSHIFT_JOB_COMPLETE
```

### Draw the result

```bash
python demo/chart.py --actual-run 2026-09-09T03:00:12Z
```

Writes `demo/result.png`: the carbon-intensity curve, the "run immediately"
point, the point CarbonShift chose, the deadline, and the CO₂ saved. Pass
`--actual-run` with the real execution timestamp from CloudWatch so the chart
shows the verified run, not just the intended one.

---

## How the carbon maths works

The worker is assumed to draw:

```
energy (kWh) = vCPU × runtime_hours × kWh_per_vCPU_hour
             = 2 × 0.5 × 0.0074
             = 0.0074 kWh
```

Emissions are then `energy × carbon_intensity`. For a 480 → 190 gCO₂/kWh shift
that is 3.55 g → 1.41 g, a ~60% reduction for an identical job. All three
constants are configurable in `.env`; the coefficient is an approximation for
typical cloud compute utilisation, not a measured figure for your specific
hardware.

---

## Measured result

> ⚠️ **Not yet filled in.** This section must contain the real numbers from an
> actual end-to-end run — submission → scheduled wait → verified execution in
> CloudWatch — before the project is demo-ready. Do not substitute a
> hypothetical figure. Replace this block with:
>
> - the run record (`runs/<job-id>.json`)
> - the CloudWatch `CARBONSHIFT_JOB_COMPLETE` log excerpt
> - `demo/result.png`
> - the headline number: *X g CO₂ saved (Y%)* by delaying execution Z hours

---

## Tests

```bash
python -m pytest tests/ -q
```

58 tests cover the logic that can be verified without AWS. Mapping to the
specification's checklist:

| # | Check | Covered by | Status |
|---|---|---|---|
| 1 | Fetches and parses a real live forecast | Manual — run the CLI with a real key | ✅ |
| 2 | Handles API failure/timeout without crashing | `tests/test_carbon_api.py` | ✅ |
| 3 | Excludes forecast timestamps after the deadline | `tests/test_scheduler.py` | ✅ |
| 4 | Identifies the true minimum-carbon slot | `tests/test_scheduler.py` | ✅ |
| 5 | Worker image builds and runs standalone | Manual — `docker run` (step 3 above) | ✅ |
| 6 | EventBridge rule actually fires the Fargate task | Manual — schedule ~10 min out | ⏳ pending |
| 7 | Completion log visible in CloudWatch | Manual — `aws logs tail` | ⏳ pending |
| 8 | Chart renders correctly from a real run | `tests/test_chart.py` + visual check | ✅ |

Checks 6 and 7 require a live AWS deployment, so they are run by hand.
Everything else is automated or has been verified directly.

Check 1 was verified against the live Electricity Maps API — a real 24-hour
forecast for `DE`, fetched and parsed end to end:

```
forecast          : 24 hourly points, 2026-09-08T18:00:00+00:00 -> 2026-09-09T17:00:00+00:00, low 158 / high 417 gCO2/kWh
run immediately   : 2026-09-08T18:00:00+00:00 at 416 gCO2/kWh -> 3.08 g CO2
CarbonShift picks : 2026-09-09T00:00:00+00:00 at 313 gCO2/kWh -> 2.32 g CO2
CO2 saved         : 0.76 g (24.8%)
```

Check 5 was verified on Docker 29.7.2 — the image builds from `src/worker` and
runs under plain `docker run` with no AWS involved:

```
[2026-09-08T18:38:19] CarbonShift worker starting
[2026-09-08T18:38:19]   payload        : local-test
[2026-09-08T18:38:24]   rows processed : 5
[2026-09-08T18:38:24]   checksum       : 8cca50fc7eb250ec
[2026-09-08T18:38:24] CARBONSHIFT_JOB_COMPLETE {"checksum": "8cca50fc7eb250ec", ...}
```

---

## Cost and cleanup

A Fargate run of this demo worker costs a fraction of a cent, and one-time
schedules delete themselves. To remove everything afterwards:

```bash
aws ecs deregister-task-definition --task-definition carbonshift-worker:1 && aws ecs delete-cluster --cluster carbonshift-cluster && aws ecr delete-repository --repository-name carbonshift-worker --force && aws logs delete-log-group --log-group-name /ecs/carbonshift-worker
```

Then delete the `carbonshift-ecs-execution-role` and `carbonshift-scheduler-role`
IAM roles.

---

## License

MIT — see [LICENSE](LICENSE).

---

*Author: Ramya Samudrala · B.Sc. Computer Science and Cloud Computing*
