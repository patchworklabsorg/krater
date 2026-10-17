# Run only when compute is cheap

How to make a Ganymede job wait for a GPU price you choose, so your budget goes further. Krater does the waiting and the
renting: you set a target price, and your job starts when a Vast.ai machine at or under it is available.

> **Status:** Krater Compute passed its first staging run on real Vast machines (2026-10-11) and is switched on in
> production separately. Try it on a small budget first.

## The idea

- **Start under your price.** A job waits in the queue until an offer's full hourly cost (GPU plus your disk) is at or
  under your **target price**. Krater never pays more than your **max price**, nor more than your project's hourly cap.
- **Interruptible by default.** Krater rents interruptible (bid) machines, which are usually much cheaper than on-demand
  ones. Someone can outbid you, which pauses your machine. Krater waits a few minutes, raises your bid in small steps up
  to your max price, and if the machine stays paused, moves your job to another one.
- **Don't lose work.** A pause or a move stops your program mid-run, so save checkpoints and resume from them.

## Choosing a price

[GPU price history](/pricing/history) shows each GPU's cheapest offer and its typical range over the last day, week or
month. A target at the cheapest price waits for the dips; one inside the typical range starts sooner. Set the max price
a little above the target, so a bid raise can keep your machine through a short spike.

If nothing meets your target for a while, you choose what happens after your **max wait**: keep waiting, accept offers
up to your max price, rent on demand up to your max price, or cancel.

## Checkpoints

Krater gives every job a checkpoint directory and tells it whether it's resuming:

| Variable | Meaning |
| --- | --- |
| `KRATER_CHECKPOINT_DIR` | Save checkpoints here. Krater restores the latest ones here before your command starts |
| `KRATER_RESUMED` | `1` when your job is resuming after a pause or a move: load your latest checkpoint |
| `KRATER_OUTPUT_DIR` | Put results here; uploaded when your command finishes |

Krater uploads changed checkpoint files every 10 minutes and when your command exits. Write each checkpoint under a
temporary name ending in `.tmp` and rename it when it's complete, so a half-written file is never uploaded. For example,
with PyTorch:

```python
import os, torch

ckpt_dir = os.environ.get("KRATER_CHECKPOINT_DIR", "checkpoints")
latest = os.path.join(ckpt_dir, "latest.pt")
if os.environ.get("KRATER_RESUMED") == "1" and os.path.exists(latest):
    state = torch.load(latest)
    model.load_state_dict(state["model"])
    optimizer.load_state_dict(state["optimizer"])
    start_step = state["step"]

# every few hundred steps:
torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(), "step": step}, latest + ".tmp")
os.replace(latest + ".tmp", latest)
```

Your project has a storage quota (50 GB unless an admin set another), so keep only the checkpoints you need.

## Secrets

If your job needs a token (Hugging Face, Weights & Biases), add it under **Secrets** on your project's Compute page.
Every job and box of the project gets it as an environment variable (`HF_TOKEN`, say); in a box, log-in shells
have it. Nobody can read a value back on the page, but the machine's host could, so use a token you can limit and
revoke, never your main account password.

## Submitting

On your project's page, open **Compute** and choose **New job or box**: your container image, the command, the GPU
models you accept (or a minimum amount of GPU memory), disk, target and max price, and a max runtime. Your image needs a
POSIX shell and one of curl, wget or Python. Its own entrypoint isn't run, so put any setup in the command.

## What Krater adds on top

- **Price cap:** no machine costs more than your project's hourly cap (by default $5/hour; an admin can set another).
- **Budget:** your job holds its expected cost while it runs, so two jobs can't spend the same dollars. When your
  project's spend reaches its ceiling, Krater stops its machines.
- **Clean-up:** a finished job's machine is destroyed within a minute, so nothing keeps billing.

## Things to know

- **Pauses are normal** with a low target. That's the trade-off for paying less. Choose on-demand for work that can't
  checkpoint.
- **Single machine only:** a job runs on one machine with up to 8 GPUs.
- **Interactive work:** use a box instead of a job. It's an SSH workspace that stops itself when idle and keeps
  `/workspace` between sessions. Add your SSH key under **SSH keys** first. For Jupyter, start it in the box and
  tunnel to it: `ssh -L 8888:localhost:8888 -p <port> root@<host>`, then open `http://localhost:8888`. When Krater
  stops your box for being idle, and an hour before a stopped box is destroyed, it tells you in the project's Slack
  channel.
