from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import os
import pandas as pd
from sqlalchemy import create_engine
import shutil
from airflow.exceptions import AirflowSkipException


path = '/opt/data/banking/tinkoff/new'
path_processed = '/opt/data/banking/tinkoff/processed'


# проверка папки на наличие файлов
def file_check(path, **kwargs):    

    if not os.path.exists(path):
        raise FileNotFoundError(f'Папка {path} не существует')
    
    files = os.listdir(path)

    if not files:
        print(f'Файлы в {path} не найдены')
        raise AirflowSkipException(f'Файлы в {path} не найдены')
    else:
        kwargs['ti'].xcom_push(key='file_list', value=files)
        print(f'Список файлов передан: {files}')
        return files


# предобработка файла
def file_processing(**kwargs):
    files = kwargs['ti'].xcom_pull(key='file_list')

    if not files:
        print('Нет файлов для обработки')
        return None

    to_db = pd.DataFrame()

    for file in files:
        file_path = os.path.join(path, file)
        df_file = pd.read_excel(file_path)
        to_db = pd.concat([to_db, df_file], axis=0, ignore_index=True)
    
    to_db['Дата операции'] = pd.to_datetime(to_db['Дата операции'], dayfirst=True).dt.strftime('%Y-%m-%d %H:%M:%S')
    to_db['Дата платежа'] = pd.to_datetime(to_db['Дата платежа'], dayfirst=True).dt.strftime('%Y-%m-%d')
    to_db.columns = ['operation_date', 'payment_date', 'card_number', 'status', 'amount', 
                     'currency', 'amount_payment', 'currency_payment', 'cashback', 'category',
                     'mcc', 'description', 'bonus', 'invest_round', 'amount_round']
    print(to_db)
    kwargs['ti'].xcom_push(key='to_db', value=to_db.sort_values('operation_date').to_dict())
    return to_db


# загрузка в БД
def load_db(**kwargs):
    data = kwargs['ti'].xcom_pull(key='to_db')
    if not data:
        print('Нет данных для загрузки')
        return None
    
    load_df = pd.DataFrame(data)

    POSTGRES_USER = os.environ.get('POSTGRES_USER')
    POSTGRES_PASSWORD = os.environ.get('POSTGRES_PASSWORD')
    POSTGRES_DB = os.environ.get('POSTGRES_DB')

    engine = create_engine(f'postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}@postgres:5432/{POSTGRES_DB}')

    load_df.to_sql(
        name='banking_tinkoff',
        con=engine,
        if_exists='append',
        index=False
    )

    print(f"Данные успешно загружены в таблицу banking_tinkoff")

# перемещение из new в processed
def file_moving(path, path_processed, **kwargs):
    files = kwargs['ti'].xcom_pull(key='file_list')
    
    for file in files:
        file_path = os.path.join(path, file)
        if not os.path.exists(file_path):
            print(f"Файл {file_path} не найден!")
        else:
            shutil.move(file_path, path_processed)
            print(f"Файл перемещён из {file_path} в {path_processed}")


default_agrs = {
    'owner': 'nmashtakov',
    'depends_on_past': False,
    'retries': 0,
    'retry_delay': timedelta(seconds=5),
    'start_date': datetime(2025, 1, 4)
}

dag = DAG(
    'raw_banking_tinkoff',
    default_args=default_agrs,
    schedule_interval=None #'0 0 * * 1'
)

check = PythonOperator(
    task_id='file_check',
    python_callable=file_check,
    op_kwargs={'path': path},
    dag=dag
)

processed = PythonOperator(
    task_id='file_processing',
    python_callable=file_processing,
    dag=dag
)

load = PythonOperator(
    task_id='load_db',
    python_callable=load_db,
    dag=dag
)

replace = PythonOperator(
    task_id='file_moving',
    python_callable=file_moving,
    op_kwargs={'path': path,
               'path_processed': path_processed},
    dag=dag
)


check >> processed >> load >> replace