FROM python:3.10

COPY . /app

WORKDIR /app

RUN pip install -r requirements.txt

# parbake the analysis cache so a cold start doesn't have to rebuild it
RUN python factoids.py

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--preload", "app:app"]
