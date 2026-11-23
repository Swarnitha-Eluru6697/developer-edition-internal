# Real, buildable image for ml-studio/backend and ml-studio/pipeline-runner
# — the SAME image, two different tags for two different real roles, not
# two near-duplicate Dockerfiles that could drift apart:
#   - ml-studio/backend    : the API server / standalone worker
#     (see k8s/worker-deployment.yaml)
#   - ml-studio/pipeline-runner : what Argo actually runs per pipeline
#     stage (see workflowEngine.ts's buildStageTemplate(), and
#     backend/src/cli/run-*-stage.ts — the real CLI entrypoints this
#     image needs dist/cli/*.js to exist for)
#
# Both roles need the exact same real runtime: Node to run the compiled
# TypeScript, Python + requirements-training.txt for every real trainer
# (classical_trainer.py, pytorch_trainer.py, timeseries_trainer.py,
# llm_finetune_trainer.py) the Node side spawns as a subprocess. A
# pipeline stage isn't a lighter-weight thing than the API/worker
# process — it calls the exact same real service modules
# (modelRegistration.ts, hpoWorker.ts, etc.) they do.
#
# For real GPU-backed stages (native PyTorch, LLM fine-tuning, HPO over
# either), use Dockerfile.cuda instead — a CUDA-capable base with the
# identical requirements-training.txt install, tagged separately (see
# k8s/llm-gpu-worker-deployment.yaml). This Dockerfile's plain Node base
# has no CUDA runtime; torch will genuinely run, but CPU-only.
#
# Build:
#   docker build -f Dockerfile -t ml-studio/backend:2.37.0 .
#   docker tag ml-studio/backend:2.37.0 ml-studio/pipeline-runner:2.37.0
# (or build+tag pipeline-runner directly with the same -f Dockerfile .)
#
# UNVERIFIED against a real docker build/push — no docker daemon or
# container registry in the environment this was written in, same
# disclosure standard as every other infra-adjacent artifact in this
# app (Argo, UCS, KServe). Structurally mirrors Dockerfile.cuda, which
# uses the identical apt/pip/npm/tsc recipe on a CUDA base instead of
# this plain one — that structure is the same one this file reuses.

# syntax=docker/dockerfile:1

# Placeholder for the optional local InaiEra_dataplane_GatewayAuth checkout.
FROM scratch AS gatewayauth

# Builds @aadhyaanalyticsinc/gateway-auth from that checkout (tag node-v1.1.0-rc.1,
# the version package.json pins) so no GitHub token is needed. With no checkout
# passed, /out stays empty and the main stage uses the token path (CI).
FROM node:22-bookworm-slim AS gatewayauth-node
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,from=gatewayauth,target=/gatewayauth \
    mkdir -p /out /src \
 && if [ -d /gatewayauth/.git ]; then \
      git config --global --add safe.directory '*' \
   && git -C /gatewayauth archive node-v1.1.0-rc.1 node | tar -x -C /src \
   && cd /src/node && npm ci --no-audit --no-fund && npm run build \
   && npm pack --pack-destination /out; \
    fi

FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
# libgomp1: the OpenMP runtime LightGBM (and XGBoost/CatBoost) load at import.
# The pip wheels don't bundle it and -slim doesn't ship it, so without it every
# LightGBM run fails with "libgomp.so.1: cannot open shared object file".
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ca-certificates git libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Real Python training dependencies — installed before the npm/tsc
# steps so a code-only change doesn't invalidate this (slow) layer.
COPY requirements-training.txt ./
# CPU-only torch first: on x86_64 the PyPI torch wheel pulls the NVIDIA CUDA libraries (several GB) that this
# CPU image never uses, and the extra space ran Docker Desktop's disk out mid-build on smaller machines. With torch
# already installed, the requirements below (torch>=2.2, and everything that depends on it) keep this one.
RUN pip3 install --no-cache-dir --break-system-packages --index-url https://download.pytorch.org/whl/cpu torch
RUN pip3 install --no-cache-dir --break-system-packages -r requirements-training.txt

# Kokoro (text-to-speech) tags English with spaCy's en_core_web_sm and, if it is missing, tries to
# `pip install` it on first use, which this system Python refuses (PEP 668): every Kokoro call failed.
# Installed here instead, in its own layer so the large layer above stays cached.
RUN pip3 install --no-cache-dir --break-system-packages \
      https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# LLM fine-tuning (training/llm_finetune_trainer.py) builds its train/eval split with HuggingFace `datasets`, which
# requirements-training.txt never listed: every LLM training run failed with "No module named 'datasets'".
# Its own layer, so the large dependency layer above stays cached.
RUN pip3 install --no-cache-dir --break-system-packages "datasets>=2.20"

# Real Node dependencies and real TypeScript compilation — npm run
# build (tsc -p .) is the exact same command verified locally to
# produce dist/cli/*.js from src/cli/*.ts; nothing different happens
# inside the image than what already runs in CI/local dev.
# @aadhyaanalyticsinc/gateway-auth: if the gatewayauth-node stage built it from a
# local checkout, install that tarball (no registry, no token). Otherwise it comes
# from GitHub Packages: .npmrc maps the scope to npm.pkg.github.com and the token
# arrives as the BuildKit secret `npm_token` (the CI workflow passes
# INAIERA_SOURCE_TOKEN). The token is only in this RUN's environment and never
# lands in a layer.
COPY package*.json .npmrc tsconfig.json ./
COPY --from=gatewayauth-node /out/ /tmp/gatewayauth/
RUN --mount=type=secret,id=npm_token \
    tgz="$(ls /tmp/gatewayauth/*.tgz 2>/dev/null | head -n1)"; \
    if [ -n "$tgz" ]; then \
      rm -f .npmrc \
   && node -e 'const f="package.json",p=require("./"+f);p.dependencies["@aadhyaanalyticsinc/gateway-auth"]="file:"+process.argv[1];require("fs").writeFileSync(f,JSON.stringify(p,null,2))' "$tgz" \
   && npm install --no-audit --no-fund; \
    else \
      NODE_AUTH_TOKEN="$(cat /run/secrets/npm_token)" npm ci; \
    fi
COPY src ./src
RUN npm run build && npm prune --omit=dev

# Non-TypeScript files the compiled code loads by __dirname. tsc emits only
# .js, so anything else has to be copied in beside it or the image starts and
# immediately fails on a file that exists in the repo but not in dist/:
#
#   training/*.py  — every trainer and runner the Node side spawns
#   db/schema.sql  — read by initSchema(), which server.ts awaits on BOOT.
#                    Missing, the container crashes with ENOENT before it
#                    listens; the repo looked fine because `npm run dev` runs
#                    from src/ where the file is right there.
RUN cp -r src/training dist/training \
 && cp src/db/schema.sql dist/db/schema.sql

EXPOSE 4000
CMD ["node", "dist/server.js"]
