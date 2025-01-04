from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
from pendulum import timezone


moscow_tz = timezone("Europe/Moscow")

def print_data():
    print('Hello World')

default_args = {
    'owner': 'nmashtakov',
    'depends_on_past': False,
    'retries': 1,
    'start_date': datetime(2025, 1, 1, 0, 0),
}

dag = DAG(
    'first_dag',
    default_args=default_args,
    schedule_interval='24 17 * * *',
    catchup=False 
)

t1 = PythonOperator(
    task_id='def0',
    python_callable=print_data,
    dag=dag
)

