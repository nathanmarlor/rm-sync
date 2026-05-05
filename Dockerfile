FROM jbarlow83/ocrmypdf:latest

# Install Node.js 22
RUN apt-get update -qq && \
    apt-get install -y -qq curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y -qq nodejs && \
    rm -rf /var/lib/apt/lists/*

# Build the Node.js rm→PDF renderer
WORKDIR /build
COPY package.json ./
RUN npm install --silent
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build && \
    cp render.js /app/render.js && \
    cd / && rm -rf /build

WORKDIR /app
COPY sync.py ./

# Use the venv Python — requests, pdfminer.six, ocrmypdf are all pre-installed there
CMD ["/app/.venv/bin/python3", "/app/sync.py"]
