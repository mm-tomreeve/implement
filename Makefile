VENV ?= .venv
BIN := $(VENV)/bin
export ANSIBLE_CONFIG := $(CURDIR)/ansible.cfg

.PHONY: venv collections lint test render smoke e2e clean

venv:
	python3 -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet 'ansible-core>=2.14' ansible-lint yamllint shellcheck-py

collections:
	$(BIN)/ansible-galaxy collection install -r requirements.yml -p collections

lint: collections
	$(BIN)/yamllint .
	$(BIN)/ansible-lint
	$(BIN)/shellcheck -x install.sh tools/build-offline-bundle.sh tests/smoke/run.sh tests/render/run.sh tests/e2e/run.sh tests/e2e/functional.sh
	for pb in playbooks/*.yml; do $(BIN)/ansible-playbook --syntax-check $$pb || exit 1; done

test:
	$(BIN)/python -m unittest discover -s tests -v

render: collections
	ANSIBLE_PLAYBOOK=$(BIN)/ansible-playbook tests/render/run.sh

smoke:
	tests/smoke/run.sh

e2e:
	tests/e2e/run.sh

clean:
	rm -rf collections tests/smoke/out tests/render/out tests/e2e/out
