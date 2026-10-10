# Run only when compute is cheap

How to make a Ganymede job run only while GPU prices stay under a number you choose, so your budget goes further. This
uses SkyPilot and Vast.ai's **interruptible** (spot) machines. It isn't a Krater feature: you set it in your SkyPilot
task file.

> **Status:** the pieces below come from SkyPilot's documentation and source. Interruptible Vast machines and automatic
> recovery haven't been tested end to end on Ganymede yet (they're on the staging checklist). Try it on a small budget
> first.

## The idea

- **Only start under your price:** `max_hourly_cost` makes SkyPilot pick only machines at or below it. It's checked
  when a machine is chosen, not afterwards.
- **Keep running only while the price stays under it:** Vast's interruptible machines are rented by **bidding**. If
  someone outbids you, your machine is interrupted, so you never pay more than your bid.
- **Carry on automatically:** launched as a **managed job**, SkyPilot relaunches after an interruption, again only on a
  machine under your `max_hourly_cost`.
- **Don't lose work:** interruptions stop your job mid-run, so it must save checkpoints and resume from them.

## Task file

```yaml
# cheap-train.yaml
resources:
  infra: vast
  accelerators: RTX4090:1
  use_spot: true           # interruptible (bid) machines
  max_hourly_cost: 0.40    # dollars per hour; only machines at or below this

setup: |
  pip install -r requirements.txt awscli

run: |
  # Resume from the latest checkpoint if one exists, save a new one regularly, and upload it (see below).
  python train.py --resume-from checkpoints/latest --checkpoint-every 15m \
    --on-checkpoint "aws --endpoint-url $S3_ENDPOINT s3 sync checkpoints/ s3://$CHECKPOINT_BUCKET/$PROJECT/"
```

Launch it as a managed job, targeting your project's workspace (the name is on your Krater project page):

```bash
sky jobs launch -w ganymede-<your-workspace> cheap-train.yaml
```

## Checkpoints: upload them yourself

SkyPilot can't mount storage buckets on Vast, so your job has to copy checkpoints out itself, for example with `aws s3
sync` or `rclone` to your own bucket, and download the latest one at startup. Keep the interval short enough that an
interruption only costs you a few minutes.

## What Krater adds on top

- **Price cap:** Krater caps every launch's hourly price at your project's limit (by default $5/hour per machine;
  an admin can set a different one per project, and the project page shows yours). A lower
  `max_hourly_cost` of yours is kept.
- **Autodown:** Krater forces autodown after 30 idle minutes.
- **Budget:** when your project's spend reaches its ceiling, launches are rejected and running machines are shut down.
  Cheap interruptible machines make the budget last longer, but it still applies.

## Things to know

- **Interruptions are normal.** With a low `max_hourly_cost`, expect your job to pause and resume, sometimes for a while
  if prices stay high. That's the trade-off for paying less.
- **Your own bid price:** by default SkyPilot bids the machine's minimum. Setting your own bid amount isn't documented
  for the SkyPilot version Ganymede runs yet; `max_hourly_cost` is the supported knob.
- **Single machine only:** Vast doesn't support multi-node clusters.
- **Checking prices:** there's no pricing page in Krater yet. `sky gpus list` works once you're signed in and targeting
  your project workspace.
