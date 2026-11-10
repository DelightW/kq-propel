# KQ-Propel on Hugging Face Spaces (Docker SDK, free CPU Basic: 2 vCPU, 16 GB RAM).
#
# The Space holds only this file and README.md. The code is cloned from GitHub
# at build time, so updating the Space means pushing to GitHub and pressing
# "Factory rebuild" in the Space settings. Secrets (keys, the staff password
# hash, the Daraja callback token) are entered as Space secrets, never here.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# Spaces run the container as user 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/user/.cache/huggingface
WORKDIR /home/user

ARG REPO=https://github.com/DelightW/kq-propel.git
ARG BRANCH=priority-1-hardening
RUN git clone --depth 1 --branch "$BRANCH" "$REPO" app

WORKDIR /home/user/app/backend
# CPU-only PyTorch first: the default wheel bundles CUDA and is several GB.
RUN pip install --no-cache-dir --user torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir --user -r requirements.txt

# Download the embedding model during the build, so a cold start does not.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# Runtime files live outside the code checkout. On the free tier this folder
# is reset when the Space restarts; with paid persistent storage use /data.
ENV KQ_RUNTIME_DIR=/home/user/runtime \
    SECURE_COOKIES=true

EXPOSE 7860
# --proxy-headers: Spaces sit behind a proxy, and the staff lockout counts
# failed sign-ins per client address, which must be the real one.
CMD ["sh", "-c", "mkdir -p \"$KQ_RUNTIME_DIR\" && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 7860 --proxy-headers --forwarded-allow-ips='*'"]
