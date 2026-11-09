# Pipeline image: `docker build -f docker/pipeline.Dockerfile -t qualimap-pipeline .`
# Run: `docker run --rm --env-file .env qualimap-pipeline run --due`
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    QM_CACHE_DIR=/cache

# GDAL/PROJ come with the rasterio, pyproj and shapely wheels; osmium needs libexpat/zlib at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libexpat1 zlib1g \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pipeline/pyproject.toml pipeline/
COPY pipeline/src pipeline/src
RUN pip install "./pipeline[all]"

# The catalog is read from the repository root (catalog/indicators.yaml + catalog/local/*.yaml).
COPY catalog catalog

VOLUME ["/cache"]
ENTRYPOINT ["qm"]
CMD ["status"]
