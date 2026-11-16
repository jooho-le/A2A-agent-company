# SCN-001 실험용 샌드박스 베이스(운영자 준비 단계). Chromium headless shell 실행 라이브러리만 더한다.
# 이 베이스 위에 dev의 docker/sandbox/Dockerfile이 hash-locked wheel과 Playwright 브라우저를 네트워크 없이 설치한다.
# 패키지 목록(PACKAGES)은 evaluation.environment의 DRIVER_PACKAGES(Playwright 드라이버용 libatomic1)와 CHROMIUM_PACKAGES
# (Playwright가 Debian 13에서 Chromium에 요구하는 직접 라이브러리 21개 + 기본 글꼴)이다. 빌드 뒤 실제 브라우저 실행 파일을
# ldd로 확인해 빠진 라이브러리가 없어야 다음 단계로 간다.
# 생성 코드·시험지·API 키를 넣지 않는다. Python·pip·/usr/local/bin/python과 기존 ENV는 고정 Python 베이스 그대로다.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG PACKAGES
RUN test -n "${PACKAGES}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ${PACKAGES} \
    && rm -rf /var/lib/apt/lists/*
