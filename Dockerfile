FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Amsterdam

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py homewizard_p1.py ./

RUN useradd --create-home --uid 1000 app
USER app

CMD ["python", "main.py"]
