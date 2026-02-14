FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ src/
COPY scripts/ scripts/

# Create data directories
RUN mkdir -p data/exports

# Default: run the bot
CMD ["python", "-m", "src.main"]
