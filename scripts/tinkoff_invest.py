import requests
import json
import os
import psycopg2
from datetime import datetime


# Отправка любого запроса к API
def sendRequest(method, data):
    token = os.getenv('TINKOFF_INVEST_TOKEN')
    based_url = 'https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1'

    # Заголовки запроса
    headers = {
        'accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    response = requests.post(based_url + method, headers=headers, json=data)
    return(response)

# Получение информации по брокерским счетам
def getAccounts():
    getAccounts = '.UsersService/GetAccounts'
    request_data = {}
    # Передает данные для отправки запроса
    response = sendRequest(getAccounts, request_data)
    prepared_data = []

    if response.status_code == 200:
        parsed_response = response.json()
        for account in parsed_response['accounts']:
            prepared_data.append({
                'account_id': int(account['id']),
                'name': account['name'],
                'status': account['status'],
                'opened_date': datetime.strptime(account['openedDate'], '%Y-%m-%dT%H:%M:%SZ'),
                'closed_date': datetime.strptime(account['closedDate'], '%Y-%m-%dT%H:%M:%SZ')
            })
    else:
        print(f'Ошибка {response.status_code}: {response.text}')
        return

    with open('/opt/airflow/data/prepared_accounts.json', 'w') as f:
        json.dump(prepared_data, f, default=str)

def loadData():
    with open('/opt/airflow/data/prepared_accounts.json', 'r') as f:
        prepared_data = json.load(f)

    conn = psycopg2.connect(
        host=os.getenv('POSTGRES_HOST'),
        port=5432,
        database=os.getenv('POSTGRES_DB'),
        user=os.getenv('POSTGRES_USER'),
        password=os.getenv('POSTGRES_PASSWORD')
    )
    cursor = conn.cursor()

    records = []
    current_time = datetime.now()
    for record in prepared_data:
        records.append(cursor.mogrify("(%s, %s, %s, %s, %s, %s)", 
                      (record['account_id'], record['name'], record['status'], record['opened_date'], record['closed_date'], current_time)).decode('utf-8'))

    insert_query = f"""
    INSERT INTO invest_accounts_list (account_id, name, status, opened_date, closed_date, processed_dttm) 
    VALUES {','.join(records)} 
    ON CONFLICT (account_id) DO UPDATE 
    SET name = EXCLUDED.name, 
        status = EXCLUDED.status, 
        opened_date = EXCLUDED.opened_date, 
        closed_date = EXCLUDED.closed_date,
        processed_dttm = EXCLUDED.processed_dttm;
    """

    cursor.execute(insert_query)
    conn.commit()
    cursor.close()
    conn.close()