# \# Employee Attendance \& Analytics API

# 

# FastAPI and MongoDB backend for Employee Attendance \& Analytics.

# 

# \## Requirements

# \- Python 3.11+

# \- MongoDB 6.0+

# 

# \## Local Setup \& Run

# ```bash

# python -m venv .venv

# source .venv/bin/activate

# pip install -r requirements.txt

# cp .env.example .env  # configure MONGO\_URI and MONGO\_DB

# python sample\_seed.py

# uvicorn app.main:app --port 8000 --reload

