FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Amsterdam
 
WORKDIR /src
 
# Install gw2pvo from the local checkout (with the SEMS+ gw_api.py)
# instead of from PyPI.
COPY . /src
RUN pip install /src
 
ENTRYPOINT ["gw2pvo", "--config", "/gw2pvo.cfg"]
