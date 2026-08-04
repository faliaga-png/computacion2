.PHONY: run install clean

install:
	python3 -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt

run:
	python3 src/main.py

clean:
	rm -rf venv
	find . -name "__pycache__" -type d -exec rm -rf {} +
	rm -f dump_*.json
