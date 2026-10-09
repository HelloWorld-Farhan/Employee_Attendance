# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing employee attendance lifecycle management, race-safe punch operations, manual corrections with an audit trail, and MongoDB aggregation analytics pipelines.

## Requirements
* Python 3.11+
* MongoDB 6.0+ (Atlas or local container)

## Setup & Running

1. **Clone the repository and install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment**:
   Copy `.env.example` to `.env` or supply environment variables:
   ```bash
   cp .env.example .env
   ```
   * `MONGO_URI`: MongoDB connection string (e.g. `mongodb://localhost:27017` or Atlas connection string).
   * `MONGO_DB`: Database name (defaults to `attendance_db`).

3. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

   Interactive Swagger UI is available at: `http://localhost:8000/docs`

## Highlights & Implementation Notes
* **All-in-One Application**: All endpoints, schemas, indexes, and aggregation logic reside in [`app/main.py`](file:///d:/Projects/Employee_Attendance/app/main.py).
* **Automatic Index Management**: Indexes (`emp_code` unique, `(emp_code, date)` compound unique, sorting, and range indexes) are created idempotently on application startup via FastAPI lifespan.
* **Strict Business Logic (R1–R10)**:
  * IST (`UTC+05:30`) calendar dates and overnight shift rollover support.
  * Late computation strictly past the 10:00 grace threshold.
  * Overtime calculation with $\ge$ 30-minute threshold.
  * Decimal half-up rounding (`ROUND_HALF_UP`) for work hours and rates.
* **Concurrency Safety**: Database unique constraints enforce single punch-in per date; atomic updates ensure race-safe punch-out and optimistic history preservation under concurrent edits.
* **Pure Aggregations**: Analytics endpoints are built on MongoDB aggregation pipelines with zero Python loop computation.
* **Explain API**: `GET /admin/explain/{endpoint}` reports execution stats and index scan (`IXSCAN`) coverage.

## Project Structure
* `app/main.py` - Core application code, models, and endpoints
* `requirements.txt` - Project dependencies
* `REVIEW.md` - Analysis of starter defects and fixes
* `DECISIONS.md` - Technical answers on indexing, concurrency, and architecture
* `.env.example` - Environment configuration template
