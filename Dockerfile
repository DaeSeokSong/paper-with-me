FROM python:3.12-slim

WORKDIR /srv/paper-with-me

COPY pyproject.toml README.md LICENSE ./
COPY pwc/ pwc/
COPY app/ app/
RUN pip install --no-cache-dir -e ".[web,stream,deploy]"

# HF Space는 UID 1000 사용자로 실행되므로 데이터 디렉터리를 쓰기 가능하게 둔다
RUN mkdir -p /data && chmod 777 /data
ENV PWC_DB=/data/pwc.sqlite \
    HF_HOME=/data/hf-cache

EXPOSE 8000

# PWC_DB가 없으면 PWC_DATA_REPO(HF Datasets)에서 스냅샷을 받아 시작한다.
# 로컬에서는 -v ./data:/data 로 직접 주입해도 된다.
CMD ["/bin/sh", "-c", "python -m app.bootstrap && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
