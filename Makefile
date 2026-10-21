PYTHON ?= $(shell if [ -f .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PYTEST_ARGS ?=

.PHONY: test test-unit test-integration test-e2e coverage lint check build sdk benchmark serve deploy-validate
test:
	$(PYTHON) scripts/dev.py test $(PYTEST_ARGS)
test-unit:
	$(PYTHON) -m pytest tests -m unit $(PYTEST_ARGS)
test-integration:
	$(PYTHON) -m pytest tests -m integration $(PYTEST_ARGS)
test-e2e:
	$(PYTHON) -m pytest tests/e2e e2e_tests $(PYTEST_ARGS)
coverage:
	$(PYTHON) -m pytest tests e2e_tests --cov=unimem --cov-report=xml --cov-fail-under=80 $(PYTEST_ARGS)
lint:
	$(PYTHON) scripts/dev.py lint
check:
	$(PYTHON) scripts/dev.py check
	$(PYTHON) scripts/export_openapi.py --check
build:
	$(PYTHON) -m build
sdk:
	$(PYTHON) scripts/generate_sdks.py
benchmark:
	bash reproduce.sh
serve:
	$(PYTHON) scripts/dev.py serve
deploy-validate:
	helm lint deploy/helm/unimem-hub --set image.repository=unimem-hub --set image.tag=local --set existingSecret=hub-secrets
	helm template local deploy/helm/unimem-hub --set image.repository=unimem-hub --set image.tag=local --set existingSecret=hub-secrets > /dev/null
