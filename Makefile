.PHONY: install run test demo sync-bq e2e

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	.venv/bin/pytest -q

demo:
	chmod +x scripts/demo.sh
	./scripts/demo.sh

sync-bq:
	.venv/bin/python scripts/sync_to_bigquery.py

e2e:
	.venv/bin/python scripts/e2e_verify.py
