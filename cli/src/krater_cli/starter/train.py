"""A small Krater training job to copy: it trains, checkpoints, resumes and saves its results the way Krater expects.

It trains a tiny character-level language model on the Python standard library's own source (every Python image has
it, so there's nothing to download) and, at the end, writes a sample of the "Python" it learned to write.

Replace the model and data with your own and keep the four Krater habits marked KRATER below:

1. read the folders from the environment (`KRATER_CHECKPOINT_DIR`, `KRATER_OUTPUT_DIR`);
2. save a checkpoint every few minutes, atomically (write a temporary file, then rename it);
3. load the latest checkpoint when there is one (`KRATER_RESUMED=1` says this run is a resume);
4. write results to `KRATER_OUTPUT_DIR` and exit 0 when done.

Run it anywhere: `python train.py --minutes 1` on a laptop (CPU is fine), `krater run` for a rehearsal in Docker, or
as a Krater job on a GPU.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import sys
import sysconfig
import time

import torch
import torch.nn.functional as F
from torch import nn

# KRATER 1: the folders. The defaults let the script run outside Krater too.
CHECKPOINT_DIR = os.environ.get("KRATER_CHECKPOINT_DIR", "./checkpoints")
OUTPUT_DIR = os.environ.get("KRATER_OUTPUT_DIR", "./outputs")
CHECKPOINT = os.path.join(CHECKPOINT_DIR, "latest.pt")
SMOKE = os.environ.get("KRATER_SMOKE") == "1"  # a smoke test stops after a few minutes: do a few steps and finish


def log(message: str) -> None:
    # Print progress often: it's what the job page and `krater logs` show. flush=True so it isn't held in a buffer.
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_text(max_bytes: int) -> bytes:
    """The standard library's .py files, joined. Swap this for your own dataset (from $KRATER_INPUT_DIR, say)."""
    stdlib = sysconfig.get_paths()["stdlib"]
    chunks, size = [], 0
    for path in sorted(glob.glob(os.path.join(stdlib, "**", "*.py"), recursive=True)):
        with open(path, "rb") as f:
            data = f.read()
        chunks.append(data)
        size += len(data)
        if size >= max_bytes:
            break
    return b"\n".join(chunks)[:max_bytes]


class TinyLM(nn.Module):
    """A small GRU over bytes: fast to train and small to checkpoint. Replace it with your model."""

    def __init__(self, hidden: int = 256, layers: int = 2):
        super().__init__()
        self.embed = nn.Embedding(256, hidden)
        self.rnn = nn.GRU(hidden, hidden, layers, batch_first=True)
        self.head = nn.Linear(hidden, 256)

    def forward(self, x, state=None):
        out, state = self.rnn(self.embed(x), state)
        return self.head(out), state


def save_checkpoint(model, optimizer, step: int, train_seconds: float, loss: float) -> None:
    # KRATER 2: write to a temporary name, then rename. A machine can stop at any moment; a rename is all or nothing,
    # so latest.pt is always a whole checkpoint, never half of one.
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    tmp = CHECKPOINT + ".tmp"
    torch.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "step": step,
            "train_seconds": train_seconds,
            "loss": loss,
        },
        tmp,
    )
    os.replace(tmp, CHECKPOINT)
    log(f"saved checkpoint at step {step}")


def sample(model, device, prompt: bytes = b"def ", length: int = 600) -> str:
    model.eval()
    with torch.no_grad():
        x = torch.tensor([list(prompt)], device=device)
        logits, state = model(x)
        out = list(prompt)
        for _ in range(length):
            probs = F.softmax(logits[0, -1] / 0.8, dim=-1)
            nxt = int(torch.multinomial(probs, 1))
            out.append(nxt)
            logits, state = model(torch.tensor([[nxt]], device=device), state)
    model.train()
    return bytes(out).decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=20, help="training time in total, across restarts")
    parser.add_argument("--save-every", type=float, default=5, help="minutes between checkpoints")
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--seq", type=int, default=256)
    parser.add_argument("--lr", type=float, default=2e-3)
    parser.add_argument("--data-mb", type=float, default=8)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"device {device}" + (f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else ""))
    torch.manual_seed(0)

    data = torch.tensor(list(load_text(int(args.data_mb * 2**20))), dtype=torch.long)
    split = int(len(data) * 0.95)
    train_data, val_data = data[:split], data[split:]
    log(f"data: {len(data):,} bytes")

    model = TinyLM().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    step, train_seconds, loss_value = 0, 0.0, float("nan")

    # KRATER 3: resume. Krater restores the checkpoint folder before the command starts, after a move to another
    # machine or a restart on the same one, and sets KRATER_RESUMED=1. Loading whenever the file is there also covers
    # running the script twice by hand.
    if os.path.exists(CHECKPOINT):
        ckpt = torch.load(CHECKPOINT, map_location=device)
        model.load_state_dict(ckpt["model"])
        optimizer.load_state_dict(ckpt["optimizer"])
        step, train_seconds, loss_value = ckpt["step"], ckpt["train_seconds"], ckpt["loss"]
        log(f"resumed from step {step} ({train_seconds / 60:.1f} min trained so far)")
    elif os.environ.get("KRATER_RESUMED") == "1":
        log("KRATER_RESUMED=1 but no checkpoint yet: starting from the beginning")

    # When Krater stops the command (you cancel the job, or a box ends) it sends SIGTERM first: save, then exit.
    stopping = False

    def on_sigterm(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_sigterm)

    budget = args.minutes * 60
    if SMOKE:
        budget = min(budget, train_seconds + 60)  # one minute of real steps proves the setup; the result can wait
    last_save = last_log = time.monotonic()
    start_seconds = train_seconds
    started = time.monotonic()

    while train_seconds < budget and not stopping:
        idx = torch.randint(0, len(train_data) - args.seq - 1, (args.batch,))
        x = torch.stack([train_data[i : i + args.seq] for i in idx]).to(device)
        y = torch.stack([train_data[i + 1 : i + args.seq + 1] for i in idx]).to(device)
        logits, _ = model(x)
        loss = F.cross_entropy(logits.reshape(-1, 256), y.reshape(-1))
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
        step += 1
        loss_value = loss.item()
        train_seconds = start_seconds + (time.monotonic() - started)

        if step % 100 == 0 or time.monotonic() - last_log >= 30:  # every 100 steps, and at least every 30 s
            last_log = time.monotonic()
            log(f"step {step} loss {loss_value:.3f} trained {train_seconds / 60:.1f}/{args.minutes:g} min")
        if time.monotonic() - last_save >= args.save_every * 60:
            save_checkpoint(model, optimizer, step, train_seconds, loss_value)
            last_save = time.monotonic()

    save_checkpoint(model, optimizer, step, train_seconds, loss_value)
    if stopping:
        log("stopped by Krater: checkpoint saved, exiting")
        return 143  # 128 + SIGTERM, so the job doesn't count as succeeded

    # KRATER 4: results go to KRATER_OUTPUT_DIR, uploaded when the command exits. Exit 0 means "succeeded".
    with torch.no_grad():
        n = min(len(val_data) - 1, 64 * 1024)
        logits, _ = model(val_data[:n].unsqueeze(0).to(device))
        val_loss = F.cross_entropy(logits[0], val_data[1 : n + 1].to(device)).item()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    torch.save(model.state_dict(), os.path.join(OUTPUT_DIR, "model.pt"))
    with open(os.path.join(OUTPUT_DIR, "metrics.json"), "w") as f:
        json.dump(
            {
                "steps": step,
                "train_minutes": round(train_seconds / 60, 2),
                "loss": loss_value,
                "val_loss": val_loss,
                "device": device,
            },
            f,
            indent=2,
        )
    with open(os.path.join(OUTPUT_DIR, "sample.txt"), "w") as f:
        f.write(sample(model, device))
    log(f"done: {step} steps, val loss {val_loss:.3f}; results in {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
