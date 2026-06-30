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

# 앱이 듣는 포트를 Railway가 감지하도록 EXPOSE. PORT가 주입되면 그 값을, 없으면 8080 사용.
EXPOSE 8080

# Flask 내장 서버로 직접 실행 (스케줄러 1개 + threaded).
CMD ["python", "app.py"]
