srpm:
	dnf -y install rpm-build pyproject-rpm-macros python3-devel python3-pip python3-setuptools python3-wheel desktop-file-utils
	version=$$(python3 -c 'import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])'); \
	name=penguin-burner; \
	tmpdir=$$(mktemp -d); \
	tarball="$(outdir)/$${name}-$${version}.tar.gz"; \
	tar --exclude=.git --exclude=.copr --exclude=dist --exclude=build --exclude='*.egg-info' \
		--transform "s,^\.,$${name}-$${version}," \
		-czf "$${tarball}" .; \
	rpmbuild -bs packaging/rpm/penguin-burner.spec \
		--define "_sourcedir $(outdir)" \
		--define "_srcrpmdir $(outdir)" \
		--define "_topdir $${tmpdir}/rpmbuild"; \
	rm -rf "$${tmpdir}"
