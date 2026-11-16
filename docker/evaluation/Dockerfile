# 평가 전용 준비 레시피. 승인한 Linux base digest와 hash-locked wheelhouse를 별도 준비한다.
# 이 폴더만 build context로 사용하고 생성 코드·시험지·API 키를 이미지에 넣지 않는다.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
USER 0:0
RUN --network=none /usr/local/bin/python -I -B -c 'import re,sys; assert sys.platform == "linux"; assert re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", sys.argv[1])' "${BASE_IMAGE}"
RUN --network=none \
    --mount=type=bind,source=prepared/requirements.lock,target=/tmp/requirements.lock,readonly \
    --mount=type=bind,source=prepared/wheelhouse,target=/tmp/wheelhouse,readonly \
    /usr/local/bin/python -I -B -m pip --isolated install --no-index \
    --find-links=/tmp/wheelhouse --only-binary=:all: --require-hashes --no-cache-dir -r /tmp/requirements.lock
RUN --network=none /usr/local/bin/python -I -B -m pip --isolated check
RUN --network=none /usr/local/bin/python -I -B -c 'from importlib.metadata import version; [version(n) for n in ("fastapi","httpx","sqlalchemy","argon2-cffi")]'
COPY prepared/requirements.lock /opt/evaluation/requirements.lock
USER 10001:10001
WORKDIR /work
