# Densify stage: Depth Anything 3 (ByteDance-Seed), pose-conditioned multi-view depth -> dense point cloud.
# Note: DA3 giant / nested checkpoints are CC BY-NC 4.0 (non-commercial); DA3-BASE / DA3METRIC-LARGE are Apache 2.0.
FROM pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
# never let a dependency swap the base image's torch
RUN python -c "import torch,torchvision;print(f'torch=={torch.__version__.split(\"+\")[0]}\ntorchvision=={torchvision.__version__.split(\"+\")[0]}')" > /etc/pip-constraints.txt \
 && echo "pycolmap==4.2.1" >> /etc/pip-constraints.txt && cat /etc/pip-constraints.txt
ENV PIP_CONSTRAINT=/etc/pip-constraints.txt
ARG DA3_SHA=main
RUN pip install --no-cache-dir xformers==0.0.32.post2 && \
    pip install --no-cache-dir "git+https://github.com/ByteDance-Seed/Depth-Anything-3.git@${DA3_SHA}" addict
COPY pipeline/pyproject.toml /src/pipeline/pyproject.toml
COPY pipeline/scanner_pipeline /src/pipeline/scanner_pipeline
RUN pip install --no-cache-dir /src/pipeline scipy
# bake the weights into the image (runtime HF/GitHub outages made model downloads fail silently before)
ENV HF_HOME=/opt/hf
ARG DA3_MODEL=depth-anything/DA3NESTED-GIANT-LARGE-1.1
RUN python -c "from huggingface_hub import snapshot_download as d; d('${DA3_MODEL}')" && \
    python -c "from depth_anything_3.api import DepthAnything3 as M; m=M.from_pretrained('${DA3_MODEL}'); print(sum(p.numel() for p in m.parameters())/1e9,'B params')"
RUN python -c "import depth_anything_3.api, pycolmap, cv2, scipy, scanner_pipeline.stages.densify; print(pycolmap.__version__)"
ENV PYTHONUNBUFFERED=1 SCANNER_WORKDIR=/work HF_HUB_OFFLINE=1
ENTRYPOINT ["python", "-m", "scanner_pipeline"]
