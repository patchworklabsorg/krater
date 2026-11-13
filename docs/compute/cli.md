# The `krater` command

Krater Compute from a terminal: submit jobs and boxes, watch them, SSH into a box, manage a project's secrets. It's a
small separate package in `cli/` (Python 3.10+, httpx only) that talks to Krater's API (`/api/v1/`,
`krater.web.routers.api`) and signs in with Weave.

## For members

Install it from GitHub (it is not on PyPI), with uv or pip:

```bash
uv tool install "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"
```

Sign in, then work:

```bash
krater login --url https://krater.patchworklabs.org
krater projects                        # your projects (--all adds withdrawn and completed ones)
krater prices --gpu 4090               # what an hour costs now (--all adds models nobody offers)
krater submit <project> --name train --image ghcr.io/me/train:v3 --command "python train.py" --gpu "RTX 4090" --limit 0.35 --max-runtime 240
krater jobs <project>
krater show <job>
krater logs <job>                      # what the command printed (the end of it)
krater logs <job> -f                   # keep printing until the job is finished (--interval 5)
krater wait <job>                      # block until it's finished; handy in scripts
krater cancel <job>
krater submit <project> --name notebook --image ubuntu:24.04 --box --gpu "RTX 3060" --limit 0.08
krater ssh <job> -- -L 8888:localhost:8888
krater download <job> results/         # outputs, checkpoints and logs, under results/<kind>/ (--skip-existing)
krater open <job|project>              # the web page, in your browser
krater secrets set <project> HF_TOKEN
```

`krater login` prints a Weave link (and opens it). Sign in there, and Weave shows a code: paste it into the terminal.
That works over SSH too. The CLI keeps its sign-in in `credentials.json` under `%APPDATA%\krater` on Windows or
`~/.config/krater` elsewhere, readable by you only, and refreshes it as needed. `krater logout` asks Weave to revoke
the tokens (best effort: it says so if Weave couldn't be reached) and deletes the file either way.
`krater secrets set` reads the value from standard input or asks for it without echoing, so it never lands in your
shell history (with input redirected, such as `< file` or `< NUL`, it reads that instead of waiting at a prompt).
Every command takes `--json`, which prints Krater's answer unchanged (full ids, every row).

### Naming projects and jobs

You don't type UUIDs. The `projects` and `jobs` tables show the first 8 characters of each id, and anywhere a
command takes one:

- a `<project>` is its full id, the first characters of it (4 or more), or any part of its title in any case
  (`krater jobs vision`; an exact title wins over longer titles that contain it);
- a `<job>` is its full id, the first 4 or more characters of it, or its name in any case (`krater logs
  train-v3`). Names can repeat, so a name means the job of that name that is still queued, running, paused or
  stopped; if none is, and several have ended, the CLI lists them.

If that fits more than one, the CLI lists the candidates (short id and name) and stops. A short id, a name or a
title is looked up in the projects you're on (`GET /projects`, then each project's `/jobs`), all in the CLI. A **full** id
goes straight to the API without listing anything, so an admin can act on a job of a project they aren't on. For
the full ids, use `--json`.

### Prices

Prices are dollars per hour including the disk; a leading `$` is fine (`--limit '$0.07'`). There are two ways to
price a job:

- **A limit order**, `--limit 0.07`: it starts once a machine can be had at or under the limit, never pays more, and
  when it's outbid it keeps its machine (and what's on its disk) while Krater raises the bid up to the limit, moving
  to another machine only once one is free under the limit. Interruptible machines pay their bid, so Krater bids
  just enough to win, not the limit itself. It waits to start for as long as it takes, or `--expires 12h` cancels it
  if it hasn't started by then.
- **A target and a max**, `--target 0.07 --max 0.10`: it starts at or under the target; outbid, the bid rises up to
  the max, and after a while (the project's "move after", 30 minutes by default) it moves elsewhere and resumes from
  its last stored checkpoint. `--max-wait` and `--on-max-wait` say what happens if nothing is free at the target.
  `--on-outbid hold` keeps the machine instead, as a limit order does. On demand (`--on-demand`) needs this form.

Leave the price out and `krater submit` shows the cheapest and median price now for the GPU models you named (every
model with offers if you named none; on demand with `--on-demand`, otherwise interruptible), explains the options,
and exits with 2 without queueing anything. `krater prices` is the whole list, cheapest first, with only the models that have offers now unless
you pass `--all`, and `--gpu 4090` narrows it to names containing that.

### What it may cost

`krater submit ... --estimate` checks the options like a real submit and prints what the job may cost: its max price
for its max runtime, plus its data estimate at the transfer price cap, and recent prices for the GPU models named. It
queues nothing. It then waits up to 30 seconds for Krater's live check of Vast for the job's shape and prints the
cheapest offer that fits right now (`live` in the API's answer, polled at its `poll` path while `status` is
`checking`; `krater/services/live_prices.py`). A queued job prints the same line, and `krater show <job>` splits the spend into GPU, disk and data,
with Vast's figure for each where it has one. `POST /api/v1/projects/{id}/jobs/estimate` takes the same body as a
submit.

### Job files, sweeps and reruns

- `krater submit -f job.toml` reads `project` and any option, by its long name (`max-runtime = 120` or
  `max_runtime`), from a TOML file, plus a `[sweep]` table. An option typed on the command line wins over the file,
  so `submit`'s defaults are applied only after the file is read. Python 3.10 reads TOML with `tomli`, 3.11+ with
  the standard library.
- `--sweep NAME=V1,V2` (repeatable) and `--count N` expand on the member's side into up to 100 ordinary jobs, named
  `<name>-<index>`, each command prefixed with `export KRATER_SWEEP_INDEX=<index> NAME=<value>;` (values shell-quoted).
  Krater sees separate jobs; there is no sweep object. If Krater refuses one part-way, the CLI says which were queued.
- `krater inputs upload <project> PATH...` asks `POST /api/v1/projects/{id}/inputs/upload` for presigned URLs (one
  PUT up to 64 MB, else 64 MB parts finished with `/inputs/complete`), the same quota check a machine's uploads get,
  and skips files already stored at the same size. `list` and `rm` (a folder ends in /) complete it; `submit --input`
  names them.
- `krater rerun <job>` posts the job's `spec` from `GET /api/v1/jobs/{id}` (the submit body it was queued with) to
  the submit endpoint, so the budget, verification and transfer checks run again. `--no-smoke` drops a smoke test
  (the full run), `--smoke` makes it one.

### Trying a job before paying for it

- **`krater run`** runs a job on this computer in Docker, free. It takes `submit`'s options and job files (price
  and GPU options are ignored) and sets the container up as a Krater machine does (`krater_cli/local.py`):
  `--entrypoint sh` and `sh -c <command>` in the image's working directory, `--platform linux/amd64`, the same
  `KRATER_*` variables plus `KRATER_LOCAL=1`, and `krater-local/<name>/outputs` and `checkpoints` mounted at
  `/krater/outputs` and `/krater/checkpoints` (`--dir` moves them). Project inputs can't be downloaded, so
  `--input-dir FOLDER` gives a local folder as `$KRATER_INPUT_DIR` (read-only). Secret values can't be read back, so
  given a project it passes each secret that is set in this shell under the same name (`-e NAME`, never the value in
  argv) and lists the ones that aren't. A sweep runs one job (`--index N`, default 0); `--resumed` sets
  `KRATER_RESUMED=1`; `--local-gpu` adds `--gpus all`; `--docker podman` uses Podman. It needs no sign-in unless
  given a project, and exits with the command's exit code, explaining 125, 126, 127 and 137.
- **`krater submit --smoke`** (`--smoke-minutes N`, 1 to 30, default 5; the job form's "Smoke test first") queues
  the job on the machine it asks for as a smoke test (`krater/services/smoke.py`): the runner sets `KRATER_SMOKE=1`,
  stops the command after N minutes of it running, and the job **succeeds** ("Smoke test passed") if the command
  hadn't failed by then. A command that fails sooner fails the job as usual. The timer starts with the command,
  after the image pull and restores. The scheduler ends the machine itself at N + 10 minutes of running (the backstop),
  and the budget hold and estimate are for that, not the max runtime, which the job keeps for `rerun --no-smoke`.
  Nothing else about the job changes (its wait, price and max runtime are the full run's), so `rerun --no-smoke`
  queues exactly the full run. A sweep's smoke test queues only its first job, and says to run the same `submit`
  without `--smoke` for the whole sweep. Against a Krater from before smoke tests (which would ignore
  the field and run the whole job) the CLI cancels the job at once and says so. `krater run --smoke` does the same
  stop on this computer.

### Data transfer

Vast bills data into and out of the machine per GB on top of the hourly price. `--data-in 400 --data-out 100` (GB)
tells Krater roughly how much the job moves, so it prefers hosts where that is cheap. If that much data at the
transfer price cap would cost a large share of the project's budget, Krater asks before queueing: `krater submit`
shows the cost and asks `Queue it anyway? [y/N]`, and without a terminal the answer is no. `--confirm-transfer-cost`
queues it without asking.

### Downloading

`krater download <job> results/` fetches every file again each time: the file list has no modified time or checksum, and
a checkpoint that is uploaded again keeps its size, so a same-size file on disk can't be told from the current one.
`--skip-existing` leaves alone any local file whose size matches, which is what you want to carry on after an
interruption (or for `logs`, which only grow) but not for checkpoints you may have overwritten. Download links last
15 minutes; when one runs out partway the CLI asks Krater for fresh ones and carries on. A file name with an empty,
`.` or `..` part is skipped with a message, and a file that can't be written is reported and skipped (the exit code
is 1 at the end).

### Following and waiting

`krater logs -f` prints the end of the log and then whatever is added, checking every `--interval` seconds (5). A
retry prints `--- attempt 2 ---` and starts over; more than 1 MB of new output between checks prints
`[... skipped N bytes ...]`. It stops when the job is finished, printing its final state. `krater wait` does the
same polling without the log and exits with the job's outcome: 0 if it succeeded, the command's own exit code if it
failed with one, 1 otherwise (cancelled, or no code). `--timeout <minutes>` gives up with exit code 124 and
leaves the job running. Ctrl-C ends either one quietly with 130.

A 5xx or 429 from Krater, a timeout or a dropped connection doesn't end either one: it waits (the interval, doubling to at
most 60 s) and tries again, so a blip can't look like a failed job. Any other
error (a 4xx, an expired sign-in) still ends it with exit code 1. With `--timeout`, an outage that lasts past it
ends the wait with 124.

Every command also waits out a deploy (`krater_cli.api`): a read or an estimate that gets a 502, 503 or 504 from
Krater's proxy, and any request that couldn't connect at all, is tried again after 1, 2, 4 and 8 s. A submit or
another change that gets a 502 is never sent again, because Krater may already have done it: check with
`krater jobs` before submitting again.

Output is printed with terminal control characters and escape sequences removed (a job's output can come from
any teammate who can launch on the project); `krater logs --raw` prints it as it is.

### SSH

`krater ssh` connects only to a box whose state is `running`; a box that's still starting (its machine can have an
address already) or stopped gets a message pointing at `krater show`. On Windows it runs `ssh` as a child process
and exits with its exit code (replacing the process would detach it from the console).

## How sign-in works

The CLI is a public OAuth client of Weave (it can't keep a secret), so it uses the authorization code flow with PKCE
(S256), which Weave requires of public clients:

1. `GET /api/v1/cli-config` tells the CLI Weave's issuer, the CLI app's client id, the scopes (`openid profile`) and
   the redirect: Doorkeeper's out-of-band `urn:ietf:wg:oauth:2.0:oob`, for which Weave shows the code.
2. The member signs in at Weave's authorize page; the CLI swaps the code (plus its PKCE verifier) for an access token
   (two hours) and a refresh token.
3. Each API request carries `Authorization: Bearer <access token>`. Krater checks it with Weave using the token
   itself: `GET /oauth/token/info` must name the CLI app (`KRATER_WEAVE_CLI_CLIENT_ID`), and `GET /oauth/userinfo`
   gives the member's `sub` and applies Weave's app-access rules. (Doorkeeper doesn't let a token introspect itself,
   so introspection isn't used.) The answer is cached for a minute. Then Krater authorizes the member through Weave's
   directory exactly as for a page: roles are re-checked live for anything that changes state.
4. A token for any other Weave app is refused, and the API never accepts the browser's session cookie, so it needs
   no CSRF protection and a web page can't call it on a member's behalf.

A member who has never signed in to Krater in a browser is asked to do that once: Krater creates its user record
from Weave's id_token at that first sign-in.

## Setting it up (admins)

In Weave, create an OAuth application **Krater CLI**:

- **Confidential:** no (a public client; the CLI holds no secret).
- **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob`.
- **Scopes:** `openid profile`.
- **Who may use it:** the same app-access restriction as Krater's own app, so only Ganymede members can sign in.

Then set `KRATER_WEAVE_CLI_CLIENT_ID` to its client id in Krater's environment. The API exists only with that set and
`KRATER_COMPUTE_MODE=vast`.

**A nicer sign-in, later:** with a loopback redirect the browser hands the code straight back to the CLI. Doorkeeper
already matches `http://127.0.0.1/callback` on any port, but Weave in production refuses plain-http redirect URIs
(`force_ssl_in_redirect_uri`). A one-line Weave change allows loopback addresses:

```ruby
force_ssl_in_redirect_uri { |uri| !IPAddr.new(uri.host).loopback? rescue true }
```

After that, add `http://127.0.0.1/callback` to the app's redirect URIs and set `KRATER_WEAVE_CLI_LOOPBACK=true`.

## Developing it

```bash
cd cli
uv run --group dev pytest
```

The tests run the CLI against a fake Krater and Weave (`httpx.MockTransport`). The API's own tests are in
`tests/web/test_api.py`, and the Weave token check's in `tests/weave/test_live_client.py`.
