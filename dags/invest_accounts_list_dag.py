from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from datetime import datetime, timedelta
from tinkoff_invest import getAccounts, loadData

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2023, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    'invest_accounts_list_dag', 
    default_args=default_args, 
    schedule_interval='@hourly',
    catchup=False
) as dag:
    fetch_task = PythonOperator(
        task_id='fetch_tinkoff_invest_account',
        python_callable=getAccounts,
    )

    load_task = PythonOperator(
        task_id='load_invest_accounts_list',
        python_callable=loadData,
    )

    fetch_task >> load_task
