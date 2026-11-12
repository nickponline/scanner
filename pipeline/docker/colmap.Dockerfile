# SfM stage: COLMAP 4.2.1 (CUDA, ONNX for ALIKED/LightGlue) + pycolmap for DB/model IO
FROM colmap/colmap:20260929.8466
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
COPY pipeline/pyproject.toml /src/pipeline/pyproject.toml
COPY pipeline/scanner_pipeline /src/pipeline/scanner_pipeline
RUN pip install --no-cache-dir /src/pipeline pycolmap==4.2.1 scipy
# RoMa v2 dense matcher (optional SfM path, runs in a subprocess): torch + romav2; weights (RoMa v2 incl. its DINOv3
# backbone code via torch.hub) baked into the image so jobs don't depend on GitHub at run time
ENV TORCH_HOME=/opt/torch
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu126 \
 && pip install --no-cache-dir romav2==2.0.1 opencv-python-headless \
 && python -c "from romav2 import RoMaV2; RoMaV2(); print('romav2 ok')"
# ONNX Runtime's CUDA EP (ALIKED/LightGlue) needs cuDNN 9; COLMAP dlopens the unversioned libcudnn.so
RUN pip install --no-cache-dir "nvidia-cudnn-cu12>=9,<10" \
 && D=$(python -c "import nvidia.cudnn,os;print(os.path.join(nvidia.cudnn.__path__[0],'lib'))") \
 && for f in $D/libcudnn*.so.9; do ln -sf $f ${f%.9}; done && ln -s $D /opt/cudnn && ls /opt/cudnn
ENV LD_LIBRARY_PATH=/opt/cudnn:/usr/local/cuda/lib64
RUN python -c "import pycolmap, scipy.spatial.transform, scanner_pipeline.stages.sfm" && python -c "import torch, romav2, cv2, scanner_pipeline.roma_match" && colmap -h >/dev/null
# COLMAP fetches its ONNX models (ALIKED, LightGlue, LoMa) from GitHub releases on first use; GitHub 504s made jobs
# silently fall back to SIFT. Bake them into COLMAP's cache ($HOME/.cache/colmap/<sha256>-<name>) at build time.
RUN set -e; C=/root/.cache/colmap; mkdir -p $C; \
  for e in \
   "https://github.com/colmap/colmap/releases/download/3.13.0/aliked-n16rot.onnx 39c423d0a6f03d39ec89d3d1d61853765c2fb6a8b8381376c703e5758778a547" \
   "https://github.com/colmap/colmap/releases/download/3.13.0/aliked-lightglue.onnx b9a5de7204648b18a8cf5dcac819f9d30de1a5961ef03756803c8b86c2dceb8d" \
   "https://github.com/colmap/colmap/releases/download/3.13.0/sift-lightglue.onnx e0500228472b43f92b3d36881a09b3310d3b058b56187b246cc7b9ab6429096e" \
   "https://github.com/colmap/colmap/releases/download/3.13.0/bruteforce-matcher.onnx 3c1282f96d83f5ffc861a873298d08bbe5219f59af59223f5ceab5c41a182a47" \
   "https://github.com/davnords/storage/releases/download/loma/loma_detector.onnx b6af99c5e730034ac9b675d1ebe05d0679af4569a3c26f10a6a50f91e02dc512" \
   "https://github.com/davnords/storage/releases/download/loma/loma_descriptor_dedode_g.onnx 5a7b9eaf7425d4513c5d7feae86080bae7ed3aceae7fb1b9f059d0752e2ad564" \
   "https://github.com/davnords/storage/releases/download/loma/loma_matcher_G.onnx 62134cde779ed1031982c13c2c1952ed14856d715e97072364821a91f6276cf9" \
   "https://github.com/davnords/storage/releases/download/loma/loma_matcher_B.onnx ba5a2773b29cace19f1240e14e5a080cca3eaf9f69a7adb829a1d470557001c7"; \
  do set -- $e; f=$C/$2-$(basename $1); \
     curl -fsSL --retry 12 --retry-all-errors --retry-delay 5 -o $f $1; echo "$2  $f" | sha256sum -c -; done; ls -la $C
ENV PYTHONUNBUFFERED=1 SCANNER_WORKDIR=/work
ENTRYPOINT ["python", "-m", "scanner_pipeline"]
