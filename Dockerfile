# Forzamos la plataforma amd64 para compatibilidad con servidores estándar
FROM --platform=linux/amd64 python:3.11-slim

WORKDIR /app

# Optimizamos caché: copiamos requirements primero
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8000

# Usamos formato JSON array (exec form) que es el correcto
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]