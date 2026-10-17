# Krater Compute runner

The runner (`runner/`, Go, standard library only) is the only Krater code on a rented machine. The Dockerfile's
`runner` stage builds it as a static linux/amd64 binary, and the portal serves it at `/runner/v1/krater-runner`.

## How a job starts

Krater creates the instance in Vast's `args` mode with `onstart: "sh"` (Vast's entrypoint override) and a bootstrap
script as `args` (`krater/compute/launch.py`). The bootstrap:

1. downloads the runner from `$KRATER_RUNNER_URL` with curl, wget or Python, whichever the image has;
2. checks its sha256 (when the image has `sha256sum`) and refuses to run a mismatch;
3. runs it;
4. waits forever afterwards. Vast restarts a container whose main process exits and keeps billing, so only Krater
   ends a machine, by destroying it (or the runner stops it, below).

Checked locally on 2026-10-11 with alpine (busybox wget), python:3.12-slim (Python only) and debian:bookworm-slim
(none: it prints why and waits, and Krater's runner start deadline fails the job).

The runner then:

1. calls `POST /runner/v1/start` with its token and gets the command and whether this is a resume;
2. restores the job's checkpoints into `$KRATER_CHECKPOINT_DIR` (default `/krater/checkpoints`);
3. runs the command with `sh -c`;
4. sends a heartbeat every minute, with the mean GPU use from `nvidia-smi` sampled every 15 s, and stops the
   command (SIGTERM, then SIGKILL after 30 s) when Krater answers `stop`;
5. uploads changed checkpoints every 10 minutes, and checkpoints and outputs (`$KRATER_OUTPUT_DIR`, default
   `/krater/outputs`) when the command exits, retrying a storage error for up to 5 minutes;
6. saves the command's stdout and stderr, with its own lines, to the job's log (`logs/attempt-<n>.log`, up to
   16 MB; the rest still reaches Vast's container log) and uploads it every minute and at exit;
7. reports the exit code. The scheduler destroys the machine on its next pass. If the final upload still failed
   after its retries, the exit is reported anyway (the machine mustn't bill forever) and the runner logs which of
   checkpoints and outputs were not saved, before the final log upload so the line reaches the job's log. `/exit`
   carries only the code, so the job still shows as finished.

## When Krater is gone: the machine stops itself

Krater ends machines only while it is up. On staging (2026-10-14) Krater went down with a box running, and the box
billed for four hours with nobody watching. So the runner keeps a dead-man switch (`runner/selfstop.go`): every
answer from Krater, a refusal included, resets it. Only Krater's own answers count: a 2xx, or a 4xx whose body is
a JSON object. A 5xx, an HTML page from a proxy in front of it (a WAF challenge on 403 or 429, Funnel) or no
connection doesn't, and only a 403 with a JSON `error` is taken for Krater refusing the token. After
`self_stop_after_seconds` without one (the `runner_self_stop_minutes` policy, 3 hours by default, sent with `/start`;
3 hours too when `/start` itself never got through), the runner:

1. stops a job's command (SIGTERM, up to 45 s to save a checkpoint) and doesn't report its exit, which Krater would
   take for the job's own end;
2. stops its machine through Vast's API (`PUT /api/v0/instances/{id}/`, `{"state": "stopped"}`) with the key Vast
   puts in every container for its own instance (`CONTAINER_API_KEY`, read from `/proc/1/environ` when the runner's
   environment lacks it), retrying every minute until Vast takes it.

It stops rather than destroys: GPU billing ends, and the disk keeps whatever the job or the member did while Krater
was away, since nothing reaches storage without Krater. The runner stays up after `Run` returns, so the switch also
covers a machine whose exit Krater never came to collect. Back up, Krater tells a self-stop from an outbid machine
(Vast shows both as `intended_status: stopped`) by the silence: a self-stopped runner has been quiet for at least the
self-stop time. It starts a job's machine again (the runner resumes with the checkpoints still on its disk; if the GPU
is taken, the pause rules move the job as for an outbid machine) and shows a box as stopped, for its member to resume.

A `self_stop_after_seconds` of -1 turns the switch off, for a provider whose machines can't stop themselves
(`Capabilities.self_stop`, [providers.md](providers.md)).

**How the machine stops is the provider's to say.** `/start` carries a `self_stop` descriptor, built from the active
provider's `self_stop_method` (never from its name):

| `self_stop` | The runner |
|---|---|
| `{"method": "vast_api", "url", "instance_id"}` | `PUT`s the stop to Vast's API as above; `instance_id` is a string of digits (anything else disables the stop call) |
| `{"method": "shutdown"}` | runs `shutdown -h now`, falling back to `poweroff`, then `halt`, retrying every minute until one runs (a cloud VM whose shutdown behaviour is stop) |
| `null` | has no stop call; Krater then also sends `self_stop_after_seconds: -1` |
| any other `method` | logs once, when it fires, "Krater named a self-stop method this runner doesn't know: X"; it still stops the command first and leaves the machine running |

The descriptor wins over the legacy fields. Krater still sends `vast_instance_id` (an integer) and `vast_api_url`
next to it, for runners already on machines: they ignore the unknown `self_stop` key and keep stopping through Vast
as before. A runner that gets no descriptor (an older Krater) falls back to those two fields.
`self_stop_after_seconds` keeps its meaning whatever the method: above 0 the silence in seconds, below 0 off, 0 the
3-hour default.

Checked with Go tests against a fake Vast, the scheduler's tests, and a rehearsal on staging (2026-10-14): the real
runner in a local container, a 30-minute project override, the portal stopped. It fired after 31 minutes, stopped
the command, sent one stop to a fake Vast with the container key, and reported no exit; Krater, shown the machine
stopped, paused the job and started the machine again.

**Checked on a rented machine** (staging, 2026-10-14, an on-demand RTX 3060, $0.04 for the whole test): with a
30-minute override and the portal and worker stopped right after a heartbeat, the runner fired at 31 minutes, stopped
the command, and Vast took the stop from the container key (`intended_status: stopped`, the instance kept, not
destroyed). Krater, back, recorded `self_stopped`, paused the job, started the machine again (Vast answered 200), and
the runner was reporting again within seconds. Two earlier tries on interruptible machines were taken back by Vast
within minutes of renting, before the switch could fire; Krater read both as outbid, correctly, since their runners
had reported moments before.

One gap remains: a runner whose machine restarts while Krater is still down (an outbid machine Vast resumes) can't
fetch `/start`, so it runs on the 3-hour default rather than the project's setting until Krater answers.

The scheduler doesn't count Krater's own downtime against a runner either: a live instance it hasn't looked at for
5 minutes or more restarts its runner deadlines (`watch_resumed_at`), so the first pass after an outage doesn't fail
every job whose heartbeats couldn't get through.

The job's team reads the log, outputs and checkpoints on the job page (Output and Files; downloads are redirects to
URLs signed for 15 minutes) or with `krater logs` and `krater download`. Reviewers see the job but not its files.

## Boxes

A box launches in Vast's SSH mode (`runtype: ssh_direct`): Vast's own init runs sshd and is the main process, and the
bootstrap runs as its startup script, starting the runner in the background. The runner then:

1. writes the box owner's SSH keys (from `/start`) as the whole of `~/.ssh/authorized_keys` (0600, `.ssh` 0700);
2. restores `/workspace` (`$KRATER_WORKSPACE_DIR`) from the job's stored workspace;
3. sends a heartbeat every minute with GPU use and the number of established connections to port 22, read from
   `/proc/net/tcp` and `tcp6`;
4. saves `/workspace` every 10 minutes, and when a heartbeat reply says `sync` (Krater is about to stop the box),
   then confirms with `POST /runner/v1/synced`, but only once the save succeeded (retried for up to a minute).
   After a failed save it stays silent: Krater keeps asking on each heartbeat until its sync deadline, and records
   the box as not saved.

Checked on staging (2026-10-11), plain `ubuntu:24.04` in SSH mode: Vast installs and runs sshd itself, runs the
startup script, and the runner starts within a minute of the rental. `authorized_keys` held only the member's key
(the account had no SSH keys, and Vast's `SSH_PUBLIC_KEY` was empty). 22/tcp is mapped to a public port
(`VAST_TCP_PORT_22`), which is what the job page shows; Vast's own SSH proxy (`sshN.vast.ai`) works too. Krater
refuses to start boxes while the Vast account has any SSH keys, since Vast may install them in every instance.

What a member can read inside their box: the container's environment (`/proc/1/environ`) has `KRATER_JOB_TOKEN`
(this instance's runner token: it can only report for this box), Vast's `CONTAINER_API_KEY` (it can stop or destroy
this one instance; Krater treats that like any unexpected stop) and a `JUPYTER_TOKEN` Vast generates. Boxes are
SSH-only in v1 (Adam, 2026-10-11): members run Jupyter themselves through an SSH tunnel.

## What a member's job sees

| Variable | Meaning |
| --- | --- |
| `KRATER_RESUMED` | `1` when this is a resume (a later attempt, or a restart after a pause): load the latest checkpoint |
| `KRATER_CHECKPOINT_DIR` | Save checkpoints here. Restored before the command starts |
| `KRATER_OUTPUT_DIR` | Put results here. Uploaded when the command exits |
| `KRATER_JOB_ID`, `KRATER_ATTEMPT` | For logs |
| the project's secrets | Such as `HF_TOKEN`, set on the project's Compute page |

Write a checkpoint under a temporary name ending in `.tmp` or `.partial` and rename it when it's complete: the
runner skips those names, so it never uploads a half-written file. The image needs a POSIX `sh` and one of curl, wget
or Python; its own ENTRYPOINT is not run, so put any setup in the command. An image whose `USER` isn't root can't
create `/krater/...`, so the runner uses `/tmp/krater/checkpoints` and `/tmp/krater/outputs` instead (the variables
say which). If it can't make its directories at all it reports exit code 125.

## Project secrets

A project's team sets secrets on its Compute page (`krater.services.project_secrets`): write-only, encrypted with
the keys in `KRATER_PROJECT_SECRETS_KEY_FILE` (Fernet, one per line, newest first), which only the portal reads. The
runner gets them in its `start` reply and puts them in the command's environment; for a box it writes
`/etc/profile.d/krater-secrets.sh` (mode 0600, outside `/workspace`, so never synced), which login shells read. It
logs how many, never a value. Names are capital letters, digits and underscores; `KRATER_`, `VAST_`, `CONTAINER_`,
`LD_`, `PYTHON*`, `PATH`, `HOME` and the like are refused.

What this protects and what it doesn't: values never go into Vast's create request, so they don't sit in Vast's
instance records. But the runner fetches them with `KRATER_JOB_TOKEN`, which is in that request's environment, so
anyone holding the organization's Vast key could read the token while the machine runs and fetch them too. And the
machine's host can read anything on the machine. Members should use tokens they can scope and revoke.

## Security

The runner holds one secret, `KRATER_JOB_TOKEN`. It works for this instance only and only while it is live (Krater
stores its sha256). Storage is reached through presigned URLs for this job's own keys
(`projects/<project>/jobs/<job>/checkpoints|outputs/`), single PUTs up to 64 MB and multipart parts above that, so no
request body passes Cloudflare's cap in front of the storage host. Each URL is signed for its exact size, so a
machine can't upload more than it asked for, and Krater refuses URLs past the project's storage quota (an admin
setting, 50 GB by default; replacing a file counts only the difference). Checked against SeaweedFS 4.47 on
2026-10-11: a body of the signed size gets 200, a bigger or smaller one 403, for single PUTs and parts alike.
Unfinished multipart uploads are aborted hourly once their job has ended (SeaweedFS lists no start time, so age
alone can't decide). The organization's Vast key never reaches a machine.

## Tests

```bash
docker run --rm -v "$PWD/runner:/src" -w /src golang:1.23 go test -race ./...
```
