# Smoke test: a CUDA image builds inside the runner (no GPU needed for building).
FROM docker.io/nvidia/cuda:13.4.3-base-ubuntu26.04@sha256:e2eb43e25d404e2c48716f80afdb8989a84c4da588f5509330322842084d97b0
RUN echo "built inside multica-agent-runtime" > /built.txt
CMD ["cat", "/built.txt"]
