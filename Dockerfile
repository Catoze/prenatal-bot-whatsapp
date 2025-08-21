
# Dockerfile para rodar em produção (qualquer nuvem que aceite Docker)
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dependências do sistema (opcional, manter leve)
RUN apt-get update && apt-get install -y --no-install-recommends     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# Troque para requirements_production.txt se preferir incluir gunicorn aqui
RUN pip install --no-cache-dir -r requirements.txt gunicorn==22.0.0

COPY . /app

ENV PORT=8000
EXPOSE 8000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:${PORT}"]
