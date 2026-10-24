PYTHON ?= python3
TOOL ?= all
JOBS ?= 2
PACKAGE_DIR ?= dist/packages
BUNDLE ?= $(PACKAGE_DIR)/$(TOOL).wisp.zip

.PHONY: help list plan build test package sizes validate check
help:
	@$(PYTHON) build.py --help
list:
	$(PYTHON) build.py list
plan:
	$(PYTHON) build.py plan $(TOOL) --jobs $(JOBS)
build:
	$(PYTHON) build.py build $(TOOL) --jobs $(JOBS)
test:
	$(PYTHON) build.py test $(TOOL) --jobs $(JOBS)
package:
	$(PYTHON) build.py package $(TOOL) --output-dir "$(PACKAGE_DIR)"
sizes:
	$(PYTHON) build.py sizes $(TOOL) --package-dir "$(PACKAGE_DIR)"
validate:
	$(PYTHON) build.py validate "$(BUNDLE)"
check:
	$(PYTHON) test-build.py
	$(PYTHON) test-packages.py
	$(PYTHON) test-workspace.py
