FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.9.5
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY streamlit_app.py ./streamlit_app.py
COPY .streamlit/config.toml ./.streamlit/config.toml
RUN useradd --uid 10001 --create-home agent && mkdir /data && chown agent:agent /data
USER agent
EXPOSE 8000
CMD ["/opt/venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
