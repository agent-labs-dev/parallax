# Trusted-verify image: the checks open the shipped scene in the same Debian Blender the desktop box installs
# (python:3.13-slim is Debian trixie, as the box is), so a scene saved in the box reads back exactly. EGL and
# Mesa let `solution/build_scenes.py` render headless here.
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends blender libegl1 libgl1-mesa-dri \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
