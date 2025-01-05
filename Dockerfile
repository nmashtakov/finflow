FROM apache/airflow:2.9.2
COPY requirements.txt /requirements.txt
RUN pip install -r /requirements.txt