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
| `Dockerfile` | A public PyTorch base plus `train.py` |
| `job.toml` | The job's options, for `krater submit -f job.toml` |
| `.github/workflows/publish.yml` | Builds and publishes the image when you push a tag |

## Run it

You need an approved Krater project with a budget, and the `krater` command
([install](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md#install-and-sign-in)).

**1. Make it yours.** Copy this folder into a new GitHub repository of your own (or use the template, if your team
has one). In `job.toml`, set `project` to your project's name and `image` to
`ghcr.io/<your GitHub name>/<your repository>:v1`, in lower case.

**2. Publish the image.** Push a tag and GitHub builds it for you (Actions tab; about 5 minutes):

```bash
git tag v1 && git push origin v1
```

Then make it public once: on GitHub, your profile, **Packages**, the package, **Package settings**, **Change
visibility** to public. Vast hosts pull it without signing in.

(Or build it yourself: `docker build -t ghcr.io/you/krater-starter:v1 . && docker push ghcr.io/you/krater-starter:v1`.)

**3. Try it on your own computer, free.** This runs the job in Docker as a Krater machine would, on your CPU:

```bash
krater run -f job.toml --smoke
```

You should see `step ... loss ...` lines, a saved checkpoint and `done`. The results are in
`krater-local/starter/outputs/`.

**4. Run it on a GPU.** See the price first, then queue it:

```bash
krater submit -f job.toml --estimate
krater submit -f job.toml
krater logs starter -f          # follow it; Ctrl+C stops following, not the job
krater download starter results/ --only outputs
cat results/outputs/sample.txt
```

The project page's **Compute** tab shows the same job, if you'd rather watch it there.

## Make it your own

Keep the four habits marked `KRATER` in `train.py` and change the rest:

1. **Read the folders from the environment**: `$KRATER_CHECKPOINT_DIR`, `$KRATER_OUTPUT_DIR` (and
   `$KRATER_INPUT_DIR` for [project inputs](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md#inputs)).
2. **Save a checkpoint every few minutes**, to a temporary name and then renamed, so a stop never leaves half a file.
3. **Load the latest checkpoint when it's there.** Krater puts it back before your command starts, after an outbid
   pause or a move to another machine, with `KRATER_RESUMED=1`.
4. **Write results to `$KRATER_OUTPUT_DIR` and exit 0.** Anything else counts as failed.

Then:

- **Your dependencies:** uncomment the `requirements.txt` lines in the `Dockerfile`.
- **Your data:** upload it once with `krater inputs upload <project> data/`, add `input = ["data/"]` to `job.toml`,
  and read it from `$KRATER_INPUT_DIR/data/`.
- **Tokens (Hugging Face, Weights & Biases):** add them as project secrets on the Compute page; they arrive as
  environment variables. Never put them in the image.
- **A bigger GPU:** change `gpu` and `limit` in `job.toml`. `krater submit -f job.toml --gpu "RTX 4090"` (with no
  price) shows the current prices without queueing anything.
- **Each new version:** tag it (`v2`, `v3` ...) and change `image` to match. Krater records the exact image each job
  ran, so a pinned tag tells you which code made which result.

The full guide: [Running jobs and boxes](https://github.com/patchworklabsorg/krater/blob/main/docs/guides/compute-jobs.md).
