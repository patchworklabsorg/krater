# Integrating the OSPool

This is how Krater Compute could run members' jobs on the OSPool, for the talks with OSG. It builds on the provider seam
in [providers.md](providers.md). It draws on OSG's and HTCondor's documentation, read on 2026-10-14. A claim that
rests on a search summary, or that combines sources, is marked **unverified**. Everything else links its source.

## Recommendation

**Integrate as a gateway.** OSG sees Patchwork as the only identity. Krater submits every job, and members never get
OSG accounts. OSG allows this. Its 2024 gateway slides say "A gateway operator must be affiliated with a US-academic,
government, or non-profit institute. Gateway users can be anywhere"
([OSPool for SGX3](https://ScienceGateways.org/app/site/media/files/webinars/2024-03-21-OSPool-for-SGX3.pdf)).
This also solves the problem that Ganymede's teenage members have no institutional identity to sign in to OSG with.

**Offer it to members as a second, opt-in kind of job:** "free, may wait and be interrupted". Vast stays the default,
because Vast "starts now" and supports boxes. On the OSPool, Krater would run batch jobs only:
- one GPU each, at most 24 GB of GPU memory for most jobs;
- under 20 hours per run, with checkpoints.

**Three gates, in this order.** A "no" to any one ends or reshapes the plan:

1. **Outbound HTTPS from execution points.** Krater's runner talks to Krater and its storage during the whole job.
   - No OSPool page promises this. Sites that contribute machines must offer "permissive outbound network connectivity",
     which suggests egress works ([campus requirements](https://osg-htc.org/campus-docs/ospool-requirements/)).
   - But HTCondor 24.7.3 added a setting that lets "EP administrators ... enforce no outbound networking for jobs"
     ([OSG 24 release notes](https://osg-htc.org/docs/release/osg-24/)).
   - **First step:** run the test job in the appendix, then ask OSG.
   - If egress is blocked, Krater would have to move files through HTCondor instead ("No egress" below). That is a
     much bigger build.
2. **Is Patchwork Labs eligible?** The policy requires work "relevant to research and/or education efforts associated
   with an academic, government, or non-profit institution in the United States"
   ([AP acceptable use policy](https://osg-htc.org/docs/submit/ap-ospool-aup/)). It says nothing about minors: the
   policy pages contain no "minor", "guardian", "parent" or "under 18". Ask directly whether a US non-profit serving
   teenagers qualifies.
3. **Sign-off on automated submission.** The policy asks users to "contact us" rather than "running scripts (including
   `cron`) to automate job submission" ([policy](https://osg-htc.org/osg-portal-documentation/overview/references/policy/)).
   A portal submitting for its members is exactly that, so it needs a facilitator's agreement. It also holds AP
   operators "responsible for their users' behavior". Krater already has review, budgets and the guardian gate, which
   is a good story to tell here.

**Ask OSG for an Access Point hosted for the gateway** (route C below). If that isn't on offer, run our own (route A).

**Cost:** OSG says "All capacity and services are open - no proposal, no allocation, no fee" (SGX3 slides). The
capacity is opportunistic and shared by fair-share.

## Access Point routes

An Access Point (AP) is the HTCondor host jobs are submitted from. Krater's worker has to submit to one.

| Route | What it is | For | Against |
| --- | --- | --- | --- |
| **A. Our own AP** | Patchwork runs an AP that joins the OSPool ([install](https://osg-htc.org/docs/submit/install-ospool-ap/)). The worker submits to its schedd with an ID token. | Full control. The worker can run beside the schedd and read the job event log. Job ads are visible only to us. | Needs a host with 6 cores, 12 GB RAM and 1 TB disk, a public IP with forward and reverse DNS, and inbound TCP 9618. Also needs an OSG consultation, registration in OSG's Topology, a COManage admin contact, and the Gratia accounting probe. We would be "responsible for [our] users' behavior", OSG may disconnect the AP, and an AP idle for 90 days is disabled. OSG "strongly recommends" starting on its own APs first. |
| **B. A service account on an OSG-run AP** (ap40 and the like) | One Patchwork account on OSG's AP. The worker submits remotely. | No host to run. | Weakest route. The policy forbids persistent processes on APs, so the worker can't live there. Nothing in OSPool's documentation covers authenticating to its APs from another host. The generic HTCondor recipe runs `condor_token_fetch` on the AP and copies the token off ([remote jobs](https://htcondor.readthedocs.io/en/25.x/users-manual/submitting-a-remote-job.html)). Other users on the AP can read our job ads (`condor_q -allusers`). OSG moved accounts between APs in December 2025. |
| **C. An AP hosted for the gateway** | PATh/Morgridge host an OSPool AP for a gateway project. They did this for the Event Horizon Telescope gateway ([PEARC'24 paper](https://par.nsf.gov/servlets/purl/10645376)). | OSG runs the host. We still get our own schedd and token. It is the documented gateway precedent. | It depends on OSG agreeing. EHT had NSF funding and was a research collaboration. |

Routes A and C look the same to Krater: a schedd of our own, reached with a token. Build for that.

## Identity, accounting and minors

- **ProjectName is public.** Every job needs a registered `+ProjectName` (format `Institution_PIName`). OSG's
  per-project statistics page shows the project name, PI, organization and field of science.
  - Use **one** project, `PatchworkLabs_<staff PI>`, for all of Ganymede.
  - Never use per-team or per-member projects, because their names would carry teenagers' identities onto a public
    page.
  - This is the same line the gallery setting for minors draws (SPEC "Completion flow & public gallery").
- **Per-member attribution.** OSG documents no gateway-username attribute, unlike ACCESS's
  `gateway_submit_attributes`.
  - Krater adds its own pseudonymous ids to each job ad: `+KraterJobId`, `+KraterProjectId` (UUIDs) and
    `+KraterLabel`.
  - With these, Patchwork can answer any question OSG asks about a job, without OSG holding personal data.
  - Offer OSG periodic counts of unique members, the way ACCESS gateways report them.
- **Fair-share.** OSG shares capacity fairly between users and projects. Under one account, all of Ganymede competes as
  a single user, so Krater has to keep the fairness between its own projects:
  - a per-project limit on concurrent OSPool jobs;
  - a cap on idle jobs, using HTCondor's `max_idle` (held jobs count as idle);
  - OSG's queue limit of 10,000 jobs.
- **Budgets.** Nothing bills, so dollar budgets don't ration the pool. HTCondor reports
  `RemoteWallClockTime` and `GPUsProvisioned` for each job, which gives the actual GPU-hours
  ([job attributes](https://raw.githubusercontent.com/htcondor/htcondor/main/docs/classad-attributes/job-classad-attributes.rst)).
  The OSPool client can report these as its `charges`, which turns Krater's reconcile on for it. Two ways to use them:
  - **A shadow price.** Each GPU class has an admin-set hourly rate, and the OSPool's offers carry it. Krater's spend
    accrues at an instance's hourly rate (`krater.compute.spend`), and the hourly cap and affordability checks read the
    offer's rate. So with the rate in the offer, the ceilings, the 80%/100% alerts and the reports work during the run.
    Reconcile then corrects the total to the actual GPU-hours. **This is the recommendation.**
    - With $0 offers instead, every project could afford every job, the hourly cap would never bind, and the alerts
      would stay silent until reconcile.
    - OSPool jobs carry no target or max set by the member: the rate is Patchwork's.
  - **A separate GPU-hour allowance per project.** This is clearer to members, but it is a second ledger to build.

## How it maps onto Krater

The OSPool client implements `ComputeProvider` in `krater/ospool/`, using HTCondor's Python bindings, `htcondor2`.
The bindings are Linux wheels only (glibc 2.28+, x86-64 and aarch64), from [PyPI](https://pypi.org/project/htcondor/).
- The worker image (`python:3.12-slim`, Debian) can install them.
- Tests on Windows can't, so import `htcondor2` lazily, and have the fake replace the schedd object rather than the
  module.
- Pin an exact version. Releases 25.x ship only the v2 API, and maintainers prune old wheels
  ([migration guide](https://htcondor.readthedocs.io/en/latest/apis/python-bindings/api/version2/migration-guide.html)).
- Patch our AP past security advisory
  [HTCONDOR-2026-0001](https://htcondor.org/security/vulnerabilities/HTCONDOR-2026-0001).

| Krater | OSPool |
| --- | --- |
| Capabilities | `interruptible=True`. `charges=True`, from the GPU-hours above. Everything else is `False`: no on-demand, bids, outbid pauses, stop/start, SSH (so no boxes), self-stop or search quota. Job validation already refuses the rest with a message naming the provider. |
| `search_offers` | One "offer" per GPU class, at the class's shadow rate (above): capability and memory from `GPUs_Capability` and `GPUs_GlobalMemoryMb` in slot ads (`Collector.query(AdType.Slot, 'GPUs > 0')`), cached for an hour. Whether a host other than the AP may read the OSPool collector is **unverified**. If it can't, fall back to a static class list. Past GPU jobs ran on V100 16 GB, 2080 Ti 10 GB, A10 22 GB, RTX 3090 and 4090 24 GB, and A100 40/80 GB. No H100 ([GPU jobs](https://osg-htc.org/osg-portal-documentation/htc_workloads/specific_resource/gpu-jobs/)). |
| `create_instance` | `Schedd.submit(Submit({...}), spool=True)`, then `Schedd.spool(result)` to upload `krater-runner` and `job-token`. Details below. In `htcondor2`, `spool` defaults to `False`, and without it the input files would exist only on the worker. The cluster id becomes the instance id (`1234.0`, so instance ids must be strings). |
| `show_instance` / `list_instances` | One query per scheduler pass, `constraint='Owner == "<us>" && KraterLabel =!= undefined'` with a projection, answering every show and list from it. A remote tracker should query a schedd at most once a minute ([scalable job tracking](https://htcondor.readthedocs.io/en/24.x/apis/python-bindings/tutorials/Scalable-Job-Tracking.html)). Jobs that left the queue come from `history(since=...)`. |
| `destroy_instance` | `act(JobAction.Remove, "1234.0")`. Then confirm on later passes that the job left the queue. `act`'s return value is undocumented. |
| Interruption | HTCondor re-runs an evicted job by itself, on another slot. Detect it from `NumJobStarts` rising, or from `LastVacateTime` and `VacateReasonCode` (1011 preempted, 1013 shut down, 1014 drained). The runner's next `/start` already counts as a resume (`runner_starts > 1`) and restores the last checkpoint from Krater's storage. |
| `stop` / `start` / `change_bid` / SSH keys | Not offered (capabilities). |

**State mapping.** The JobStatus codes are from HTCondor's
[job status codes](https://raw.githubusercontent.com/htcondor/htcondor/main/docs/codes-other-values/job-status-codes.rst).

| JobStatus | Krater phase |
| --- | --- |
| 1 Idle | provisioning: waiting for a slot, or re-waiting after an eviction |
| 2 Running | running |
| 3 **Removing** | still there. It is gone only when it leaves the queue. |
| 4 Completed | finished. Read the runner's report, then remove the job if it lingers. |
| 5 Held | depends on `HoldReasonCode`. 15 (submitted on hold) and 16 (spooling input) are normal steps of a spooled submit. Others are failures or limits: 26 (system periodic hold, such as memory), 34 (out of resources), 46/47 (duration exceeded), 12/13 (transfer). Krater treats a hold as a lost machine: remove the job, then requeue from the checkpoint or fail it, by the job's attempt count. |
| 7 Suspended | running, but treat it as interrupted for heartbeats |

### The submit description

```
universe                = container
container_image         = osdf:///ospool/<ap>/data/<us>/images/<image>-<digest>.sif   # or docker://, see Images
executable              = krater-runner                  # shipped, so no download from inside the job
transfer_executable     = true
transfer_input_files    = job-token                      # the runner's token as a file, never in `environment`
should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
request_gpus            = 1
gpus_minimum_capability = <from the job>
gpus_minimum_memory     = <MB, from the job>
request_cpus / request_memory / request_disk = <from the job>
+ProjectName            = "PatchworkLabs_<PI>"
+JobDurationCategory    = "Medium"                       # held at 20 h; "Long" holds at 40 h
+KraterLabel            = "<prefix><job>-<attempt>"
+KraterJobId            = "<uuid>"
+KraterProjectId        = "<uuid>"
environment             = "KRATER_URL=... KRATER_INSTANCE_ID=..."   # nothing secret
periodic_remove         = NumJobStarts > 10              # stop before OSG's 30-execution hold
max_retries             = 0
```

- **The job token.** Job ads, `Environment` included, can be read by anyone the schedd grants READ
  ([`condor_q -allusers`](https://htcondor.readthedocs.io/en/latest/man-pages/condor_q.html)). So the token goes in a
  transferred file, read from the sandbox's working directory (`$_CONDOR_SCRATCH_DIR`). Project secrets need no
  change: they arrive in the `/start` reply, never in the submit.
- **The runner is the executable.** OSPool ignores the image's entrypoint, and so does Krater's launch already. The
  shipped runner replaces the bootstrap's download, which removes one dependency on egress.
  - The runner must **exit** when the job is done. On Vast the bootstrap waits forever, because Vast restarts a
    container whose process ends. On HTCondor, waiting would hold the slot until the duration limit.
  - The runner needs a "provider" field in `/start` telling it which of the two to do. That fits the self-stop
    descriptor planned in providers.md.
- **Runtime.**
  - A Medium job is held at 20 hours and a Long job at 40, unless it checkpoints through `checkpoint_exit_code`
    at least as often as its category allows
    ([duration categories](https://osg-htc.org/osg-portal-documentation/htc_workloads/workload_planning/jobdurationcategory/)).
  - The risk of interruption "rises after 10 hours"
    ([checkpointing](https://osg-htc.org/osg-portal-documentation/htc_workloads/submitting_workloads/checkpointing-on-OSPool/)).
  - Krater already checkpoints to its own storage. So rather than using exit code 85, an OSPool job's max runtime per
    run is capped at 20 hours (`allowed_execute_duration`, hold code 47).
  - A job that needs longer requeues as a new attempt from its checkpoint, as on Vast.
- **Spooling.** A worker that isn't on the AP must spool. `submit(..., spool=True)` creates the job held (hold code
  15) until `spool(result)` uploads its input files (hold code 16 while uploading). `condor_submit -name` does this on
  its own, but `htcondor2` doesn't. A spooled job stays in the queue after it completes. Nothing comes back through HTCondor here: outputs go to Krater's storage. So Krater removes completed
  jobs itself, and its fake must model hold codes 15 and 16 as normal.

### Changes to Krater itself

Besides the "Still Vast-shaped" list in [providers.md](providers.md#still-vast-shaped-to-change-with-the-second-provider),
which covers string instance ids, a neutral instance phase, launch split per provider and a provider per job:

1. **Deadlines start when the job runs, not when it is submitted.**
   - The runner's start deadline counts from the rental. On the OSPool, a job can sit idle for hours, and again after
     each eviction.
   - Start the clock at `JobCurrentStartDate`, and suspend heartbeat-loss handling while the job is idle.
   - The job's `max_wait_minutes` covers the idle time instead. A job still idle at the end is removed, and expires as
     queued jobs do today.
2. **Never two copies.** If Krater requeues a job whose heartbeats stopped while HTCondor still holds it, two copies
   would run.
   - On a lost heartbeat, Krater removes the HTCondor job and waits until it has left the queue before submitting the
     next attempt.
   - An HTCondor re-run after an eviction is not a new Krater attempt: same instance, `runner_starts + 1`.
3. **The runner exits when done**, as above. The runner already falls back to `/tmp/krater/...` when it can't create
   `/krater`. HTCondor bind-mounts `/tmp` into the job's scratch directory by default (`MOUNT_UNDER_SCRATCH`), which is
   deleted when the job leaves the machine. That OSPool's execution points keep this default is **unverified**.
4. **Images**, below.
5. **A provider per job** (providers.md): the job form gains "free, may wait" vs. "paid, starts now". The scheduler then
   runs one pass per active provider.

### Images

OSPool's documented path is a `.sif` file, built with Apptainer outside the AP (the AP has no Docker), and served
through OSDF ([containers](https://osg-htc.org/osg-portal-documentation/htc_workloads/using_software/containers/)).
Caches key on the file name, so every update needs a new name.

- HTCondor accepts `docker://` images in `container_image`. OSPool's pages never show it, so whether execution points
  pull directly is **unverified**.
  - Docker Hub allows 100 anonymous pulls per 6 hours per IP, which would bite a batch of jobs.
- OSG's release notes mention authentication for fetching Docker images. There is no OSPool how-to.

**Phase 1:**
- a short list of Patchwork-built images, such as PyTorch and JAX on CUDA, as `.sif` files on OSDF;
- OSG's own images on CVMFS (`/cvmfs/singularity.opensciencegrid.org/`).

Later, Krater could convert a member's image (`apptainer build x.sif docker://...`) in a build step, and stage it
under a name containing its digest.

### No egress (the fallback if gate 1 fails)

If jobs can't reach Krater, the runner can't talk to it during the job, and HTCondor moves the files instead:

- **Before the job:** checkpoints and the start info (command and secrets) go in as `transfer_input_files`.
- **After the job:** outputs, checkpoints and the log come back through `transfer_output_files` to the AP. Krater's
  worker then fetches them (`retrieve`) and stores them.

What it costs:
- no live logs, heartbeats or GPU-use figures;
- stops only through `condor_rm`;
- checkpoints only through exit code 85 (which saves `transfer_checkpoint_files` to the AP);
- secrets in the job's sandbox on the AP, which any site admin can read.

The data limits are under 1 GB per file through the AP, and a 100 GB OSDF quota
([data overview](https://osg-htc.org/osg-portal-documentation/htc_workloads/managing_data/overview/)).

It is a second runner mode and a second file path in the worker. **Build it only if OSG says egress is blocked.** HTCondor's
`s3://` output plug-in, whose URLs are pre-signed on the AP, is a middle way that also needs OSG's confirmation.

## Testing

- **A local pool.** [mini-htcondor-docker-pool](https://github.com/ColeBollig/mini-htcondor-docker-pool) provides a
  central manager, an AP, an execution point and a remote host with only `htcondor2`, using ID tokens. That is our
  topology exactly.
  - Fake GPUs come from `condor_gpu_discovery -simulate`, through `use feature: GPUs(-simulate:...)`. That they then
    match jobs is **unverified**.
  - Container jobs need Apptainer on the execution point, which the image lacks.
  - Test CPU-only jobs with the real runner first.
- **A fake schedd** in the test suite, serving the calls the client makes, as `FakeVast` does for Vast. It must cover:
  - Idle, Running, Removing, Completed and Held;
  - hold codes 15 and 16;
  - evictions that raise `NumJobStarts`;
  - jobs that linger after completing.

## Plan

1. **Talk to OSG (no code).**
   - Write to support@osg-htc.org, or come to office hours: Tue 4–5:30 pm ET, Thu 11:30 am–1 pm ET.
   - Ask the questions below. A Patchwork staff member gets an ordinary OSPool account (COManage; Google or GitHub
     only "if your institution is not an option").
   - Run the egress test job (appendix) on ap40 by hand: CPU first, then one GPU.
2. **Krater prerequisites:** the "Still Vast-shaped" changes, a provider per job, and runner changes 1–3 above. These
   help AWS/GCP too.
3. **`krater/ospool/`:** the client, the fake schedd, and the local pool for the runner end to end.
4. **Pilot** on the agreed AP route, with a staff test project. Measure:
   - time from submit to start for one-GPU jobs;
   - eviction rates;
   - whether `docker://` pulls work.
5. **Members:** opt-in per job, with the curated images, a shadow price or GPU-hour cap, a per-project concurrency
   limit, and unique-member counts for OSG.

## Questions for OSG

1. Can jobs on OSPool execution points open outbound HTTPS to an arbitrary host for their whole run? Do any sites
   enforce "no outbound networking", and is there a ClassAd to require egress?
2. Is Patchwork Labs, a US non-profit running a research program for teenagers, an eligible institution? Does the
   operator need an academic sponsor?
3. May a gateway serve members under 18? Patchwork reviews every project, enforces budgets, and requires verified
   guardian consent.
4. Will you sign off on a portal submitting automatically for its members? What rate, idle and concurrency limits do
   you want?
5. Which AP route do you prefer for us: an AP hosted for the gateway (as for EHT), our own AP, or a service account? For
   a service account, how would a host other than the AP authenticate (ID token, SciTokens)?
6. One `ProjectName` for the whole program, or per team? Can we keep minors' names off the public project page?
7. Does a pseudonymous per-member id in the job ad, plus periodic unique-member counts, meet your accounting needs?
8. Can execution points pull `docker://` images directly? Do private registries work? What image size limits apply?
9. GPU supply in 2026: roughly how many GPUs, which models, and what wait times and eviction rates for 1-GPU jobs of a
   few hours? Anything above 24 GB beyond the A100s?
10. Can other users or site administrators read a job's environment or sandbox?
11. Can a host other than the AP read the OSPool collector's slot ads?
12. If we later take paying or for-profit partners, does anything change?

## Alternatives

| | Fits | Doesn't fit |
| --- | --- | --- |
| **NSF ACCESS** ([policy](https://allocations.access-ci.org/allocations-policy)) | **The best-documented gateway model:** <ul><li>a community account;</li><li>the gateway user submitted with each job;</li><li>quarterly unique-user counts.</li></ul> Eligible: US non-profits "directly associated with education or research", and a K-12 teacher may be the PI. Explore allocations are 400,000 credits. Jetstream2 has GPU VMs (A100, L40S, H100 by quota), which could carry boxes the way clouds do (**unverified**). | The PI must hold a graduate degree or higher and an institutional email. Allocations need a proposal. |
| **NRP Nautilus** ([nrp.ai](https://nrp.ai/)) | Free to US non-profit research and education. Kubernetes. | Namespace admins are faculty, staff or postdocs. No service-account guidance. GPU use must stay above 40%, and idle pods are banned. A100 quota defaults to zero. |
| **NAIRR Pilot** | AI research allocations for non-profits. | Institutional email required, reviewed monthly, results must be open, no stated programmatic access. K-12 not mentioned. |
| **PATh Facility** | Dedicated A100s, no eviction. Reached by `htcondor annex` from an OSG AP. | "NSF-funded researchers" only. |
| **Cloud credits** (AWS through TechSoup, Azure for nonprofits) | They fit the AWS/GCP provider in providers.md. | Small: Azure's is $2,000 a year. TechSoup's AWS terms are **unverified** here. |

## Appendix: the egress test job

Run this by hand from an OSPool account. It needs no Krater code. While it runs, Krater staging is reachable from the
internet, because it needs the Funnel back on. Turn the Funnel off afterwards.

**Setup:**
- `TEST_URL`: a Krater health URL through the Funnel.
- `PUT_URL`: a presigned PUT for a throwaway key on staging's storage.
  - Sign it for exactly **1,048,576 bytes**. Krater's URLs are bound to their size, and any other size gets a 403.
  - Give it an expiry of a day or more, because twenty queued jobs can start hours apart.

`egress.sub`:
```
TEST_URL                = https://<staging funnel>/healthz
PUT_URL                 = <presigned PUT, 1,048,576 bytes, 24 h expiry>
executable              = egress.sh
arguments               = "'$(TEST_URL)' '$(PUT_URL)'"
+ProjectName            = "<our project>"
job_machine_attrs       = GLIDEIN_Site
request_cpus            = 1
request_memory          = 1GB
request_disk            = 1GB
# request_gpus = 1       # second run
log                     = egress.log
output                  = egress.$(Cluster).$(Process).out
error                   = egress.$(Cluster).$(Process).err
queue 20                 # spread across sites
```

`egress.sh`:
```sh
#!/bin/sh
echo "host=$(hostname)"
curl -sS -m 20 -o /dev/null -w 'get %{http_code} %{time_total}s\n' "$1" || echo "get failed: curl exit $?"
head -c 1048576 /dev/urandom > probe.bin
curl -sS -m 60 -o /dev/null -w 'put %{http_code} %{time_total}s\n' -T probe.bin "$2" || echo "put failed: curl exit $?"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
```

**Reading it:**
- Any HTTP status, even a 403 or 404, means the job reached the host. Only a curl failure means egress is blocked:
  - exit 6: name resolution;
  - exit 7: connection;
  - exit 28: timeout.
- A 403 on the PUT points to the URL (size or expiry), not to the network.
- Find each job's site with `condor_history -af ClusterId ProcId MachineAttrGLIDEIN_Site0`. `$GLIDEIN_Site` may not be
  in the job's environment.
- **Pass:** every job reaches both hosts, across many sites.
- **Failures clustered by site:** egress depends on the site, and Krater would need a requirement that excludes those
  sites.
