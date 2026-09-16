# CarbonShift

**Run your cloud jobs when the electricity is clean.**

Some hours of the day have much cleaner electricity than others. CarbonShift
waits for a clean hour, then runs your job. Your code does not change. Only the
timing changes.

In a real test on the German grid, the same job produced **49.5% less CO2**
simply by running 16 hours later.

---

## Contents

1. [The problem](#the-problem)
2. [How CarbonShift solves it](#how-carbonshift-solves-it)
3. [Who this is for](#who-this-is-for)
4. [Try it in 60 seconds](#try-it-in-60-seconds)
5. [The menu](#the-menu)
6. [What it can do](#what-it-can-do)
7. [Architecture](#architecture)
8. [Full setup with AWS](#full-setup-with-aws)
9. [Proof that it works](#proof-that-it-works)
10. [Tests](#tests)
11. [What it does not do](#what-it-does-not-do)

---

## The problem

Electricity is not equally clean all day.

When the wind blows and the sun shines, a lot of power comes from renewables.
Carbon intensity goes down. When they stop, the gap is filled by gas and coal.
Carbon intensity goes up.

On the German grid we measured a range of **251 to 512 gCO2 per kWh inside one
day**. The dirtiest hour was more than twice as bad as the cleanest hour.

Cloud jobs ignore this completely. A backup set for 8pm runs at 8pm, no matter
what the grid is burning at 8pm.

For jobs that could have run at any time, that is a wasted opportunity.

## How CarbonShift solves it

You tell CarbonShift two things:

1. What the job is
2. When it must be finished by

CarbonShift then does this:

1. Looks up the carbon forecast for the next 24 hours
2. Throws away every hour after your deadline
3. Picks the cleanest hour that is left
4. Books the job with AWS to run at that exact time
5. AWS runs it, and the result is logged

Same job. Same output. Less CO2.

If no hour before your deadline is cleaner than right now, CarbonShift refuses
to move the job. Delaying it would make things worse, so it does not delay it.

## Who this is for

CarbonShift is useful if you run **batch work that nobody is waiting for**:

* Nightly backups
* Data pipelines and ETL
* Report generation
* Model training
* Cleanup and archiving

It is **not** for anything a user is waiting on. Login, payments, alerts,
health checks, anything serving live traffic. Delaying those is the wrong
trade, and CarbonShift does not try to be useful there.

### How much you save depends on your grid

We measured five real grid zones:

| Grid | Daily swing | Realistic saving |
|:--|:--|:--|
| Germany (DE) | 2.6x | about 50% |
| Britain (GB) | 2.5x | about 45% |
| South India (IN SO) | 1.5x | about 30% |
| West India (IN WE) | 1.4x | about 25% |
| US Mid Atlantic | 1.2x | about 12% |

A flat grid means a small saving. That is the honest answer. CarbonShift will
tell you when there is nothing worth saving instead of pretending otherwise.

---

## Try it in 60 seconds

**You do not need an AWS account for this.** You only need Python 3.10 or newer
and a free API token.

### 1. Get the code

```bash
git clone https://github.com/Ramyasamudralaa/carbonshift.git
```

```bash
cd carbonshift
```

### 2. Install

```bash
pip install -r requirements.txt
```

### 3. Run the setup helper

```bash
python setup.py
```

It asks you a few questions and writes your settings file for you. You never
have to edit any config by hand.

It walks through six steps: your machine, your region, your carbon API token,
AWS (optional), the AI scanner (optional), and then it writes everything. Both
optional steps can be skipped and added later by running it again.

It will ask for an API token. Get a free one at
[portal.electricitymaps.com](https://portal.electricitymaps.com/). The helper
tests your token against the live service before it accepts it, so a wrong
token is caught immediately.

### 4. See it work

```bash
python -m src.scheduler --payload "my-first-job" --deadline-hours 12 --dry-run
```

```
forecast          : 24 hourly points, low 251 / high 512 gCO2/kWh
run immediately   : 2026-09-10T17:00:00+00:00 at 497 gCO2/kWh -> 3.68 g CO2
CarbonShift picks : 2026-09-11T09:00:00+00:00 at 251 gCO2/kWh -> 1.86 g CO2
delay             : 16.0 h
CO2 saved         : 1.82 g (49.5%)
```

That is real data from the real grid. `--dry-run` means it decides and shows
you, but changes nothing.

### 5. See the picture

```bash
python demo/dashboard.py
```

---

## The menu

If you would rather not remember any commands:

```bash
python carbonshift.py
```

```
  CarbonShift
  Run cloud jobs when the electricity grid is cleanest.

  Fully set up. Jobs can be scheduled and will really run.

  1. Set up CarbonShift            api key, region, writes your settings
  2. Schedule a job                pick a job and a deadline
  3. Open the live dashboard       charts and a countdown, in your browser
  4. See my history and savings    every run, and the running total
  5. Generate a carbon report      monthly report you can file or print
  6. Scan for shiftable jobs       which of your jobs could be shifted
  7. Check my setup is healthy     finds problems and how to fix them
  8. Deploy to AWS                 one time, creates the cloud resources
  9. Quit
```

Pick option 2 and it asks you *"What is the job called?"* and *"How many hours
until it must have started?"*. No flags to remember.

The line at the top tells you how far your setup has got. The menu will not let
you schedule a job before you are ready.

---

## What it can do

### Find which jobs are worth shifting

```bash
python -m src.scanner --cron demo/sample-crontab
```

CarbonShift can move a job you already know is flexible. The harder question is
which of your forty scheduled jobs are flexible at all.

The scanner reads them and decides.

```
  SHIFTABLE
    backup_database.sh          0.421 kg/yr  high
      looks like a backup, running 365x a year
    etl_warehouse_load.py       0.421 kg/yr  high
      looks like an ETL job, running 365x a year
    retrain_model.py            0.421 kg/yr  high
      looks like model retraining, running 365x a year

  NOT SHIFTABLE
    healthcheck.sh             looks like health checking, something is waiting on it
    heartbeat.py               looks like heartbeats, something is waiting on it
    send_alert_queue.py        looks like alerting, something is waiting on it

  8 of 12 jobs look shiftable.
  Potential saving: 2.239 kg CO2 per year
```

The yearly figure uses a live forecast for **your own grid zone**, not a
generic number.

| Where your jobs are | Command |
|:--|:--|
| A crontab file | `--cron /etc/crontab` |
| Typed or piped in | `--cron -` |
| Your AWS schedules | `--aws` |
| As JSON | `--json` |

**Two ways to classify:**

`--engine heuristic` is the default. It uses rules about the command name and
how often the job runs. It needs no API key, no internet, and costs no energy.
How often a job runs is the strongest clue. Anything running more than once an
hour is keeping something alive, not doing batch work.

`--engine claude` has an AI read each job instead. It is better with unclear
names. It needs an `ANTHROPIC_API_KEY`. If the key is missing or the service
does not answer properly, it quietly falls back to the rules.

**Why the AI does not pick the hour.** Picking the cleanest hour is just
finding the smallest of 24 numbers. An AI would be slower, cost money, and give
different answers each time. Deciding whether a job can wait six hours is a
judgement call, and that is the part worth an AI.

**Does the AI cost carbon?** Yes, which is why it runs **once per job, not once
per run**. A nightly job is classified one time and that answer is reused for
all 365 runs that year. It also uses the smallest capable model. An AI call
that cost more carbon than the shift saved would defeat the whole point.

### Jobs that cannot start too early

Some work has two constraints, not one. Data does not land until noon, so the
job cannot start before then, but it still has to be finished by evening.

```bash
python -m src.scheduler --payload "afternoon-etl" --not-before 2026-09-17T12:00:00Z --deadline 2026-09-17T18:00:00Z
```

CarbonShift then picks the cleanest hour inside that window instead of the
cleanest hour overall.

### Schedule a job for real

```bash
python -m src.scheduler --payload "nightly-backup" --deadline-hours 12
```

This books the job with AWS. You can then see it sitting in the AWS console
under EventBridge, Schedules. At the chosen time AWS runs it by itself. Your
computer can be switched off.

### Watch it happen

```bash
python demo/dashboard.py
```

One web page that works without internet once it is open. It shows:

* The percentage saved on the latest decision
* A live countdown to the next job
* Your running total
* The carbon curve, with the deadline and both hours marked
* Your recent runs

Add `--watch` to keep it refreshing. Useful for leaving on a screen.

### See everything you have saved

```bash
python -m src.history --verify
```

An AWS schedule deletes itself as soon as it fires. So afterwards there is
nothing left in the console to look at. Every decision is saved on your machine
when it is made, and this reads them back.

```
  WHEN IT RAN       JOB                    TYPE        BEFORE   AFTER     SAVED  STATUS
  2026-09-10 17:00  carbonshift-e2e-test   scheduled      462     497    -0.26 g  confirmed ran
  2026-09-11 09:00  carbonshift-demo-run   scheduled      497     251    +1.82 g  confirmed ran
  2026-09-16 11:00  ramya-proof-test       scheduled      252     236    +0.12 g  confirmed ran

  Totals across 4 real scheduled runs
    CO2 saved                        :    2.45 g (19.4%)
```

`--verify` checks each run against the AWS logs and tells you which ones really
happened. `--chart` draws it.

### Produce a report you can file

```bash
python -m src.report --month 2026-09 --verify --csv
```

A number on a screen is not evidence. If your organisation has to report its
emissions, it needs a document.

This makes one. It is a web page you can print to PDF, plus a spreadsheet file
if you want one.

The document contains:

| Section | What is in it |
|:--|:--|
| Headline | kg CO2 avoided, jobs shifted, average delay |
| Summary | Energy used, emissions before and after, grid zones |
| Every job | One row each, with whether it was confirmed |
| Method | Where the data came from and how the sums were done |
| Limits | What the numbers are, and what they are not |

**Three rules it will not break:**

1. **Previews never count.** They were never scheduled, so counting them would
   overstate the saving.
2. **With `--verify`, any job with no AWS log is left out of every figure.** The
   report says how much was therefore not claimed.
3. **Without `--verify`, the report says so on its face** and tells you to run
   it again before showing anyone.

The report states plainly that the figures are estimates based on forecasts,
not measured emissions, and that it is not an audit. A document that claims
more certainty than it has is worse than no document.

Put your organisation name in `CARBONSHIFT_ORGANISATION` to have it appear on
the report.

### When something is not working

```bash
python -m src.doctor
```

This is the one to run when you are stuck.

Booking a job is not instant. It runs hours later, on a machine you are not
watching. So the failures that matter most are the quiet ones.

Here is the important example. If your worker image is missing from AWS, the
scheduler still books the job perfectly, because booking does not check. Then
at 3am AWS tries to start the job, cannot find the image, and gives up. There
is no log, because nothing ever ran. The schedule has already deleted itself.
**You wake up to no evidence at all.**

The doctor checks for that, and everything else, in one go:

```
  Your machine
  OK    Python version      3.11.4
  OK    Core dependencies   requests, python-dotenv, matplotlib

  Carbon data
  OK    API key             set (35 characters)
  OK    Live forecast       24 points, 128 to 416 gCO2/kWh (3.2x swing)

  AWS
  OK    ECS cluster         carbonshift-cluster (ACTIVE)
  FAIL  Worker image        not found in ECR
           -> The image is missing, so a scheduled job would fail silently.
           -> docker build -t carbonshift-worker src/worker
           -> docker push <your-ecr-uri>:latest
```

Every problem comes with the command that fixes it. Missing AWS credentials is
only a warning, not a failure, because you can still use `--dry-run` without
them.

---

## Architecture

```
                Your job, plus a deadline
                           |
                           v
              +--------------------------+
              |   CarbonShift scheduler  |
      +------>|  picks the cleanest hour |
      |       |     before the deadline  |
      |       +------------+-------------+
      |                    |
      v                    v
+-------------+   +---------------------------+
| Electricity |   | AWS EventBridge Scheduler |
|    Maps     |   |    holds a one time rule  |
|  forecast   |   +-------------+-------------+
+-------------+                 |
                                v
                    +------------------------+
                    |    ECS Fargate task    |
                    |   runs your container  |
                    +------------------------+
                                |
                                v
                        CloudWatch Logs
```

Five parts, each with one job to do.

| Part | File | What it does |
|:--|:--|:--|
| Carbon data | `src/carbon_api.py` | Gets the hourly forecast. Knows nothing about AWS. |
| Decision | `src/scheduler.py` | Picks the hour and books it with AWS. |
| Worker | `src/worker/` | The container that actually runs. |
| Setup | `infra/deploy.py` | Creates the AWS resources, one time. |
| Chart | `demo/chart.py` | Draws the before and after picture. |

Plus the newer tools: `src/scanner.py` finds shiftable jobs, `src/report.py`
produces the filable document, `src/history.py` reads back past runs,
`src/doctor.py` diagnoses problems, `demo/dashboard.py` is the live page, and
`carbonshift.py` is the menu.

**The decision logic contains no AWS code at all.** The four functions that do
the actual choosing have zero references to any cloud library. That is checked,
not assumed. It also means moving this to Azure or Google Cloud would mean
rewriting two files, not redesigning the project.

---

## Full setup with AWS

You only need this if you want jobs to actually run. Everything above works
without it.

### What you need first

* An AWS account
* AWS CLI installed, and `aws configure` already done
* Docker Desktop installed and running

### Steps

**1. Run the setup helper again.**

```bash
python setup.py
```

Once it sees your AWS credentials it offers to do the whole thing for you. It
creates the resources, writes every setting into your `.env` by itself, then
builds and uploads the worker image.

**There is nothing to copy and paste.** Say yes and wait about a minute.

It is safe to run more than once. It reuses anything that already exists.

**2. Check it all worked.**

```bash
python -m src.doctor
```

**3. Book a real job.**

```bash
python -m src.scheduler --payload "nightly-backup" --deadline-hours 12
```

Open the AWS console, go to EventBridge and then Schedules, and your job is
sitting there.

### Checking a job really ran

```bash
aws logs tail /ecs/carbonshift-worker --since 2h --filter-pattern CARBONSHIFT_JOB_COMPLETE
```

On Windows, run that in **PowerShell**, not Git Bash. Git Bash rewrites the
first slash of the log name into a Windows path, so the search silently finds
nothing and it looks like your job never ran.

---

## Proof that it works

All eight checks in the project specification pass.

| # | Check | How it was proved |
|:--|:--|:--|
| 1 | Reads a real live forecast | Live call, 24 real points for Germany |
| 2 | Survives API failures | Automated tests |
| 3 | Never picks an hour after the deadline | Automated tests |
| 4 | Always finds the genuinely cleanest hour | Automated tests |
| 5 | Worker runs on its own in Docker | Ran it, exit code 0 |
| 6 | AWS really fires the job on time | Ran it, fired at the exact minute |
| 7 | The completion log really appears | Read it out of CloudWatch |
| 8 | The chart is correct | Automated tests plus looking at it |

### The real run

A job was booked on 10 September with a 16 hour deadline. CarbonShift chose
09:00 the next morning, the German solar peak. AWS ran it there by itself.

| | |
|:--|:--|
| Would have run at | 17:00, at 497 gCO2/kWh, making 3.68 g CO2 |
| Actually ran at | 09:00, at 251 gCO2/kWh, making 1.86 g CO2 |
| Delay | 16 hours |
| **CO2 saved** | **1.82 g, which is 49.5%** |

The log AWS wrote:

```
[2026-09-11T09:01:07] CarbonShift worker starting
[2026-09-11T09:01:07]   payload        : carbonshift-demo-run
[2026-09-11T09:01:07]   scheduled for  : 2026-09-11T09:00:00+00:00
[2026-09-11T09:01:07]   actual start   : 2026-09-11T09:01:07+00:00
[2026-09-11T09:01:12]   checksum       : 8cca50fc7eb250ec
[2026-09-11T09:01:12] CARBONSHIFT_JOB_COMPLETE {"payload": "carbonshift-demo-run", ...}
```

Booked for 09:00:00. Started at 09:01:07. The 67 second gap is AWS starting the
container.

**The strongest single piece of evidence is the checksum.** It is identical to
the one produced by running the container on a laptop. The same job, doing the
same work, provably ran in both places. Only the hour was different.

---

## Tests

```bash
python -m pytest tests/ -q
```

**162 tests, all passing, in under two seconds.** They need no AWS account and
no internet.

| File | Covers |
|:--|:--|
| `test_carbon_api.py` | Reading forecasts, and every way that can fail |
| `test_scheduler.py` | Deadlines, picking the minimum, the AWS request, the refusal rule |
| `test_scanner.py` | Job classification, especially never marking an urgent job as shiftable |
| `test_report.py` | Report sums, and never claiming a saving it cannot prove |
| `test_dashboard.py` | Countdown, totals, and safety |
| `test_doctor.py` | Problem detection and exit codes |
| `test_deploy.py` | AWS setup branches |
| `test_worker.py` | The job always produces the same result |
| `test_chart.py` | Drawing from a real record |
| `test_menu.py` | Menu logic |

---

## What it does not do

Being clear about this matters more than sounding impressive.

* **Only AWS.** Not Azure, not Google Cloud. The decision logic would carry
  over. The booking and setup parts would need rewriting.
* **One job at a time.** There is no queue.
* **No website or login.** It is a tool you run, not a service you sign up for.
  Your keys stay on your own computer.
* **The numbers are estimates.** Energy use is based on a standard figure, not
  a meter on your actual server. The percentage saved is reliable. The exact
  grams are an estimate.
* **It trusts the forecast.** If the forecast is wrong, the choice is wrong,
  and nothing here would notice.
* **Whole hours only.** A deadline shorter than an hour usually has no usable
  slot, and CarbonShift will say so rather than guess.
* **The saving is calculated, not measured.** We prove the job moved to the
  chosen hour, and we prove the forecast said that hour was cleaner. We do not
  independently measure the grid at the moment it ran. Closing that gap is the
  clearest next improvement.

---

## Cost

The whole demonstration cost well under one US cent. Clusters, roles and
one time schedules have no standing charge. You pay only for the seconds your
job actually runs.

To remove everything afterwards, see the teardown commands at the bottom of
`infra/deploy.py`.

---

## License

MIT. See [LICENSE](LICENSE). Use it for anything.

---

*Built by Ramya Samudrala. B.Sc. Computer Science and Cloud Computing.*
