# PenguinBurner 0.2.6 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.6 improves managed Q2RTX dependency installation on systems
where the OpenSSL 1.1 compatibility fallback has trouble reaching the primary
CentOS Stream package index.

### Q2RTX Dependency Downloads

- Q2RTX release metadata now falls back from the GitHub API to the official
  GitHub releases page before failing.
- Q2RTX release downloads now use a mirror-aware retry path, keeping the
  official GitHub release URL as the current source.
- OpenSSL 1.1 compatibility RPM lookup now retries multiple CentOS Stream
  AppStream package indexes.
- OpenSSL compatibility RPM downloads now retry the resolved RPM across all
  configured CentOS Stream mirrors.
- Download timeout and failure messages now include the URL that failed, making
  install reports easier to diagnose.

## PyPI Release Summary

PenguinBurner 0.2.6 improves Q2RTX dependency installation reliability by adding
fallback lookup/download sources for the OpenSSL 1.1 compatibility RPM and
clearer URL-specific timeout diagnostics.
