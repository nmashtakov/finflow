#!/bin/sh
set -e

sleep 10
pip install --disable-pip-version-check -r /app/requirements.txt
python manage.py migrate
exec python manage.py runserver 0.0.0.0:8000
