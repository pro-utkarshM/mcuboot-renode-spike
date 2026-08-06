# syntax=docker/dockerfile:1
#
# This image deliberately builds its test firmware while the network is
# available.  The default runtime has all source, tools, signed images, and
# the Renode binary already present, so the proof runner needs no network.

FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea AS mcumgr-builder

ARG DEBIAN_FRONTEND=noninteractive
ARG MCUMGR_COMMIT=5c56bd24066c780aad5836429bfa2ecc4f9a944c

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git golang-go \
    && rm -rf /var/lib/apt/lists/*

RUN git init /tmp/mcumgr \
    && git -C /tmp/mcumgr remote add origin https://github.com/apache/mynewt-mcumgr-cli.git \
    && git -C /tmp/mcumgr fetch --depth=1 origin "${MCUMGR_COMMIT}" \
    && git -C /tmp/mcumgr checkout --detach "${MCUMGR_COMMIT}" \
    && test "$(git -C /tmp/mcumgr rev-parse HEAD)" = "${MCUMGR_COMMIT}" \
    && cd /tmp/mcumgr/mcumgr \
    && CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w' -o /usr/local/bin/mcumgr .

FROM antmicro/renode:1.16.1@sha256:fc2a8c1bad2296a6d7cbc852bbf5540b22b778bdeb0ad42a45b8c54ea1e6a24c

USER root

ARG DEBIAN_FRONTEND=noninteractive
ARG ZEPHYR_REVISION=684c9e8f32e4373a21098559f748f06915f950c9
ARG ZEPHYR_SDK_VERSION=1.0.1
ARG ZEPHYR_SDK_SHA256=21b85981cb5a1818d9bc53d82af80f208946ec038b982ff1907287572ed3a634
ARG ZEPHYR_HOSTTOOLS_SHA256=4d90f2d9a42802bc76bfaacd2bb52e6c30a046a425148e1ed1a7311cb66bde60
ARG RENODE_VERSION=1.16.1
ARG WEST_VERSION=1.5.0
ARG MCUMGR_COMMIT=5c56bd24066c780aad5836429bfa2ecc4f9a944c

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    ZEPHYR_BASE=/opt/zephyrproject/zephyr \
    ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk \
    ZEPHYR_TOOLCHAIN_VARIANT=zephyr \
    PATH=/opt/venv/bin:/opt/zephyr-sdk/arm-zephyr-eabi/bin:/opt/renode:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Antmicro's digest-pinned official image supplies the complete headless
# Renode runtime. Install only the upstream Zephyr build prerequisites.
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get -o Acquire::Retries=10 update \
    && apt-get -o Acquire::Retries=10 install -y --no-install-recommends \
       ca-certificates curl file git make ninja-build cmake ccache \
       python3 python3-venv python3-pip python3-dev \
       build-essential gperf device-tree-compiler xz-utils \
       libffi-dev libssl-dev \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "west==${WEST_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# SDK 1.0 ships its common host tooling separately from architecture-specific
# toolchains.  This downloads that common piece plus *only* arm-zephyr-eabi,
# rather than the otherwise multi-gigabyte all-toolchains SDK bundle.
RUN curl -fsSL -o /tmp/hosttools.tar.xz "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/hosttools_linux-x86_64.tar.xz" \
    && echo "${ZEPHYR_HOSTTOOLS_SHA256}  /tmp/hosttools.tar.xz" | sha256sum -c - \
    && curl -fsSL -o /tmp/arm-zephyr-eabi.tar.xz "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz" \
    && echo "${ZEPHYR_SDK_SHA256}  /tmp/arm-zephyr-eabi.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/zephyr-sdk \
    && tar -xJf /tmp/hosttools.tar.xz -C /opt/zephyr-sdk \
    && tar -xJf /tmp/arm-zephyr-eabi.tar.xz -C /opt/zephyr-sdk \
    && rm -f /tmp/hosttools.tar.xz /tmp/arm-zephyr-eabi.tar.xz \
    && test -x /opt/zephyr-sdk/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc

COPY west.yml /opt/zephyrproject/manifest/west.yml

# The minimal Zephyr manifest is fixed project-by-project. `west update` is
# intentionally confined to image construction; no proof target invokes it.
# Git history is discarded in this same layer after revision verification so it
# does not bloat the resulting image; sysbuild only needs the source tree.
RUN west init -l /opt/zephyrproject/manifest \
    && cd /opt/zephyrproject \
    && west update --narrow --fetch-opt=--depth=1 \
    && test "$(git -C zephyr rev-parse HEAD)" = "${ZEPHYR_REVISION}" \
    && find /opt/zephyrproject -type d -name .git -prune -exec rm -rf {} +

# The architecture archive above contains the compiler tree, but the SDK's
# minimal bundle supplies the CMake package metadata and installs the host
# tools. Merge that official bundle into the same path, then restore the
# legacy root symlink used by PATH via setup.sh's documented -o option.
ARG ZEPHYR_MINIMAL_SHA256=ca9bc0ff66fafca1dac9d592a36d953cf16d096a9d09b1c0357f021cf9f6a7eb
RUN curl -fsSL -o /tmp/zephyr-sdk-minimal.tar.xz \
       "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-x86_64_minimal.tar.xz" \
    && echo "${ZEPHYR_MINIMAL_SHA256}  /tmp/zephyr-sdk-minimal.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/zephyr-sdk-minimal /opt/zephyr-sdk/gnu \
    && tar -xJf /tmp/zephyr-sdk-minimal.tar.xz -C /tmp/zephyr-sdk-minimal --strip-components=1 \
    && cp -a /tmp/zephyr-sdk-minimal/. /opt/zephyr-sdk/ \
    && mv /opt/zephyr-sdk/arm-zephyr-eabi /opt/zephyr-sdk/gnu/arm-zephyr-eabi \
    && rm -f /opt/zephyr-sdk/zephyr-sdk-x86_64-hosttools-standalone-0.10.sh \
    && cd /opt/zephyr-sdk \
    && ./setup.sh -h -o \
    && cd / \
    && rm -rf /tmp/zephyr-sdk-minimal /tmp/zephyr-sdk-minimal.tar.xz \
    && test -f /opt/zephyr-sdk/cmake/Zephyr-sdkConfig.cmake \
    && test -x /opt/zephyr-sdk/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc

# Zephyr 4.4 requires Python >=3.12, while the official Renode 1.16.1 image is
# based on Ubuntu 22.04 and supplies 3.10. Build the current Python 3.12
# security release from its digest-pinned python.org source archive. Keeping
# this after the fetched Zephyr layer preserves those expensive caches.
# MCUboot's imgtool imports Python's lzma module while signing, so install the
# development header before compiling Python rather than accepting a silently
# reduced standard library.
RUN apt-get -o Acquire::Retries=10 update \
    && apt-get -o Acquire::Retries=10 install -y --no-install-recommends liblzma-dev \
    && rm -rf /var/lib/apt/lists/*

ARG PYTHON_VERSION=3.12.13
ARG PYTHON_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684
RUN curl -fsSL -o /tmp/Python.tar.xz \
       "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
    && echo "${PYTHON_SHA256}  /tmp/Python.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/python-src \
    && tar -xJf /tmp/Python.tar.xz -C /tmp/python-src --strip-components=1 \
    && cd /tmp/python-src \
    && ./configure --prefix=/opt/python-3.12 --with-ensurepip=install \
    && make -j16 \
    && make install \
    && cd / \
    && rm -rf /tmp/python-src /tmp/Python.tar.xz \
    && /opt/python-3.12/bin/python3 -c 'import lzma'
RUN rm -rf /opt/venv \
    && /opt/python-3.12/bin/python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
       "west==${WEST_VERSION}" \
       -r /opt/zephyrproject/zephyr/scripts/requirements-base.txt \
       -r /opt/zephyrproject/bootloader/mcuboot/scripts/requirements.txt \
    && /opt/venv/bin/python3 --version \
    && /opt/venv/bin/pip check

COPY --from=mcumgr-builder /usr/local/bin/mcumgr /usr/local/bin/mcumgr
COPY --chown=10001:10001 . /workspace/app

# `spike` is the only runtime user.  Initial firmware builds happen as this
# user too, which catches permissions that would fail in the restricted proof
# invocation.  The script seals v1, builds v2 and the verifier variants, and
# copies only genuine build products into fixtures/.
RUN groupadd --gid 10001 spike \
    && useradd --uid 10001 --gid spike --create-home --shell /bin/bash spike \
    && cd /opt/zephyrproject \
    && west config zephyr.base zephyr \
    && mkdir -p /workspace/app/artifacts /workspace/app/build /workspace/app/fixtures \
    && chown -R spike:spike /workspace/app

WORKDIR /workspace/app
USER spike
RUN scripts/build_firmware.sh

USER root
RUN renode --version \
    && mcumgr --help >/dev/null
USER spike

ENTRYPOINT ["/workspace/app/scripts/container-entrypoint.sh"]
CMD ["make", "proof"]
