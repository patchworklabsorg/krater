# Your first job

From nothing to a finished training run on a rented GPU, using Krater's starter: a small job that already does what
Krater needs. Allow about 20 minutes, most of it waiting; the GPU time costs a few cents. You don't need to build or
publish an image.

When this works, [Running jobs and boxes](compute-jobs.md) has everything else: prices, boxes, inputs, secrets.

## What you need

- **An approved project with a budget**, and everyone on its team verified in Weave. The project page says if
  anything is missing. (New here? [Using Krater](using-krater.md) covers applying.)
- **The `krater` command**, signed in (Python 3.10 or newer; details in
  [Install and sign in](compute-jobs.md#install-and-sign-in)):

  ```bash
  uv tool install "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"
  krater login
  ```

- **Docker** on your computer (Docker Desktop is fine), to try the job for free before renting a GPU. You can skip
  that step without it.

## 1. Get the starter

```bash
krater init my-job --project <your project>
cd my-job
```

That writes the [starter](https://github.com/patchworklabsorg/krater/tree/main/examples/starter) into `my-job`:

| File | What it does |
| --- | --- |
| `train.py` | Trains a tiny model, saves a checkpoint every 5 minutes, resumes from it, writes results |
| `job.toml` | The job's options: this folder as its code, the command, the GPU, the price |
| `README.md` | How to make it your own |
| `Dockerfile` | Only for when you want your own image ([later](compute-jobs.md#preparing-your-image)) |

## 2. Try it free, on your computer

```bash
krater run -f job.toml --smoke
```

This runs the job in Docker as a Krater machine would, for a minute on your CPU. The first time, Docker downloads
the PyTorch image (about 3 GB). Look for `step ... loss ...` lines, `saved checkpoint` and `done`. If it fails here,
it would have failed on the GPU too, so fix it now.

## 3. Run it on a GPU

```bash
krater submit -f job.toml --estimate     # what it may cost; queues nothing
krater submit -f job.toml                # queue it
krater logs starter -f                   # watch it (Ctrl+C stops watching, not the job)
```

`krater submit` uploads the folder with the job (`code = "."` in `job.toml`), and the command runs inside it on
Krater's PyTorch image. It asks for a cheap GPU at up to $0.15 an hour, as a **limit order**: Krater waits for a
machine at that price or less, which usually takes a few minutes. The job page (your project, **Compute**) shows the
same thing: the state, the machine, the log and the spend.

If the machine is outbid while it runs, the job **pauses** and carries on from its last checkpoint. That's normal,
and it's why the starter saves every 5 minutes.

## 4. Get the results

```bash
krater download starter results/ --only outputs
```

`results/outputs/` has `model.pt`, `metrics.json` and `sample.txt` (what the model learned to write). On the web
they're under **Files** on the job page.

## Now make it yours

Replace the model and data in `train.py` and keep the four habits its `KRATER` comments mark:

1. Read the folders from `$KRATER_CHECKPOINT_DIR` and `$KRATER_OUTPUT_DIR`.
2. Save a checkpoint every few minutes, to a temporary name and then renamed.
3. Load the latest checkpoint whenever it's there.
4. Write results to `$KRATER_OUTPUT_DIR` and exit 0.

List your packages in a `requirements.txt` next to `train.py`: the job installs it before your command. Keep data out
of the folder (it's sent again whenever the code changes, and it's limited to 100 MB); upload it once instead.

Next steps, in the full guide:

- [Your data](compute-jobs.md#inputs): upload it once, and every job copies it in.
- [Tokens and keys](compute-jobs.md#secrets) such as `HF_TOKEN`, without putting them in your code.
- [Choosing a price](compute-jobs.md#choosing-a-price) for a bigger GPU, and [Run when it's cheap](run-when-cheap.md).
- [Your own image](compute-jobs.md#preparing-your-image), when installing packages on every start gets slow.
- [A box](compute-jobs.md#boxes-working-interactively), to work on a GPU machine over SSH while you develop.
- [Troubleshooting](compute-jobs.md#troubleshooting), when something doesn't start.
