FROM python:3.11-slim

WORKDIR /app

# 只拷源码，不拷凭证
COPY riskguard.py comate2api.py start.py license_finder.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMATE_HOST=0.0.0.0 \
    COMATE_PORT=8181 \
    COMATE_DATA_DIR=/app/data

EXPOSE 8181

# 健康检查用官方 /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8181/health',timeout=3).status==200 else 1)"

CMD ["python", "start.py"]
