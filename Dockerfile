FROM alpine

WORKDIR /app
COPY server.py .
COPY requirements.txt .

# 1. Install Python 3, pip and build tools (needed for packages with C extensions)
RUN apk --no-cache add\
    python3 \
    py3-pip


# 2. Install the Python packages that were listed in the Debian image
RUN python3 -m venv /opt/python-env
RUN /opt/python-env/bin/python3 -m pip install --upgrade pip

RUN /opt/python-env/bin/pip install --no-cache-dir -r requirements.txt

# 3. (Optional) Clean up if you added any temporary files
RUN rm -rf /var/cache/apk/*

VOLUME [ "/cache" ]

CMD [ "/opt/python-env/bin/python3", "server.py", "--config", "/app/config.toml", "--cache-dir", "/cache" ]
