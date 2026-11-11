# Running jobs and boxes

How to run your project's compute on Krater: preparing an image, submitting a job, choosing a price, watching it,
getting your results back, and working interactively in a box. Everything here works from the web and from the
`krater` command in a terminal.

> **Is compute open?** If your project page says *Launching machines from Krater isn't open yet*, an admin hasn't
> switched Krater Compute on for this site. Everything below applies once it is.

- [The short version](#the-short-version)
- [How it works](#how-it-works)
- [Before your first job](#before-your-first-job)
- [Preparing your image](#preparing-your-image)
- [What your command sees](#what-your-command-sees)
- [Submitting a job on the web](#submitting-a-job-on-the-web)
- [Choosing a price](#choosing-a-price)
- [Data transfer](#data-transfer)
- [After you queue it](#after-you-queue-it)
- [Getting your results](#getting-your-results)
- [Boxes: working interactively](#boxes-working-interactively)
- [Secrets](#secrets)
- [From a terminal: the `krater` command](#from-a-terminal-the-krater-command)
- [Recipes](#recipes)
- [Your budget and compute](#your-budget-and-compute)
- [Troubleshooting](#troubleshooting)
- [Limits and defaults](#limits-and-defaults)

## The short version

1. Get your project approved, with a budget, and every team member verified in Weave.
2. Build a container image with your code, push it to a registry the internet can reach (Docker Hub, GitHub
   Container Registry), and make sure it has `sh` and one of curl, wget or Python.
3. Make your program save checkpoints to `$KRATER_CHECKPOINT_DIR`, resume from them when `$KRATER_RESUMED` is `1`, and
   write its results to `$KRATER_OUTPUT_DIR`.
4. On your project's page, open **Compute**, then **New job or box**. Fill in the image, the command, the GPU, and a
   price, and choose **Queue it**.
5. Watch the job's page. When it finishes, download your results from **Files**, or run `krater download <job>`.

## How it works

Krater rents GPU machines from [Vast.ai](https://vast.ai), a marketplace where hosts offer GPUs at their own prices,
and pays for them from your project's budget. You never need a Vast account or a card: Krater holds the account,
rents the machine, runs your container on it, and destroys the machine when your job is done.

There are two kinds of work:

| | **Job** | **Box** |
| --- | --- | --- |
| What it is | Runs one command, then the machine is destroyed | A workspace you SSH into |
| Good for | Training runs, batch inference, data processing, sweeps | Exploring, debugging, notebooks, building your image |
| Ends when | The command exits, you cancel it, or it hits its max runtime | You cancel it. It stops itself when idle and can be resumed |
| What's kept | Checkpoints, outputs and the log, in Krater's storage | `/workspace`, in Krater's storage, between sessions |

Machines are either **interruptible** or **on demand**:

- **Interruptible** machines are rented by bidding. They're usually much cheaper, but someone who bids more can take
  the GPU, which **pauses** your machine. Krater then raises your bid, waits, or moves your job to another machine,
  depending on how you priced it.
- **On demand** machines cost more, but nobody can take them from you.

A machine is one host with 1 to 8 GPUs. Krater doesn't spread a job across several machines.

## Before your first job

Check these, in order. Each one stops a job from starting.

1. **Your project is approved and has a budget.** The project page shows a ceiling above $0. Jobs can run while the
   project is *approved*, *pending completion review* or *completion changes requested*.
2. **No funding hold.** A project approved as *awaiting funding* or *awaiting 3rd party review* can't start compute
   until an admin clears the hold.
3. **Everyone on the team is verified in Weave.** That's ID verification for every member, and a verified guardian
   for anyone under 18. Until then the budget is held and nothing starts. The project page and the new-job page list
   who it's waiting on and why, with a link to Weave's verification page.
4. **You're on the team.** Only the lead and members who accepted their invitation can queue jobs. Reviewers can see
   your jobs, but not their files or output.
5. **For a box: an SSH key.** Add your public key under **SSH keys** (in the top bar, `/account/ssh-keys`). It's the
   contents of a `.pub` file, such as `~/.ssh/id_ed25519.pub`. Never paste a private key.

## Preparing your image

Krater runs your code inside a container image you build. Any image works if it has:

- **a POSIX shell** (`sh`), and
- **one of curl, wget or Python**, which Krater uses to fetch its small helper program (the *runner*) onto the
  machine.

Most images already do: `python:3.12-slim`, `pytorch/pytorch`, `nvidia/cuda:*-runtime-*`, `ubuntu`, `alpine`.
`debian:bookworm-slim` on its own has none of the three, so a job on it never starts.

Things to know:

- **Your image's own `ENTRYPOINT` and `CMD` aren't run.** Krater runs your command with `sh -c` instead. Put any setup
  (activating an environment, `cd` into your code) in the command itself.
- **The image must be public, or on a registry the host can pull from anonymously.** Machines pull it from the
  internet. Don't bake secrets into it: use [project secrets](#secrets) instead.
- **Pin a version.** Use a tag such as `ghcr.io/you/train:v3`, not `latest`. The job page shows the exact digest each
  machine ran, so you can tell later which build a result came from.
- **Big images cost time and money.** Every new machine pulls the whole image, and data into the machine is billed
  (see [Data transfer](#data-transfer)). A 15 GB image pulled on every move adds up.
- **Non-root images work.** If your image's user can't create `/krater/...`, Krater uses `/tmp/krater/...` instead,
  and the environment variables point there.
- **Use a CUDA build that matches your framework.** Hosts provide recent NVIDIA drivers. A
  `pytorch/pytorch:*-cuda12.*-runtime` image is a safe start.

A minimal training image:

```dockerfile
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
```

Build, then push it somewhere public:

```bash
docker build -t ghcr.io/you/train:v1 .
docker push ghcr.io/you/train:v1
```

The command for a job on that image would be `cd /app && python train.py`.

**Try it locally first.** Krater can't debug your code for you, and a job that fails in its first minute still costs
the machine's startup. Run the same command in the same image on your own computer before you rent a GPU:
`docker run --rm ghcr.io/you/train:v1 sh -c "cd /app && python train.py --steps 10"`.

## What your command sees

Krater sets these environment variables for your command:

| Variable | What it's for |
| --- | --- |
| `KRATER_CHECKPOINT_DIR` | Save checkpoints here. Krater restores the latest ones here before your command starts, and uploads changed files every 10 minutes and when the command exits |
| `KRATER_RESUMED` | `1` when this run is a resume: a later attempt after a move, or a restart after a pause. Load your latest checkpoint |
| `KRATER_OUTPUT_DIR` | Put your results here. Uploaded when the command exits |
| `KRATER_JOB_ID`, `KRATER_ATTEMPT` | The job's id and which attempt this is (1, 2, ...), for your own logs |
| Your project's secrets | Such as `HF_TOKEN`, set on the project's Compute page |

By default the directories are `/krater/checkpoints` and `/krater/outputs` (or `/tmp/krater/...` for a non-root
image). Always read the variables rather than hard-coding a path.

### Checkpoints: why they matter

Your command can stop part-way through, for reasons that aren't its fault:

- an interruptible machine is outbid and Krater moves the job to another machine;
- a host fails, and Krater starts the job again elsewhere;
- Krater loses touch with the machine for a long time, and the machine stops itself to stop billing.

Each time, the job starts again from the beginning of your command, with the latest uploaded checkpoints restored
and `KRATER_RESUMED=1`. A program that saves checkpoints carries on where it left off. One that doesn't starts over
and pays for the same work twice.

Rules for checkpoints:

- **Write each checkpoint under a temporary name, then rename it.** Krater skips files ending in `.tmp` or
  `.partial`, so it never uploads a half-written one. A rename is atomic, so the finished file appears all at once.
- **Keep only what you need.** Your project has a storage quota (50 GB unless an admin set another) shared by all its
  jobs' checkpoints and outputs. Overwrite `latest.pt` rather than keeping every step.
- **Save often enough.** Uploads happen every 10 minutes, so work since the last upload is lost on a move. Saving
  every 10 to 30 minutes of compute is a good balance.

A PyTorch example:

```python
import os
import torch

ckpt_dir = os.environ.get("KRATER_CHECKPOINT_DIR", "checkpoints")
out_dir = os.environ.get("KRATER_OUTPUT_DIR", "outputs")
os.makedirs(ckpt_dir, exist_ok=True)
os.makedirs(out_dir, exist_ok=True)
latest = os.path.join(ckpt_dir, "latest.pt")

start_step = 0
if os.environ.get("KRATER_RESUMED") == "1" and os.path.exists(latest):
    state = torch.load(latest, map_location="cuda")
    model.load_state_dict(state["model"])
    optimizer.load_state_dict(state["optimizer"])
    start_step = state["step"] + 1
    print(f"resuming from step {start_step}")

for step in range(start_step, total_steps):
    train_one_step(model, optimizer)
    if step % 500 == 0:
        torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(), "step": step}, latest + ".tmp")
        os.replace(latest + ".tmp", latest)  # appears all at once

torch.save(model.state_dict(), os.path.join(out_dir, "model.pt"))
```

With Hugging Face `Trainer`, point `output_dir` at `$KRATER_CHECKPOINT_DIR`, set `save_total_limit=1` or `2`, and
pass `resume_from_checkpoint=True` when `KRATER_RESUMED` is `1`. Save the final model to `$KRATER_OUTPUT_DIR`.

### Output and logs

Everything your command prints (stdout and stderr) goes to the job's log, which Krater uploads every minute and
shows on the job page. It keeps up to 16 MB per attempt. Print progress lines (`step 1200/50000, loss 0.84`) so you can
see the job is alive. The command's exit code decides the outcome: `0` is *succeeded*, anything else *failed*.

## Submitting a job on the web

On your project's page, open **Compute**, then **New job or box**. The page shows current prices beside the form.

### What to run

| Field | What to enter |
| --- | --- |
| **Name** | Something you'll recognise later, such as `train-v3-lr3e-4`. Up to 200 characters. Names can repeat |
| **Kind** | **Job** runs a command and ends. **Box** is an SSH workspace (see [Boxes](#boxes-working-interactively)) |
| **Container image** | Such as `ghcr.io/you/train:v1`. Lowercase, as registries write it |
| **Command** | What the job runs, through `sh -c`, such as `cd /app && python train.py --epochs 3`. Up to 2,000 characters: put longer scripts in the image. A box has no command |

### Machine

| Field | What to enter |
| --- | --- |
| **GPU models** | Tick every model you'd accept. With several, Krater picks the best performance per dollar among the offers at your price |
| **Or any GPU with at least (GB of memory)** | Instead of models: any GPU with that much memory (1 to 256 GB). Useful when memory is what matters, such as "anything with 24 GB" |
| **GPUs** | 1 to 8, all on one machine. Your code must use them itself (for example with `torchrun --nproc_per_node=N`) |
| **Disk (GB)** | 8 to 2,000; 32 by default. It has to hold the image, your data, checkpoints and outputs. Disk is billed by the hour, so don't ask for far more than you need |

Choose GPU models or a minimum memory: one of the two is required.

### Price

See [Choosing a price](#choosing-a-price). In short, choose an order type and fill in:

| Field | What it does |
| --- | --- |
| **Order** | **Limit order** (one price) or **Target and max** (two prices) |
| **Limit** | For a limit order: the most you'll pay per hour, all in |
| **Start at or under** / **Never pay more than** | For a target-and-max order |
| **Interruptible** | Ticked: bid for a cheaper machine that can pause. Unticked: rent on demand (target-and-max only) |
| **Wait at most (minutes)** | How long to wait for a machine at your price. 720 (12 hours) by default, up to 7 days |
| **Then** | What happens after that wait: keep waiting, accept offers up to your max price, rent on demand up to your max price, or cancel the job |
| **Stop after (minutes of running)** | The max runtime. The job fails and its machine is destroyed when it has run this long. 480 (8 hours) by default, up to 7 days |

Prices are the **full hourly cost**: the GPU plus your disk. No price can be above your project's hourly cap, shown
at the top of the form ($5 an hour unless an admin set another).

Choose **Queue it**. The job starts when a machine at or under your price is free.

To see the price first, choose **Estimate the cost**: the form comes back filled in, with what the job may cost and
nothing queued. It shows:

- **Up to**: your max price for the max runtime, the most the machine can cost. Most jobs finish sooner and cost less.
- **Data**: your data estimate at the transfer price cap, the most that data can cost.
- **Recent prices** for the GPUs you named (not for "any GPU with this much memory"): the cheapest and typical full
  hourly price over the last day. If even the cheapest is above your max price, expect the job to wait.

## Choosing a price

### Look at the market first

- The **Prices right now** table on the form shows, for each GPU, the cheapest 1-GPU offer and the typical range, for
  interruptible and on-demand machines.
- [GPU price history](/pricing/history) shows each GPU's cheapest price and typical range over the last day, week or
  month. Pick a price from it, not from a guess.
- `krater prices --gpu 4090` shows the same from a terminal.

Prices there are for one GPU with 32 GB of disk. For 4 GPUs, expect roughly 4 times the price; more disk adds a little.

### Limit order (the default, and usually the best choice)

One price. The job:

- starts when a machine is free at or under the limit;
- never pays more than the limit. Krater bids just enough to win the machine, often well under your limit;
- when outbid, **keeps its machine**, and what's on its disk, while Krater raises the bid up to your limit. It moves
  to another machine only when one is free under the limit. Nothing on the machine is lost while it waits.

Use it when you know what an hour is worth to you and can wait. It's always interruptible.

### Target and max

Two prices. The job:

- starts at or under the **target**;
- when outbid, waits a few minutes (5 by default), then raises its bid step by step up to the **max**;
- if it's still paused after a while (30 minutes by default), Krater destroys that machine and starts the job on
  another one, from its last uploaded checkpoint.

Use it when you'd rather move than wait on a paused machine, or for **on demand** (untick *Interruptible*), which
needs this form. With on demand nobody can take the machine, so the target is simply the most you'll pay to start.

### When nothing is free at your price

After **Wait at most**, the job does what you chose under **Then**:

| Choice | What happens |
| --- | --- |
| Keep waiting | It waits as long as it takes (until you cancel it) |
| Accept offers up to my max price | It starts on anything up to the max, not just the target |
| Rent on demand, up to my max price | It switches to an on-demand machine, up to the max |
| Cancel the job | It gives up. Nothing is charged |

### Rules of thumb

- **A target near the cheapest price** waits for the dips. Fine for long jobs that checkpoint, overnight.
- **A target inside the typical range** starts within minutes most of the time.
- **Max a little above target** lets a bid raise keep your machine through a short price spike.
- **Work that can't checkpoint** (a long evaluation with no saved state, a demo you're running live): use on demand.
- **Several GPU models** give the scheduler more offers to choose from, so jobs start sooner.

## Data transfer

Vast bills each GB into and out of a machine at the host's own price, **on top of** the hourly price. Data in
includes the image pull and the checkpoints Krater restores, again on every move. Data out includes checkpoints,
outputs and anything your code uploads elsewhere.

Krater protects your budget four ways:

- **A price cap.** Krater never rents a host that charges more than $20 per TB each way (an admin can change this).
- **It counts as it goes.** Your machine reports what it has downloaded and uploaded every minute, and Krater adds
  that to your spend at the host's prices straight away, along with the image pull (from the image's size, each
  time a machine pulls it). Vast's own charges replace the estimate every hour.
- **Your estimate.** Fill in **Data into the machine** and **Data out of the machine** (in GB) on the form, or pass
  `--data-in` and `--data-out` to `krater submit`. Leave out the image: Krater knows its size. Krater then prefers
  hosts where your data is cheap, and holds its cost in your budget with the job's hours. A job needs enough budget
  for its first hour plus the pull and your data in before it starts.
- **Fast hosts for big images.** With a big image, Krater prefers a host that downloads it quickly: a slow link can
  keep a job waiting for its image for most of an hour.
- **A warning.** If your estimate, at the cap, could cost 10% or more of the project's budget, or more than is left,
  Krater asks you to confirm before queueing. A running job whose transfer passes that share posts a warning to the
  project's Slack channel, mentioning you.

Download big datasets once per machine, not once per step. Keep checkpoints small.

## After you queue it

### States

A job goes **queued → starting → running**, and ends **succeeded**, **failed** or **cancelled**. An interruptible
job that's outbid goes **running → paused**, then back to **running** when its bid wins again, or back to **queued**
when Krater moves it to another machine.

| State | Meaning |
| --- | --- |
| **queued** | Waiting for a machine at your price, for budget, for the team's verification, for a free place under the project's machine limit, or while an admin has stopped compute. The reason shows under the name |
| **starting** | A machine is rented and pulling your image. The disk bills from now; the GPU from when it runs |
| **running** | Your command is running |
| **paused** | The interruptible machine was outbid. See [Choosing a price](#choosing-a-price) for what happens next |
| **stopped** | A box that was stopped (idle, or you stopped it). Its disk is kept until it's resumed or destroyed |
| **succeeded** | The command exited with 0. The machine is destroyed within a minute or so |
| **failed** | The command exited with another code, ran past its max runtime, or the machine never got going. The reason is on the page |
| **cancelled** | You (or an admin) cancelled it, or it gave up waiting |

### The job page

Open the job from your project's **Compute** page. It shows:

- **Job**: the image (and the exact digest it ran), the command, GPU, price, what it **may cost** (the same estimate
  as the form's), what it has spent so far, and its exit code once it has one;
- **What it cost**: the spend split into GPU, disk, data in (with the image pull) and data out, Krater's own estimate
  next to what Vast charged for each part at its last hourly check. Vast's figures are the ones the bill uses. Jobs
  from before 12 November 2026 show only the total;
- **Output**: the end of what the command printed, updated every minute, with a link to the whole log;
- **Files**: checkpoints, outputs and logs, to download;
- **What happened**: every step, with times: queued, rented, started, outbid, bid raised, moved, finished;
- **Machine**: the GPU and price it was rented at, and for a box, how to connect;
- **Actions**: **Run again** (the form, filled in from this job), **Cancel** (and for a box: **Stop**, **Resume**,
  **Start it elsewhere**).

**Cancel** destroys the machine within a minute or so. Anything not yet uploaded is lost, so let a job finish if you
want its last outputs.

### What Krater does on its own

- **Finished machines are destroyed** within a minute or two, so nothing keeps billing.
- **A machine that never starts** (the image can't be pulled, has no shell, or the host is broken) is destroyed, and
  the job is tried on another host. After three bad hosts in a row, or 30 minutes without starting, the job fails:
  by then the image is the likelier cause.
- **A runner that stops reporting** (the machine crashed or lost its network) for 30 minutes fails the job.
- **If Krater itself is down** for a long time (3 hours by default), your machine stops itself, keeping its disk, so
  nothing bills unwatched. When Krater is back it starts the machine again and your job carries on from the
  checkpoints on its disk.
- **When a job ends**, Krater posts in your project's Slack channel: finished, failed (with why), or stopped by Krater
  or an admin, with what it cost, mentioning whoever submitted it. Your own cancels aren't posted.
- **A GPU that sits idle** (under 5% use for 30 minutes by default) gets one warning in the channel. The job keeps
  running and billing: often the code isn't using the GPU, or it's stuck waiting for something. Check its output.
- **Machines at once.** A project can have 4 machines running, starting or outbid at once by default (an admin can
  change it for your project). More jobs wait in the queue until one ends. A stopped box doesn't count, but resuming
  one needs a free place.
- **If an admin stops all compute** (something looks wrong with spend or with Vast), your job's machine is
  destroyed and the job goes back in the queue, saying why. It carries on from its last checkpoint in storage once
  compute is back on, and the time it was stopped doesn't count against its max wait. A running box saves /workspace
  and stops; resume it later.

## Getting your results

- **On the web:** the job's **Files** section lists outputs, checkpoints and logs. A download link works for 15
  minutes after you click it.
- **From a terminal:** `krater download <job> results/` puts them under `results/outputs/`, `results/checkpoints/` and
  `results/logs/`. `--only outputs` fetches just one kind, and `--skip-existing` leaves alone files you already have
  (handy for logs, which only grow).

Who can see them: everyone on the team, and admins. Reviewers see the job but not its files or output.

Files stay in Krater's storage and count against the project's storage quota. Krater keeps no backup of them, so
download what you need to keep.

**After the project ends** (completed or withdrawn), its checkpoints, outputs, logs and workspaces are deleted 30 days
later by default. The exact date is posted in the project's channel when it finishes. Download everything you want to
keep before then: `krater download <job> results/` for each job.

## Boxes: working interactively

A box is a GPU machine you SSH into. Use it to explore data, debug, run a notebook, or try a command before you turn
it into a job.

### Starting one

1. Add your SSH public key under **SSH keys** if you haven't.
2. **New job or box**, choose **Box**, an image (plain `ubuntu:24.04` works, or your own), the GPU and a price.
   There's no command.
3. When it's **running**, the job page shows how to connect:

   ```bash
   ssh -p <port> root@<host>
   ```

   or `krater ssh <box>`. You log in as `root`.

### Your workspace

- **Keep your work in `/workspace`.** Krater saves it to storage every 10 minutes and before it stops the box, and
  restores it when the box starts on a new machine. Anything outside `/workspace` (installed packages, files in
  `/root`) is lost when the box moves or is destroyed. Install into `/workspace` (for example a virtual environment at
  `/workspace/venv`) or put it in your image.
- **Secrets** are in your login shell's environment (from `/etc/profile.d/krater-secrets.sh`).

### Idle stop

A box with no GPU or CPU use and no open SSH session for 30 minutes (by default) is **stopped**: Krater saves
`/workspace`, then stops the machine. The GPU stops billing; the disk still does, a little.

- **Resume box** starts it again on the same machine, with everything on its disk. This only works if that machine's
  GPU is still free.
- **Start it elsewhere from the saved workspace** rents a new machine and restores `/workspace`. Use it when resume
  can't get the GPU back.
- A box that stays stopped for 24 hours (by default) is **destroyed**, disk and all. Its saved `/workspace` is still
  in storage.

Krater posts in the project's Slack channel, mentioning you, when it stops your box for being idle and an hour before
it destroys a stopped one. A stop you asked for posts nothing.

A long-running process that keeps the GPU or CPU busy counts as use, so a training run in a box keeps it up. An open
SSH session counts too, so close your session when you're done, or the box keeps spending.

### Jupyter

Boxes are SSH only. Start Jupyter inside the box and tunnel to it:

```bash
# in the box
pip install jupyterlab
jupyter lab --no-browser --port 8888 --notebook-dir /workspace
```

```bash
# on your computer
ssh -L 8888:localhost:8888 -p <port> root@<host>
# or: krater ssh <box> -- -L 8888:localhost:8888
```

Then open `http://localhost:8888` and paste the token Jupyter printed.

### Admin access

Krater admins' SSH keys go on every box, next to yours, so they can help you or check on a box. The box's page names
them. An admin who logs in can read the project's secrets, as you can, and their open session counts as use.

## Secrets

When your code needs a token (Hugging Face, Weights & Biases, an API key), add it as a project secret rather than
putting it in your image or command:

- **On the web:** the project's **Compute** page, **Secrets**. Give it a name in capital letters, digits and
  underscores (`HF_TOKEN`) and a value. Saving the same name again replaces the value.
- **From a terminal:** `krater secrets set <project> HF_TOKEN`. It reads the value without echoing it, so it never
  lands in your shell history.

Every job and box of the project gets every secret as an environment variable. Up to 20 per project, 8 KB each.
Names starting with `KRATER_`, `VAST_` or `CONTAINER_`, and system names such as `PATH` and `HOME`, are refused.

Nobody can read a value back in Krater, but **the machine's host can see anything on the machine**, and so can
anyone with a shell on your box. Use tokens you can limit and revoke (a fine-grained, read-only Hugging Face token,
say), never your account password.

## From a terminal: the `krater` command

Everything above works from a terminal too, which is handy for scripts and sweeps.

### Install and sign in

It needs Python 3.10 or newer. With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"
```

or with pip (in a virtual environment, or with `pipx install` in place of `pip install`):

```bash
pip install "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"
```

Then sign in:

```bash
krater login
```

It signs in to `https://krater.patchworklabs.org`; for another Krater, add `--url` or set `KRATER_URL`. To update later, run the same install command again (with uv, add `--force`).

`krater login` prints a Weave link (and opens it). Sign in there, and Weave shows a code: paste it into the
terminal. It works over SSH too. You need to have signed in to Krater in a browser once before.

### A whole job, start to finish

```bash
krater projects                         # your projects, with short ids
krater prices --gpu 4090                # what an hour costs now

krater submit vision \
  --name train-v1 \
  --image ghcr.io/you/train:v1 \
  --command "cd /app && python train.py --epochs 3" \
  --gpu "RTX 4090" --gpu "RTX 3090" \
  --limit 0.35 \
  --max-runtime 240 \
  --data-in 20 --data-out 5

krater jobs vision                      # the project's jobs, newest first
krater logs train-v1 -f                 # follow the output until it finishes
krater download train-v1 results/       # outputs, checkpoints and logs
```

You don't type ids: a project is any part of its title (`vision`) or the first 4 or more characters of its id, and a
job is its name or the start of its id. If that matches more than one, the CLI lists them and stops.

### `krater submit` options

| Option | Meaning |
| --- | --- |
| `--name`, `--image` | Required |
| `--command` | What a job runs (not for a box) |
| `--box` | A box instead of a job |
| `--gpu MODEL` | A GPU model you accept; repeat it for several |
| `--min-vram GB` | Or any GPU with at least this much memory |
| `--gpus N` | GPUs on the machine (1 by default) |
| `--disk GB` | 32 by default |
| `--limit $/h` | A limit order |
| `--expires 12h` | Cancel a limit order that hasn't started within this long (`90m`, `12h`, `2d`) |
| `--target $/h --max $/h` | A target-and-max order |
| `--on-demand` | Rent on demand (needs `--target` and `--max`) |
| `--max-wait MIN`, `--on-max-wait` | How long to wait, then `keep_waiting`, `raise_bid`, `on_demand` or `cancel` |
| `--on-outbid move\|hold` | When outbid for a while: move, or hold the machine (limit orders hold) |
| `--max-runtime MIN` | 480 by default |
| `--data-in GB`, `--data-out GB` | Your data transfer estimate |
| `--confirm-transfer-cost` | Queue without asking, even if transfer could cost a large share of the budget |
| `--estimate` | Say what the job may cost and queue nothing |
| `-f job.toml` | Read the options from a file; the project and options typed here win ([how](#keep-a-jobs-options-in-a-file)) |
| `--sweep NAME=V1,V2`, `--count N` | Queue one job per value (and N of each), with `$NAME` and `$KRATER_SWEEP_INDEX` set ([how](#a-hyperparameter-sweep)) |

Leave the price out, and `krater submit` shows current prices for the GPUs you named, explains the options, and
queues nothing.

### Other commands

| Command | What it does |
| --- | --- |
| `krater show <job>` | State, machine and recent events |
| `krater rerun <job>` | Queues it again with the same settings (`--name` for a new name) |
| `krater logs <job>` | The end of the output. `-f` follows it; `--attempt 1` shows an earlier attempt |
| `krater wait <job>` | Blocks until the job is finished. Exits 0 if it succeeded, the command's own code if it failed with one, 124 on `--timeout` |
| `krater cancel <job>` | Cancels it; the machine is destroyed |
| `krater stop`, `resume`, `move <box>` | Stop a box, resume it on the same machine, or start it on another from its saved workspace |
| `krater ssh <box>` | SSH into a running box. Anything after `--` goes to `ssh` |
| `krater download <job> [folder]` | Outputs, checkpoints and logs |
| `krater open <job or project>` | The web page, in your browser |
| `krater secrets list\|set\|rm <project> [NAME]` | Project secrets |
| `krater whoami`, `krater logout` | Who you're signed in as; sign out |

Every command takes `--json` for scripts. More detail is in [compute/cli.md](../compute/cli.md).

## Recipes

### A hyperparameter sweep

`--sweep NAME=V1,V2` queues one job per value, and the command sees the value as `$NAME`. Give `--sweep` more than
once for every combination; `--count N` queues N copies of each (for seeds). Every job also gets
`$KRATER_SWEEP_INDEX` (0, 1, ...) and a name ending in `-<index>`:

```bash
krater submit vision --name sweep --image ghcr.io/you/train:v1 \
  --command 'cd /app && python train.py --lr $lr --seed $KRATER_SWEEP_INDEX' \
  --gpu "RTX 4090" --limit 0.35 --max-runtime 120 --sweep lr=1e-4,3e-4,1e-3
krater jobs vision
```

Use single quotes around the command so your own shell leaves `$lr` alone. Each job's command starts with
`export KRATER_SWEEP_INDEX=0 lr=1e-4;`, so its page shows exactly what it ran with. One submit queues at most 100
jobs, a sweep asks about a costly data transfer once for all of them, and `--estimate` shows the total. Each job holds
its expected cost while it runs (see below), so a big sweep can wait for budget. Start small.

### Keep a job's options in a file

`krater submit -f job.toml` reads the options from a TOML file, under their long names. Anything you also type on the
command line wins, so one file can serve several runs:

```toml
project = "vision"
name = "train"
image = "ghcr.io/you/train:v1"
command = "cd /app && python train.py --lr $lr"
gpu = ["RTX 4090", "RTX 3090"]
limit = 0.35
max-runtime = 120
data-out = 20

[sweep]
lr = [1e-4, 3e-4]
```

```bash
krater submit -f job.toml --max-runtime 30 --name smoke-test
```

### Run a job again

**Run again** on a job's page opens the form filled in from it, to queue as it is or change first.
`krater rerun <job>` queues it again with the same settings straight away; `--name` gives the new one another name.

### Run a script and act on the result

```bash
krater submit vision --name eval --image ghcr.io/you/train:v1 \
  --command "cd /app && python eval.py" --gpu "RTX 4090" --limit 0.35 --max-runtime 60
if krater wait eval --timeout 90; then
  krater download eval results/ --only outputs
else
  krater logs eval
fi
```

### Multi-GPU training on one machine

```bash
krater submit vision --name train-4x --image ghcr.io/you/train:v1 --gpus 4 --gpu "RTX 4090" --limit 1.40 \
  --command "cd /app && torchrun --nproc_per_node=4 train.py"
```

Only rank 0 should write checkpoints and outputs.

### Develop in a box, then run as a job

1. Start a box on a cheap GPU, clone your code into `/workspace`, and get it working.
2. Turn what you ran into a Dockerfile and push the image.
3. Cancel the box, and queue the real run as a job on the GPU it needs.

## Your budget and compute

- **Spend** is what Krater's machines have cost so far: the GPU while it runs, the disk from the moment it's rented,
  the image pull, and data transfer as the machine reports it. It's estimated live and corrected every hour from
  Vast's own charges. The project page shows
  spend and what's left.
- **A job needs at least an hour of budget to start**, and then holds the cost of its max runtime (or whatever is
  left) while it runs, so two jobs can't spend the same dollars. A short max runtime holds less.
- **Warnings** go to the project's Slack channel at 80% of the budget and again at 95%: save your work then.
- **The hard stop.** Just before spend would pass the budget, Krater stops and destroys every machine of the project.
  There's no grace period, so checkpoint before you get close.
- **Need more?** Start an **amendment** from the project page and ask for a new total. See
  [Using Krater](using-krater.md#asking-for-more-budget-amendments).

## Troubleshooting

| What you see | Why, and what to do |
| --- | --- |
| No **New job or box** button | You're not on the team, the project isn't approved, it has a funding hold, or compute isn't open yet. The project page says which |
| Queued, "waiting on verification" | A team member isn't verified in Weave yet. The page lists who |
| Queued for a long time | Nothing is free at your price. Check [price history](/pricing/history), accept more GPU models, raise the limit, or choose "accept offers up to my max price" |
| Queued, "Waiting for budget" | Not enough budget left for an hour at your price, or other running jobs hold it. Lower the max runtime, wait for them, or ask for more |
| Queued, "Waiting: the project is awaiting funding" | A funding hold. An admin clears it |
| Queued, a search quota message | Krater limits how often it searches Vast. It tries again automatically |
| Failed in `starting` | The image couldn't be pulled (wrong name, private registry), or it has no `sh` and none of curl, wget or Python. Try `docker run --rm <image> sh -c "command -v curl wget python3"` |
| Failed, exit code 125 | The runner couldn't create its directories. Check that the image's user can write to `/tmp` |
| Failed with your program's exit code | Your command failed. Read **Output**. Run the same command locally in the image |
| Failed, max runtime | It ran longer than **Stop after**. Raise it, and make sure the job resumes from checkpoints |
| Paused for a long time | Outbid. A limit order holds its machine until it wins again or another is free under the limit. Raise the limit or switch to target-and-max to move sooner |
| Restarted from the beginning after a move | Your program didn't load its checkpoint. Check `KRATER_RESUMED` and the path you load from |
| Checkpoint missing after a move | It was written after the last upload (every 10 minutes), or under a name ending in `.tmp`. Save more often, then rename |
| Box won't resume | Its old machine's GPU is taken. Use **Start it elsewhere from the saved workspace** |
| `ssh: Permission denied (publickey)` | Your key isn't on the box. Add it under **SSH keys**: a running box picks up key changes within a few minutes. Check you're connecting as `root` with the matching private key |
| Packages gone after a box restart | Only `/workspace` is kept. Install into `/workspace` or into your image |
| A warning about data transfer | Your estimate could cost a big share of your budget. Check the numbers before you confirm |
| All machines stopped at once | The budget ran out. Ask for more with an amendment |

If you're stuck, ask in your project's Slack channel: reviewers and admins are in it.

## Limits and defaults

Admins can change the defaults per project.

| What | Limit or default |
| --- | --- |
| GPUs per machine | 1 to 8, one machine per job |
| Disk | 8 to 2,000 GB; 32 by default |
| GPU memory filter | 1 to 256 GB |
| Command | Up to 2,000 characters |
| Wait for a price | Up to 7 days; 12 hours by default |
| Max runtime | Up to 7 days; 8 hours by default |
| Hourly cap per machine | $5 by default |
| Data transfer price cap | $20 per TB each way by default |
| Data transfer warning | 10% of the budget by default |
| Outbid: raise the bid after | 5 minutes by default |
| Outbid: move after (target and max) | 30 minutes by default |
| Box idle stop | 30 minutes by default |
| Stopped box destroyed after | 24 hours by default |
| Machine never starts | Failed after 30 minutes, or 3 bad hosts |
| Runner silent | Failed after 30 minutes by default |
| Machine stops itself without Krater | After 3 hours by default |
| Storage quota (checkpoints and outputs) | 50 GB per project by default |
| Log kept per attempt | 16 MB |
| Checkpoint upload | Every 10 minutes and at exit |
| Download links | 15 minutes |
| Secrets | 20 per project, 8 KB each |
