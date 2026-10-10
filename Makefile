PYTHON ?= python3
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
	$(PYTHON) -m pytest tests e2e_tests --cov=commontrace --cov-report=xml --cov-fail-under=80 $(PYTEST_ARGS)
lint:
	$(PYTHON) scripts/dev.py lint
check:
	$(PYTHON) scripts/dev.py check
	$(PYTHON) scripts/dev.py docs --check
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
	helm lint deploy/helm/commontrace-hub --set image.tag=local --set existingSecret=hub-secrets
	helm template local deploy/helm/commontrace-hub --set image.tag=local --set existingSecret=hub-secrets > /dev/null
