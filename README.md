# Retail Trust Backend Service

This is the backend service for the Retail Trust & Security Dashboard. It is a FastAPI application that provides real-time transaction data and alerts via WebSockets.

## Features

- **Real-time Data Streaming**: Simulates POS (Point of Sale) and VAS (Video Analytics System) data streams.
- **Fraud Detection Engine**: Analyzes streams to detect potential fraud scenarios.
- **WebSocket API**: Broadcasts new transactions and alerts to connected clients.
- **REST API**: Provides endpoints for admin actions (e.g., validating transactions).

## Prerequisites

- Python 3.8+
- `pip` (Python package installer)

## Setup

1.  **Clone the repository** (if you haven't already):
    ```bash
    git clone https://github.com/7013011480/Retail-Trust-Backend-Service.git
    cd "Retail Trust Backend Service"
    ```

2.  **Create a virtual environment**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

## Running the Service

1.  **Activate the virtual environment**:
    ```bash
    source venv/bin/activate
    ```

2.  **Start the server**:
    ```bash
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
    ```

    The service will be available at `http://localhost:8000`.
    The WebSocket endpoint is available at `ws://localhost:8000/ws`.

## API Documentation

- **Swagger UI**: Visit `http://localhost:8000/docs` for interactive API documentation.
- **ReDoc**: Visit `http://localhost:8000/redoc` for alternative documentation.
