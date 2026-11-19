# A Krater job image: a public PyTorch base plus your code, nothing else.
#
# The base is large, but it's the same one many people use, so Vast hosts often have it cached and only the small
# layer below is new. Krater needs `sh` and one of curl, wget or Python in the image; this base has all of them.
# CUDA 12.8 runs on every GPU Krater rents, the newest (RTX 50-series) included.
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

WORKDIR /app
# Add your own dependencies here, before the code, so changing the code doesn't reinstall them:
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
COPY train.py .

# No CMD or ENTRYPOINT: Krater doesn't run them. The job's command does the work, e.g.
#   cd /app && python train.py --minutes 20
