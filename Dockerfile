# 네이버 크롤링용 Playwright(크롬) 브라우저가 포함된 공식 이미지
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

# 의존성 먼저 설치 (캐시 활용) + 운영용 WSGI 서버 gunicorn
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 혹시 모를 브라우저 버전 불일치 방지
RUN playwright install chromium

COPY . .

# root로 실행 + DB 폴더 보장 (마운트 볼륨 /data 쓰기 권한)
USER root
RUN mkdir -p /data

# DB는 영구 디스크(/data)에 저장 (재배포해도 데이터 보존)
ENV DB_PATH=/data/justice.db
ENV PORT=8000
EXPOSE 8000

# 스케줄러가 1개만 돌도록 worker 1개 + 동시 요청용 thread 8개.
# 크롤링이 길어 timeout 넉넉히(120s). $PORT 확장을 위해 shell 형식 사용.
CMD gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-8000} app:app
