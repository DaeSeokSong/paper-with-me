FROM python:3.12-slim

WORKDIR /srv/paper-with-me

COPY pyproject.toml README.md ./
COPY pwc/ pwc/
COPY app/ app/
RUN pip install --no-cache-dir -e ".[web,stream]"

# 데이터는 볼륨으로 주입한다: -v ./data:/data (pwc.sqlite 포함)
ENV PWC_DB=/data/pwc.sqlite
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
