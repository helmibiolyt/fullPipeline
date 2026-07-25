FROM docker.io/apache/airflow:2.10.3-python3.11

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Playwright browsers for anzctr / chictr / isrctn / loinc.
# Tasks run as ROOT (AIRFLOW_UID=0), so browsers must live in a shared path —
# a per-user ~/.cache install (the default) isn't found by root at runtime.
# anzctr uses channel="chrome" (Google Chrome); the others use bundled chromium.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
USER root
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth && rm -rf /var/lib/apt/lists/*
RUN playwright install --with-deps chromium chrome \
 && chmod -R a+rx /ms-playwright
USER airflow
