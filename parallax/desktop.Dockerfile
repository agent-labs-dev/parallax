# Optional layer: ordinary boxes retain box.Dockerfile and their harness install line.
ARG BOX_IMAGE
FROM ${BOX_IMAGE}
USER root
RUN test "$(dpkg --print-architecture)" = amd64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11-utils openbox dbus at-spi2-core \
    python3-gi gir1.2-gtk-3.0 libgail-common libatk-bridge2.0-0 \
    libx11-6 libxi6 libxkbcommon0 libxcb1 libxext6 \
 && rm -rf /var/lib/apt/lists/*
# Published checksums.txt, cua-driver-rs-v0.28.3; binary-only archive, no installer side effects.
ADD https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.28.3/cua-driver-rs-0.28.3-linux-x86_64-binary.tar.gz /tmp/cua-driver.tar.gz
RUN echo '51de56e37e1e57cca613cff06e22a3c64bf8285ed97e518660f3d1cb2a4e6fcf  /tmp/cua-driver.tar.gz' | sha256sum -c - \
 && tar -xzf /tmp/cua-driver.tar.gz -C /usr/local/bin cua-driver \
 && chmod 755 /usr/local/bin/cua-driver \
 && rm /tmp/cua-driver.tar.gz \
 && cua-driver --version
USER agent
