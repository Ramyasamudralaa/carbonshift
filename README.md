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

## Who this is for

You'll get value from CarbonShift if you run **batch work that isn't
latency-sensitive** — nightly ETL, report generation, data pipelines, model
training, backups, one-off processing — on AWS. Anything where "it must be done
by 7am" is true but "it must start this second" is not.

It is **not** for user-facing or latency-sensitive workloads. Delaying those is
the wrong trade, and CarbonShift makes no attempt to be useful there.

How much you save depends entirely on how variable your grid is. Measured
across five zones on real forecast data:

| Grid | Daily swing | Realistic saving |
|---|---|---|
| Germany (`DE`) | 2.6× | ~50% |
| Great Britain (`GB`) | 2.5× | ~45% |
| South India (`IN-SO`) | 1.5× | ~30% |
| West India (`IN-WE`) | 1.4× | ~25% |
| US Mid-Atlantic (`US-MIDA-PJM`) | 1.2× | ~12% |

A flat grid means a small saving. That is the honest answer, and CarbonShift
will tell you so rather than pretend otherwise — if no hour before your
deadline beats running immediately, it refuses to shift the job at all.

## Try it in 60 seconds — no AWS account needed

You do **not** need AWS to see what CarbonShift decides. `--dry-run` fetches a
real forecast, makes the real decision, and reports the real saving, without
provisioning or scheduling anything.

All you need is Python 3.10+ and a free
[Electricity Maps](https://portal.electricitymaps.com/) API token.

```bash
git clone https://github.com/Ramyasamudralaa/carbonshift.git && cd carbonshift
```

```bash
pip install -r requirements.txt
```

```bash
python setup.py
```

`setup.py` is a guided wizard. It asks which region you want, asks for your API
token, **tests the token against the live API before accepting it**, and writes
your `.env` for you. You don't have to know what any of the variables mean.

### Don't want to remember commands?

```bash
python carbonshift.py
```

```
  CarbonShift
  Run cloud jobs when the electricity grid is cleanest.

  Fully set up. Jobs can be scheduled and will really run.

  ----------------------------------------------------------------
  1. Set up CarbonShift            api key, region - writes your .env
  2. Schedule a job                pick a job and a deadline
  3. See my history and savings    every run, and the running total
  4. Check my setup is healthy     finds problems and how to fix them
  5. Deploy to AWS                 one-time, creates the cloud resources
  6. Quit
  ----------------------------------------------------------------

  Choose [2]:
```

Pick `2` and it asks *"What is the job called?"* and *"How many hours until it
must have started?"* — no flags to remember. The status line at the top tells
you how far setup has got, and the menu refuses to schedule before you're ready
rather than failing later.

Every individual command below still works exactly as it did; the menu only
drives them.

```bash
python -m src.scheduler --payload "my-nightly-job" --deadline-hours 12 --dry-run
```

```
forecast          : 24 hourly points, low 251 / high 512 gCO2/kWh
run immediately   : 2026-09-10T17:00:00+00:00 at 497 gCO2/kWh -> 3.68 g CO2
CarbonShift picks : 2026-09-11T09:00:00+00:00 at 251 gCO2/kWh -> 1.86 g CO2
delay             : 16.0 h
CO2 saved         : 1.82 g (49.5%)
```

Then render the chart:

```bash
python demo/chart.py
```

That is the whole idea, evaluated before you commit to any cloud setup. When
you want it to *actually run* your job, continue to [Setup](#setup) below.

### What you need, by how far you want to go

| To… | You need |
|---|---|
| See the decision and the chart | Python 3.10+, an Electricity Maps token |
| Run the worker locally | + Docker |
| Actually schedule and execute jobs | + an AWS account (EventBridge Scheduler, ECS Fargate, ECR, IAM, CloudWatch Logs) |

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

> **On Windows, run that in PowerShell, not Git Bash.** Git Bash rewrites the
> leading `/` of `/ecs/carbonshift-worker` into a Windows path before the AWS
> CLI ever sees it, so the query silently returns nothing and it looks like the
> job never ran. If you must use Git Bash, prefix the command with
> `MSYS_NO_PATHCONV=1`.

### Draw the result

```bash
python demo/chart.py --actual-run 2026-09-09T03:00:12Z
```

Writes `demo/result.png`: the carbon-intensity curve, the "run immediately"
point, the point CarbonShift chose, the deadline, and the CO₂ saved. Pass
`--actual-run` with the real execution timestamp from CloudWatch so the chart
shows the verified run, not just the intended one.

### See everything you've saved so far

A one-time AWS schedule **deletes itself the moment it fires**, so afterwards
there is nothing left in the console to look at. Every decision is recorded to
`runs/` when it is made, and this reads them back:

```bash
python -m src.history
```

```
  WHEN IT RAN       JOB                    TYPE        BEFORE   AFTER     SAVED  STATUS
  2026-09-10 17:00  carbonshift-e2e-test   scheduled      462     497    -0.26 g  confirmed ran
  2026-09-11 09:00  carbonshift-demo-run   scheduled      497     251    +1.82 g  confirmed ran
  2026-09-16 11:00  ramya-proof-test       scheduled      252     236    +0.12 g  confirmed ran

  Totals across 4 real scheduled runs
    CO2 that would have been emitted :   12.64 g
    CO2 actually emitted             :   10.19 g
    CO2 saved                        :    2.45 g (19.4%)
```

| Flag | What it does |
|---|---|
| `--verify` | Cross-checks each run against CloudWatch Logs and marks it *confirmed ran* or *no log found* |
| `--real-only` | Hides dry runs |
| `--chart` | Writes `demo/history.png`, cumulative savings over time |

Dry runs are always excluded from the totals — they never executed, so counting
them would inflate the number.

### Carbon reports — the evidence, not just the number

```bash
python -m src.report --month 2026-09 --verify --csv
```

A running total on a terminal is not evidence. An organisation reporting under
a carbon disclosure regime needs a **document**: the period, the jobs, the
figures, the method that produced them, and an honest statement of what the
figures are and are not.

```
  Carbon Reduction Report  All recorded activity
  ----------------------------------------------------------
    Workloads shifted                   3
    Electricity consumed           0.0222 kWh
    Baseline emissions              8.961 g
    Actual emissions                7.282 g
    Emissions avoided               1.680 g (18.7%)
    Verified against logs               3 of 4
    Excluded, unverifiable              1 (+0.770 g not claimed)
```

It writes a printable HTML report — **Ctrl+P to save as PDF** — containing:

| Section | Contents |
|---|---|
| Headline | kg CO₂e avoided, jobs shifted, mean deferral |
| Summary | Energy consumed, baseline vs actual emissions, zones and regions |
| Itemised activity | Every job, with its verification status |
| Methodology | Scope, data source, baseline definition, energy model, formulae |
| Limitations | What the figures are not, stated plainly |

`--csv` also writes a row-per-job CSV for an auditor or a spreadsheet.

**Three rules it will not break:**

- **Previews are never counted.** They were never scheduled, so counting them
  would overstate the saving.
- **With `--verify`, jobs with no CloudWatch log are excluded from every
  figure** and the report says how much was therefore not claimed.
- **Without `--verify`, the report says so on its face** and tells you to
  re-run before disclosing anything.

Set `CARBONSHIFT_ORGANISATION` in `.env` to put your organisation's name on it.

> The report states that figures are modelled estimates from forecast carbon
> intensity, not metered emissions, and that it is not an assurance statement.
> A compliance document that overstates its own certainty is worse than none.

### The live dashboard

```bash
python demo/dashboard.py
```

Builds one **self-contained HTML file** and opens it. No server, no internet, no
libraries — so it keeps working on conference wifi, which is to say on no wifi
at all.

| Panel | Shows |
|---|---|
| **Latest decision** | The percentage saved, the grid zone, the delay |
| **Countdown** | Live ticking countdown to a job that hasn't fired yet, or `DONE` |
| **Total saved** | Running total across every real run |
| **Carbon curve** | The forecast window, with the deadline, the "run now" point and the hour CarbonShift chose both marked |
| **Recent runs** | The last nine, previews greyed out |

```bash
python demo/dashboard.py --watch
```

Rebuilds every 30 seconds, for leaving on a screen. `--no-open` builds without
launching a browser.

The page is generated from the run records, so it can never disagree with what
the system actually did.

### When something isn't working

```bash
python -m src.doctor
```

Scheduling is asynchronous — the job fires hours later on a machine you aren't
watching. So the failures that matter most are the silent ones. If the worker
image is missing from ECR, `scheduler.py` still creates the schedule perfectly;
ECS then fails to pull it at 03:00, the container never starts, so there is no
log, and the schedule has already deleted itself. **You would wake up to no
evidence at all.**

The doctor checks all of that up front, in one pass, and prints the command
that fixes each problem:

```
  Your machine
  OK    Python version      3.11.4
  OK    Core dependencies   requests, python-dotenv, matplotlib
  OK    .env file           .env

  Carbon data
  OK    API key             set (35 characters)
  OK    Live forecast       24 points, 128-416 gCO2/kWh (3.2x swing)

  AWS
  OK    ECS cluster         carbonshift-cluster (ACTIVE)
  OK    Task definition     carbonshift-worker:1 (ACTIVE)
  FAIL  Worker image        RepositoryNotFoundException
           -> The image is missing, so a scheduled job would fail silently.
           -> docker build -t carbonshift-worker src/worker
           -> docker push <your-ecr-uri>:latest

  Run history
  WARN  Execution confirmed 3 of 4 confirmed, 1 with no log
           -> python -m src.history --verify   to see which
```

It checks the machine, your API key against the live API, every AWS resource
your `.env` points at, whether the worker image is really in ECR, the Docker
daemon, and whether past scheduled runs actually produced a CloudWatch log.

`--quick` skips the network calls. Exit code is `0` if nothing failed and `1`
if something did, so it works in CI. Missing AWS credentials is a **warning**,
not a failure — the `--dry-run` path doesn't need them.

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

### The mechanism, verified end to end

On 2026-09-10, a job was submitted to CarbonShift, scheduled by EventBridge,
and executed by ECS Fargate with no human involvement in between.

| | |
|---|---|
| Submitted | 16:33 UTC, deadline 17:33 UTC |
| Chosen slot | **17:00:00 UTC** (the lowest-carbon hour inside the deadline) |
| Schedule created | `arn:aws:scheduler:eu-central-1:<account-id>:schedule/default/carbonshift-worker-run-4e266db7ad` |
| Task actually started | **17:00:21 UTC** — 21 s after the target, Fargate cold start |
| Exit code | `0` |
| Checksum | `8cca50fc7eb250ec` — identical to the local `docker run`, proving determinism |

The CloudWatch log for that run:

```
[2026-09-10T17:00:21.620544+00:00] CarbonShift worker starting
[2026-09-10T17:00:21.620573+00:00]   payload        : carbonshift-e2e-test
[2026-09-10T17:00:21.620580+00:00]   scheduled for  : 2026-09-10T17:00:00+00:00
[2026-09-10T17:00:21.620587+00:00]   actual start   : 2026-09-10T17:00:21.620536+00:00
[2026-09-10T17:00:26.621309+00:00]   rows processed : 5
[2026-09-10T17:00:26.621355+00:00]   checksum       : 8cca50fc7eb250ec
[2026-09-10T17:00:26.621395+00:00] CARBONSHIFT_JOB_COMPLETE {"checksum": "8cca50fc7eb250ec", "duration_seconds": 5.0, "payload": "carbonshift-e2e-test", "rows_processed": 5, "scheduled_for": "2026-09-10T17:00:00+00:00", ...}
```

The one-time schedule deleted itself after firing, as designed.

That run was a mechanism test on a one-hour deadline, during an evening grid
ramp where no cleaner hour was reachable — so its carbon delta was negative.
It is reported here because it proves the plumbing, not the saving. (It is also
what prompted the guard described below.)

### The carbon saving

A job was submitted on 10 September with a 16-hour deadline. CarbonShift chose
**09:00 UTC on 11 September** — the German solar peak — and AWS executed it
there, unattended, while the submitting machine was switched off.

| | |
|---|---|
| Baseline (run immediately) | 17:00 UTC at **497 gCO₂/kWh** → 3.68 g CO₂ |
| CarbonShift chose | 09:00 UTC at **251 gCO₂/kWh** → 1.86 g CO₂ |
| Delay | 16 hours |
| **CO₂ saved** | **1.82 g — 49.5%** |

Verified in CloudWatch Logs:

```
[2026-09-11T09:01:07] CarbonShift worker starting
[2026-09-11T09:01:07]   payload        : carbonshift-demo-run
[2026-09-11T09:01:07]   scheduled for  : 2026-09-11T09:00:00+00:00
[2026-09-11T09:01:07]   actual start   : 2026-09-11T09:01:07+00:00
[2026-09-11T09:01:12]   checksum       : 8cca50fc7eb250ec
[2026-09-11T09:01:12] CARBONSHIFT_JOB_COMPLETE {"payload": "carbonshift-demo-run", ...}
```

Scheduled for 09:00:00, started 09:01:07 — 67 seconds of Fargate cold start.
The checksum is identical to the local `docker run`, so the same deterministic
workload provably executed in both places.

The full forecast curve behind that decision:

```
17:00  497  +0.0%   (run now — the baseline)
20:00  512  -3.0%   evening peak, the worst hour
05:00  393 +20.9%
09:00  251 +49.5%   <- chosen: solar peak, the cleanest hour
16:00  435 +12.5%
```

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
| 6 | EventBridge rule actually fires the Fargate task | Manual — schedule ~10 min out | ✅ |
| 7 | Completion log visible in CloudWatch | Manual — `aws logs tail` | ✅ |
| 8 | Chart renders correctly from a real run | `tests/test_chart.py` + visual check | ✅ |

**All eight checks pass.** Checks 1, 5, 6 and 7 need a live API key or a live
AWS deployment, so they were run by hand and their evidence is in
[Measured result](#measured-result); the rest are automated.

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
