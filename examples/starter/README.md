# Krater starter

A small training job that does everything a Krater job should: it trains on a GPU, saves checkpoints, picks up where
it left off when a machine is outbid or moved, and saves its results. Copy it, swap in your own model and data, and
you have a job that's safe to run on cheap interruptible GPUs.

What it trains: a tiny character-level model on the Python standard library's own source code (already in the image,
so nothing to download). After 20 minutes on a cheap GPU it writes `sample.txt`, a page of the not-quite-Python it
learned to write. The whole run costs a few cents.

| File | What it is |
| --- | --- |
| `train.py` | The job. The four `KRATER` comments mark what to keep when you replace the model |
| `job.toml` | The job's options, for `krater submit -f job.toml` |
| `Dockerfile` | Only if you want your own image (below) |
| `.github/workflows/publish.yml` | Builds and publishes that image when you push a tag |

## Run it

You need an approved Krater project with a budget, the `krater` command
([install](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md#install-and-sign-in)),
and Docker on your computer for the free local try.

**1. Get the files.** `krater init my-job --project <your project>` writes them into a new folder `my-job`, with your
project already in `job.toml`. (Or copy this folder, and set `project` in `job.toml` yourself.)

**2. Try it on your own computer, free.** This runs the job in Docker as a Krater machine would, on your CPU:

```bash
cd my-job
krater run -f job.toml --smoke
```

You should see `step ... loss ...` lines, a saved checkpoint and `done`. The results are in
`krater-local/starter/outputs/`. The first time, Docker downloads the PyTorch image (about 3 GB).

**3. Run it on a GPU.** See the price first, then queue it:

```bash
krater submit -f job.toml --estimate
krater submit -f job.toml
krater logs starter -f          # follow it; Ctrl+C stops following, not the job
krater download starter results/ --only outputs
cat results/outputs/sample.txt
```

`krater submit` uploads this folder (a few KB) with the job; nothing to build or publish. The project page's
**Compute** tab shows the same job, if you'd rather watch it there.

## Make it your own

Keep the four habits marked `KRATER` in `train.py` and change the rest:

1. **Read the folders from the environment**: `$KRATER_CHECKPOINT_DIR`, `$KRATER_OUTPUT_DIR` (and
   `$KRATER_INPUT_DIR` for [project inputs](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md#inputs)).
2. **Save a checkpoint every few minutes**, to a temporary name and then renamed, so a stop never leaves half a file.
3. **Load the latest checkpoint when it's there.** Krater puts it back before your command starts, after an outbid
   pause or a move to another machine, with `KRATER_RESUMED=1`.
4. **Write results to `$KRATER_OUTPUT_DIR` and exit 0.** Anything else counts as failed.

Then:

- **Your dependencies:** list them in a `requirements.txt` next to `train.py`; the job installs it before it starts.
- **Your data:** upload it once with `krater inputs upload <project> data/`, add `input = ["data/"]` to `job.toml`,
  and read it from `$KRATER_INPUT_DIR/data/`. Keep data out of this folder: it's sent with every changed version
  (up to 100 MB).
- **Tokens (Hugging Face, Weights & Biases):** add them as project secrets on the Compute page; they arrive as
  environment variables. Never put them in your code.
- **A bigger GPU:** change `gpu` and `limit` in `job.toml`. `krater submit -f job.toml --gpu "RTX 4090"` (with no
  price) shows the current prices without queueing anything.

## Your own image (optional)

Uploading the folder is enough for most jobs. Build an image when installing your dependencies takes long enough to
matter on every start, or needs more than `pip`:

1. Put this folder in a GitHub repository of your own and add your dependencies to the `Dockerfile`.
2. `git tag v1 && git push origin v1`. The workflow builds `ghcr.io/<your GitHub name>/<repository>:v1` (Actions
   tab, about 5 minutes).
3. Make it public once: your profile, **Packages**, the package, **Package settings**, **Change visibility**. Vast
   hosts pull it without signing in.
4. In `job.toml`, remove `code`, and set `image` to it and `command = "cd /app && python train.py --minutes 20"`.

Tag each new version (`v2`, `v3` ...): Krater records the exact image each job ran, so a pinned tag tells you which
code made which result.

The full guide: [Running jobs and boxes](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md).
