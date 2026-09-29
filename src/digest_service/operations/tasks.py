from celery import shared_task

from .service import execute_job


@shared_task(name="digest.execute_job", ignore_result=True)
def execute_background_job(job_id, contract_version=1):
    return execute_job(job_id=job_id, contract_version=contract_version)
