# parallax-box-<harness>:<version>-<sha8>-hyprland - a desktop box whose session is deskpilot's standalone Hyprland:
# its own DRM backend on the host's vkms display, rendered by Mesa's software rasteriser. Run with the vkms node
# passed in (`--device /dev/dri/cardN`, after `modprobe vkms` on the host); box.py does that. The recipe follows
# deskpilot's container/Dockerfile: every package from one Arch Linux Archive snapshot, so Hyprland and the headers
# the plugin builds against match.
# Build context: a directory holding one parallax wheel and px_clock.c (box.build_with_wheel).
FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899

ARG ARCH_SNAPSHOT=2026/11/12
# pacman moves to the second server, Arch's own archive mirror, when the archive stalls.
RUN printf 'Server = https://%s/repos/%s/$repo/os/$arch\n' \
      archive.archlinux.org "${ARCH_SNAPSHOT}" umea.archive.pkgbuild.com "${ARCH_SNAPSHOT}" > /etc/pacman.d/mirrorlist \
 && pacman -Syyuu --noconfirm --needed \
    hyprland mesa grim wf-recorder wl-clipboard at-spi2-core dbus glib2 \
    gcc make pkgconf wayland libxkbcommon cairo pixman libdrm libxcb \
    python uv zenity ttf-dejavu git ca-certificates nodejs npm \
 && pacman -Scc --noconfirm
# agent: runs harnesses in boxes, and Hyprland (which refuses root). /run/user/1000 is its runtime directory.
RUN useradd --create-home --home-dir /home/agent --uid 1000 --shell /bin/sh agent \
 && install -d -m 700 -o agent -g agent /run/user/1000 \
 && mkdir /workspace && chown agent:agent /workspace \
 && mkdir /etc/parallax && echo 0 > /etc/parallax/clock
# px-clock runs box processes on the world's clock (see px_clock.c).
COPY px_clock.c /tmp/px_clock.c
RUN gcc -O2 -Wall -Werror -o /usr/local/bin/px-clock /tmp/px_clock.c && rm /tmp/px_clock.c

# The task's own packages (`metadata.parallax.desktop.packages`, Arch Linux names), above the shared layer. Names are
# validated as package names in `models.Desktop`, so splitting them is safe.
ARG PACKAGES=""
RUN if [ -n "$PACKAGES" ]; then pacman -S --noconfirm --needed $PACKAGES && pacman -Scc --noconfirm; fi

# The install line comes from the harness manifest (trusted repository data), so it is a shell RUN. It precedes the
# wheels so a parallax source change reuses this layer from the build cache. npm 12 blocks install scripts, so a
# manifest whose package needs one names it with `--allow-scripts` on its own line.
ARG HARNESS_INSTALL=""
RUN if [ -n "$HARNESS_INSTALL" ]; then sh -c "$HARNESS_INSTALL"; fi
ARG DESKPILOT_REF
RUN uv pip install --system --break-system-packages "deskpilot @ git+https://github.com/agent-labs-dev/deskpilot@${DESKPILOT_REF}"
# In-box workers (`python -m parallax.desktop`, `python -m parallax.world bridge`) are parallax code.
COPY *.whl /tmp/wheels/
RUN uv pip install --system --break-system-packages /tmp/wheels/*.whl && rm -rf /tmp/wheels

USER agent
ENV HOME=/home/agent PYTHONDONTWRITEBYTECODE=1 \
    DESKPILOT_STANDALONE=1 DESKPILOT_AGENT=agent XDG_RUNTIME_DIR=/run/user/1000
# The plugin and the session's input client, built once against this image's Hyprland.
RUN deskpilot plugin build
WORKDIR /workspace
