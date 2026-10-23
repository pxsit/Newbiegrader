FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 \
 && python -m pip install --no-cache-dir -r requirements.txt

COPY grader.py server.py judge_runner.py judge_execution.py worker.py problem_store.py backend_problem_files.py container_entrypoint.py job_sandbox.py job_resources.py email_auth.py database_client.py db.py ./
COPY static ./static
COPY tools/plagiarism.py ./tools/plagiarism.py

# Problem data is NOT baked into the image. At runtime the backend reads from
# Supabase (GRADER_PROBLEM_SOURCE=supabase: Postgres rows + private Storage
# bucket); the problem source of truth is versioned outside this repo. Sandbox
# hidden test bytes remain in the trusted application/runner memory. Web jobs
# use private numeric UID/GIDs, Landlock and an inherited network syscall filter.
# These controls do not provide hard aggregate resource or workspace limits;
# see docs/audits/RESOURCE_SETUP.md and docs/audits/JOB_ISOLATION.md.
# Backend stays root to allow the setuid drop. Keep
# .env OUT of the image: secrets come from host env vars, never from files.

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["python", "container_entrypoint.py"]
