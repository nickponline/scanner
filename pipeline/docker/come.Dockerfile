# Mesh stage: CoMe (ECCV 2026) — Inria non-commercial licence (research use only)
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-devel
ENV TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9" MAX_JOBS=10 DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential libgmp-dev libcgal-dev \
      libgl1 libegl1 libx11-6 libglib2.0-0 libgomp1 libusb-1.0-0 unzip curl && rm -rf /var/lib/apt/lists/*
RUN curl -sL -o /tmp/g.zip https://github.com/zeux/meshoptimizer/releases/download/v1.3/gltfpack-ubuntu.zip \
    && unzip -o /tmp/g.zip -d /usr/local/bin && chmod +x /usr/local/bin/gltfpack && rm /tmp/g.zip
ARG COME_SHA=main
RUN git clone https://github.com/r4dl/CoMe.git /opt/CoMe && cd /opt/CoMe && git checkout ${COME_SHA}
WORKDIR /opt/CoMe
# never let a dependency swap the base image's CUDA 12.1 torch (extensions below compile against it)
RUN python -c "import torch,torchvision;print(f'torch=={torch.__version__.split(\"+\")[0]}\ntorchvision=={torchvision.__version__.split(\"+\")[0]}')" > /etc/pip-constraints.txt && cat /etc/pip-constraints.txt
ENV PIP_CONSTRAINT=/etc/pip-constraints.txt
RUN pip install --no-cache-dir plyfile tqdm dacite open3d ninja GPUtil opencv-python-headless trimesh einops \
      scikit-image==0.24 nvidia-ml-py tensorboard scipy matplotlib imageio
# py3.11 rejects the unhashable dataclass defaults in CoMe's rasteriser -> default_factory
COPY pipeline/docker/patches/dataclass_defaults.py /tmp/dataclass_defaults.py
RUN python /tmp/dataclass_defaults.py submodules/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py \
 && (grep -rlE "^\s+\w+\s*:\s*[A-Z]\w*\s*=\s*[A-Z]\w*\(\)\s*$" --include=*.py . | xargs -r -n1 python /tmp/dataclass_defaults.py || true)
RUN pip install --no-cache-dir --no-build-isolation submodules/simple-knn/ && \
    pip install --no-cache-dir --no-build-isolation submodules/diff-gaussian-rasterization/ && \
    pip install --no-cache-dir --no-build-isolation submodules/decoupled-fused-ssim/ && \
    pip install --no-cache-dir --no-build-isolation "git+https://github.com/rahul-goel/fused-ssim/"
RUN cd submodules/tetra-triangulation && \
    cmake . -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && \
    CPATH=/usr/local/cuda/targets/x86_64-linux/include:$CPATH make -j10 && \
    pip install --no-cache-dir --no-build-isolation -e .
COPY pipeline/pyproject.toml /src/pipeline/pyproject.toml
COPY pipeline/scanner_pipeline /src/pipeline/scanner_pipeline
RUN pip install --no-cache-dir /src/pipeline
# CoMe's own deps if it ships a list (best effort; pinned torch/cuda deps already present)
RUN for f in requirements.txt; do [ -f /opt/CoMe/$f ] && grep -viE "^(torch|torchvision|torchaudio|submodules|-e|git\+)" /opt/CoMe/$f > /tmp/req.txt \
      && pip install --no-cache-dir -r /tmp/req.txt || true; done
# torch 2.4 has no torch.mps.is_available (only reached when CUDA is absent, e.g. this build-time check)
RUN F=$(python -c "import importlib.util;print(importlib.util.find_spec('decoupled_fused_ssim').origin)") \
 && sed -i 's/torch\.mps\.is_available()/(hasattr(torch.mps, "is_available") and torch.mps.is_available())/' $F
RUN python -c "import open3d, trimesh, scipy, cv2, torch, simple_knn, diff_gaussian_rasterization, scanner_pipeline.stages.mesh" && test -x /usr/local/bin/gltfpack \
 && cd /opt/CoMe && for s in train.py extract_mesh_tets.py; do \
      out=$(python $s --help 2>&1) || echo "$out" | grep -q "Found no NVIDIA driver" || { echo "$out" | tail -20; exit 1; }; done
ENV PYTHONUNBUFFERED=1 SCANNER_WORKDIR=/work COME_DIR=/opt/CoMe
WORKDIR /
ENTRYPOINT ["python", "-m", "scanner_pipeline"]
