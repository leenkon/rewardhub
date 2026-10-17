.RECIPEPREFIX := >

PYTHON ?= python3
BINARY_NAME ?= RewardHub-$(VERSION)
BINARY_DIST ?= build/dist
VERSION ?= $(shell $(PYTHON) scripts/release.py next-version)

.PHONY: next-version test check bump-version changelog release binary binary-package

next-version:
>@$(PYTHON) scripts/release.py next-version

test:
>$(PYTHON) -m unittest discover -s tests -q

check:
>$(PYTHON) -m unittest discover -s tests -q
>$(PYTHON) -m py_compile app.py
>git diff --check

bump-version:
>$(PYTHON) scripts/release.py bump-version --version "$(VERSION)"

changelog:
>$(PYTHON) scripts/release.py changelog --version "$(VERSION)"

release:
>$(MAKE) check
>$(MAKE) bump-version VERSION="$(VERSION)"
>$(MAKE) changelog VERSION="$(VERSION)"
>$(MAKE) check

binary:
>$(PYTHON) scripts/build_binary.py --name "$(BINARY_NAME)" --distpath "$(BINARY_DIST)"

binary-package: binary
>$(PYTHON) scripts/package_binary.py --version "$(VERSION)" --platform "$(BINARY_PLATFORM)" --distpath "$(BINARY_DIST)"
