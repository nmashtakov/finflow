# Используем официальный образ Airflow версии 2.9.2
FROM apache/airflow:2.9.2

# Устанавливаем дополнительные пакеты, если необходимо
USER root
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# Копируем DAG и скрипты в контейнер
COPY dags/ /opt/airflow/dags/
COPY scripts/ /opt/airflow/scripts/
