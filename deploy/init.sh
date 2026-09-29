#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear
python manage.py import_seed
python manage.py configure_staff_roles
python manage.py check --deploy
