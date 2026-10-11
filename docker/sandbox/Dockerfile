# Operator-owned PREPARATION recipe, never a generated Source build.
# Requires BuildKit and explicitly approved, locally prepared dependencies.
# No default base/version. Do not build from the project root.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
USER 0:0

# Fail closed for a mutable/non-Linux base. FROM may resolve before this guard:
# the operator must approve/pin BASE_IMAGE BEFORE invoking Docker build.
RUN --network=none /usr/local/bin/python -I -B -c 'import re,sys; assert sys.platform == "linux"; assert re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", sys.argv[1])' "${BASE_IMAGE}"

# Existing base supplies approved Chromium OS libraries and Python+pip.
# Install only pre-reviewed pinned, hashed binary wheels, with no build network.
RUN --network=none \
    --mount=type=bind,source=prepared/requirements.lock,target=/tmp/requirements.lock,readonly \
    --mount=type=bind,source=prepared/wheelhouse,target=/tmp/wheelhouse,readonly \
    /usr/local/bin/python -I -B -m pip --isolated install --no-index \
        --find-links=/tmp/wheelhouse --only-binary=:all: --require-hashes \
        --no-cache-dir -r /tmp/requirements.lock
RUN --network=none /usr/local/bin/python -I -B -m pip --isolated check
RUN --network=none /usr/local/bin/python -I -B -c 'from importlib.metadata import version; version("playwright"); version("bandit")'

# Browser assets MUST match this exact Playwright package and Linux architecture.
# Preparation/downloads happen separately with operator approval, not in a Tool.
COPY prepared/requirements.lock /opt/owned-runtime/requirements.lock
COPY prepared/ms-playwright/ /ms-playwright/
# Do not add ENV PLAYWRIGHT_BROWSERS_PATH: only browser Tool injects that ENV.
# No generated source, test policy, API keys, Host sockets or runtime runner here.
USER 10001:10001
WORKDIR /work
