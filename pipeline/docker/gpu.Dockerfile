# Splat stage: gsplat 1.5.3 (MCMC) + fused-ssim, compiled for A100 (8.0), A10/L4 (8.6/8.9); Spirula CLI as an alternative engine
FROM ubuntu:24.04 AS spirula
ARG SPIRULA_VERSION=2026.10.30
ARG SPIRULA_SHA256=19e9fdabdfa73d26cabd7e078ea6b12f2f8b976e457d21b83d6a30e4921ad5a2
RUN apt-get update && apt-get install -y --no-install-recommends libvulkan1 libgomp1 curl unzip ca-certificates \
        libopengl0 libglx0 libgl1 libegl1 libgles2 libx11-6 libx11-xcb1 libxext6 libxrandr2 libxi6 libxcursor1 \
        libxinerama1 libxrender1 libxxf86vm1 libwayland-client0 libwayland-cursor0 libwayland-egl1 libxkbcommon0 libdecor-0-0 \
    && curl -fsSL -o /tmp/s.zip "https://github.com/harry7557558/spirula-studio/releases/download/v${SPIRULA_VERSION}/spirula-${SPIRULA_VERSION}-ubuntu-vulkan-x86_64.zip" \
    && echo "${SPIRULA_SHA256}  /tmp/s.zip" | sha256sum -c - \
    && mkdir -p /opt/spirula/lib && unzip -q /tmp/s.zip -d /opt/spirula && chmod +x /opt/spirula/spirula \
    && ! ldd /opt/spirula/spirula | grep "not found" \
    && ldd /opt/spirula/spirula | awk '/=> \//{print $3}' | xargs -I{} cp -L {} /opt/spirula/lib/ \
    && cd /usr/lib/x86_64-linux-gnu && cp -L ld-linux-x86-64.so.2 libvulkan.so.1 libgomp.so.1 libpthread.so.0 libdl.so.2 librt.so.1 /opt/spirula/lib/ \
    && printf '#!/bin/sh\nexec /opt/spirula/lib/ld-linux-x86-64.so.2 --library-path /opt/spirula/lib:/usr/local/nvidia/lib64 /opt/spirula/spirula "$@"\n' > /opt/spirula/spirula-run \
    && chmod +x /opt/spirula/spirula-run && ls /opt/spirula/lib

FROM pytorch/pytorch:2.8.0-cuda12.6-cudnn9-devel
ENV TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9" MAX_JOBS=10 DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ninja && \
    pip install --no-cache-dir --no-build-isolation "git+https://github.com/nerfstudio-project/gsplat.git@v1.5.3" && \
    pip install --no-cache-dir --no-build-isolation "git+https://github.com/rahul-goel/fused-ssim.git"
# Spirula Studio CLI (GPL-3.0, run as a separate program) for splat.engine="spirula". The release binary needs
# glibc 2.38 / GLIBCXX_3.4.32 (Ubuntu 24.04) but this base is 22.04, so it runs on a private 24.04 runtime through that
# runtime's own dynamic loader (spirula-run). Vulkan compute: the 24.04 loader + an NVIDIA ICD manifest; the driver
# library (EGL entry point: the GLX one refuses vkCreateInstance headless) is mounted by Batch's installGpuDrivers into /usr/local/nvidia/lib64.
COPY --from=spirula /opt/spirula /opt/spirula
RUN mkdir -p /etc/vulkan/icd.d \
    && printf '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_nvidia.so.0","api_version":"1.3"}}' > /etc/vulkan/icd.d/nvidia_icd.json \
    && (/opt/spirula/spirula-run train 3dgs --help 2>&1 | head -3) && /opt/spirula/spirula-run train 3dgs --help 2>&1 | grep -q "usage:"
ENV SPIRULA_BIN=/opt/spirula/spirula-run NVIDIA_DRIVER_CAPABILITIES=all LD_LIBRARY_PATH=/usr/local/nvidia/lib64:${LD_LIBRARY_PATH}
COPY pipeline/pyproject.toml /src/pipeline/pyproject.toml
COPY pipeline/scanner_pipeline /src/pipeline/scanner_pipeline
RUN pip install --no-cache-dir /src/pipeline pycolmap==4.2.1 opencv-python-headless scipy
RUN python -c "import gsplat, gsplat.strategy, fused_ssim, pycolmap, cv2, scipy, scanner_pipeline.stages.splat, scanner_pipeline.stages.splat_spirula"
ENV PYTHONUNBUFFERED=1 SCANNER_WORKDIR=/work
ENTRYPOINT ["python", "-m", "scanner_pipeline"]
