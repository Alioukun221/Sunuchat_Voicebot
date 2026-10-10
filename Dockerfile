# Image du serveur SunuChat (FastAPI + agent Gemini + RAG bge-m3 sur CPU).
# Copie AWS de l'image officielle Python : pas de limite de téléchargements Docker Hub.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# PyTorch en version CPU : le serveur n'a pas de carte graphique, et la version CUDA
# pèserait 4 Go de plus.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
COPY pyproject.toml uv.lock ./
RUN pip install uv==0.11.32 \
 && uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/req.txt \
 && TORCH_VERSION=$(sed -n 's/^torch==\([^ ;]*\).*/\1/p' /tmp/req.txt) \
 && grep -vE '^(torch|triton|nvidia-|cuda-)' /tmp/req.txt > /tmp/req-sans-torch.txt \
 && uv pip install --system --index-url "$TORCH_INDEX" "torch==$TORCH_VERSION" \
 && uv pip install --system -r /tmp/req-sans-torch.txt \
 && rm /tmp/req*.txt

COPY src ./src
COPY assets ./assets

EXPOSE 8000
# Un seul processus : les rappels (APScheduler) et l'état en mémoire ne se partagent pas.
CMD ["uvicorn", "sunuchat.main:app", "--host", "0.0.0.0", "--port", "8000"]
