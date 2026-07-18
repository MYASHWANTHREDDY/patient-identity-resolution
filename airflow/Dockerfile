# Airflow orchestrating BigQuery + Dataproc Serverless from local Docker (Phase 13,
# PROJECT_CONSTITUTION.md #15 -- Cloud Composer is explicitly out of budget: it bills for a
# running environment, not per-DAG execution).
FROM apache/airflow:2.10.4-python3.11

# dbt-core 1.12.0 requires click>=8.3.0; Airflow 2.10.4 requires click==8.1.7 -- genuinely
# incompatible in one environment, not just an unpinned-version accident (see
# docs/design-decisions.md, Phase 13). dbt gets its own venv, isolated from Airflow's
# site-packages; BashOperator tasks call /home/airflow/dbt-venv/bin/dbt directly.
RUN python -m venv /home/airflow/dbt-venv && \
    /home/airflow/dbt-venv/bin/pip install --no-cache-dir dbt-core==1.12.0 dbt-bigquery==1.12.0

COPY airflow/requirements.txt /tmp/requirements-airflow.txt

# --constraint pins every dependency Airflow itself already constrains (the provider package
# above included) to versions known to work with this exact Airflow release -- installing
# apache-airflow-providers-google without it is how you end up with a broken Airflow core.
RUN pip install --no-cache-dir \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.4/constraints-3.11.txt" \
    -r /tmp/requirements-airflow.txt
