FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/
COPY dashboard/ dashboard/
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# docs/ (results.md, img/pr_curve.png) and data/ (the tier's DuckDB) are runtime inputs,
# not baked into the image -- mount them at run time, e.g.:
#   docker run -p 8501:8501 -v $(pwd)/data:/app/data -v $(pwd)/docs:/app/docs mdm-dashboard

EXPOSE 8501

ENTRYPOINT ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0"]
