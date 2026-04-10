FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run the scheduler which triggers etl.py nightly at 2am
CMD ["python", "scheduler.py"]
