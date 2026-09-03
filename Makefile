# Blender to test against. Override for another install:
#   make test BLENDER="flatpak run org.blender.Blender"
#   make test HOST=distrobox-host-exec        # Blender is on the host
BLENDER ?= blender
# Set HOST when Blender lives behind a hop (a container, a wrapper):
#   make test HOST=distrobox-host-exec
# It prefixes every Blender invocation, including the `env VAR=value` form the
# install test needs — an inline assignment does not survive such a hop, and
# the test then refuses to run rather than touching your real profile.
HOST ?=
PY_TAG  ?= 3.14
PLATFORM ?= x86_64-manylinux_2_28
RRD ?=

PKG := rerun_importer
WHEEL_DIR := $(PKG)/wheels
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(PKG)/blender_manifest.toml)
ZIP := dist/$(PKG)-$(VERSION).zip

.PHONY: help wheels test test-reader test-blender test-install no-data hooks zip validate clean

help:
	@echo "make wheels          # vendor rerun-sdk + pyarrow into $(WHEEL_DIR)"
	@echo "make test            # reader tests + headless Blender build tests"
	@echo "make zip             # build $(ZIP) with Blender's own extension builder"
	@echo "make validate        # check the manifest without building"
	@echo "make test-install    # install the zip into a throwaway profile and use it"
	@echo "make no-data         # refuse recorded data in the repository"
	@echo "make hooks           # run the no-data check on every commit"
	@echo "make convert RRD=f.rrd  # headless .rrd -> .blend next to the input"

# rerun-sdk publishes abi3 wheels, so one download serves every Blender Python.
wheels:
	uv pip install --python-platform $(PLATFORM) --python-version $(PY_TAG) \
		--target $(WHEEL_DIR) "rerun-sdk>=0.20" "pyarrow>=14" \
		|| python3 -m pip install --target $(WHEEL_DIR) "rerun-sdk>=0.20" "pyarrow>=14"

test: no-data test-reader test-blender test-install

# Recorded data must never reach this repository; see tools/check_no_data.py.
no-data:
	python3 tools/check_no_data.py

# Run that check on every commit as well. Hooks are not cloned, so this is
# opt-in per clone — CI enforces the same check regardless.
hooks:
	git config core.hooksPath tools/hooks
	@echo "pre-commit hook active (tools/hooks)"

# The reader is bpy-free, so it runs under Blender's Python without a UI.
test-reader:
	$(HOST) $(BLENDER) -b --factory-startup -P tests/run_tests.py -- reader

test-blender:
	$(HOST) $(BLENDER) -b --factory-startup -P tests/run_tests.py -- build

# Installs the built zip into a THROWAWAY Blender profile, so it cannot touch
# a real install, and makes the add-on fetch its own dependencies.
# Two launches: install into an empty profile, then reopen that profile to
# prove the add-on and its SDK are still there. The second launch drops
# --factory-startup on purpose, since that is what reads saved preferences.
test-install: zip
	profile=$$(mktemp -d) && \
		$(HOST) env BLENDER_USER_RESOURCES=$$profile $(BLENDER) -b --factory-startup \
			-P tests/test_install.py -- $(ZIP) \
			--stage=install $(INSTALL_ARGS) && \
		$(HOST) env BLENDER_USER_RESOURCES=$$profile $(BLENDER) -b \
			-P tests/test_install.py -- $(ZIP) \
			--stage=verify $(INSTALL_ARGS); \
		status=$$?; rm -rf $$profile; exit $$status

# Blender's own builder, so the artifact is exactly what Blender produces for
# the extensions platform (and the manifest gets validated on the way).
zip: validate
	@mkdir -p dist
	@rm -f $(ZIP)
	$(HOST) $(BLENDER) --command extension build --source-dir $(PKG) --output-dir dist
	@echo "$(ZIP)"

validate:
	$(HOST) $(BLENDER) --command extension validate $(PKG)

convert:
	@test -n "$(RRD)" || (echo "usage: make convert RRD=path/to/file.rrd" && exit 1)
	$(HOST) $(BLENDER) -b --factory-startup -P tools/rrd2blend.py -- "$(RRD)" -o "$(RRD:.rrd=.blend)"

clean:
	rm -rf dist $(WHEEL_DIR) $(PKG)/_deps .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
