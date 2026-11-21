# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/sqlglot && \
    tar -xzf /tmp/sqlglot.tar.gz -C /app/sqlglot --strip-components=1 && \
    rm /tmp/sqlglot.tar.gz
COPY selector.py coverage.json.gz comparison_profiles.json.gz /app/
CMD ["python", "/app/selector.py"]
