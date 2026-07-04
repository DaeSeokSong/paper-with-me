FROM python:3.12-slim

WORKDIR /srv/paper-with-me

# 의존성 레이어를 소스와 분리 — 코드 수정 때마다 전체 재설치를 피한다
COPY pyproject.toml LICENSE ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" "uvicorn>=0.29" "jinja2>=3.1" "huggingface_hub>=0.23"

COPY pwc/ pwc/
COPY app/ app/
RUN pip install --no-cache-dir --no-deps -e .

# HF Space는 UID 1000 사용자로 실행되므로 데이터 디렉터리를 쓰기 가능하게 둔다
RUN mkdir -p /data && chmod 777 /data
ENV PWC_DB=/data/pwc.sqlite \
    HF_HOME=/data/hf-cache \
    HOME=/data

EXPOSE 8000

# PWC_DB가 없거나 원격 스냅샷이 갱신되면 PWC_DATA_REPO(HF Datasets)에서
# 내려받아 시작한다. 로컬에서는 -v ./data:/data 로 직접 주입해도 된다.
# exec로 uvicorn을 PID에 올려 SIGTERM graceful shutdown을 보장한다.
CMD ["/bin/sh", "-c", "python -m app.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
