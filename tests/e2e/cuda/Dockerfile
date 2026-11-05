# Smoke test: a CUDA image builds inside the runner (no GPU needed for building).
FROM docker.io/nvidia/cuda:13.4.3-base-ubuntu24.04@sha256:07adb6792c59025f2c45bfb1a3f1d1148bf6d48bf2326d604a6b2e24c4f04d6f
RUN echo "built inside multica-agent-runtime" > /built.txt
CMD ["cat", "/built.txt"]
