# CPU stages: ingest, frames, depth, lod, plan, publish
FROM rust:1.88-bookworm AS buildlod
RUN git clone --depth 1 --branch v2.4.0 https://github.com/sparkjsdev/spark.git /spark
WORKDIR /spark/rust/build-lod
RUN cargo build --release && cp $(find /spark/rust -path '*release/build-lod' -type f | head -1) /usr/local/bin/build-lod

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libegl1 libgomp1 libusb-1.0-0 libglib2.0-0 libx11-6 libxext6 libxrender1 unzip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sL -o /tmp/g.zip https://github.com/zeux/meshoptimizer/releases/download/v1.3/gltfpack-ubuntu.zip \
    && unzip -o /tmp/g.zip -d /usr/local/bin && chmod +x /usr/local/bin/gltfpack && rm /tmp/g.zip
COPY --from=buildlod /usr/local/bin/build-lod /usr/local/bin/build-lod
COPY pipeline/pyproject.toml /src/pipeline/pyproject.toml
COPY pipeline/scanner_pipeline /src/pipeline/scanner_pipeline
COPY viewer/scripts/plan_svg.py /src/pipeline/scanner_pipeline/plan_svg.py
RUN pip install --no-cache-dir "/src/pipeline[cpu]" google-cloud-secret-manager
# smoke test: every heavy import the CPU stages use (open3d needs libEGL even headless)
RUN python -c "import open3d, pycolmap, av, cv2, trimesh, shapely, manifold3d, mapbox_earcut, google.genai, sklearn, scipy; \
import scanner_pipeline.stages.depth, scanner_pipeline.stages.plan, scanner_pipeline.stages.publish, scanner_pipeline.plan_svg" \
 && test -x /usr/local/bin/build-lod && test -x /usr/local/bin/gltfpack
ENV PYTHONUNBUFFERED=1 SCANNER_WORKDIR=/work
ENTRYPOINT ["python", "-m", "scanner_pipeline"]
