# montero/celery.py
import os
from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'montero.settings')

app = Celery('montero')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()

# Configure beat schedule
app.conf.beat_schedule = {
    'process-due-swaps-every-minute': {
        'task': 'crypto_app.tasks.auto_process_swaps',
        'schedule': crontab(minute='*'),  # Run every minute
    },
}
app.conf.timezone = 'Africa/Lagos'

@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')