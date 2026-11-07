FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

EXPOSE 8000

# Forma "exec" (lista): python queda como PID 1 y recibe el SIGTERM
# de "docker compose stop" directamente, sin un shell en el medio.
CMD ["python", "src/main.py"]
