FROM python:3.12-slim
WORKDIR /app
COPY database/requirements.txt /app/database/requirements.txt
RUN pip install --no-cache-dir -r database/requirements.txt
COPY database /app/database
CMD ["python", "-m", "database.import_data", "/data"]
