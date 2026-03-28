import os
import sys
import time
from datetime import datetime

import psycopg2
from flask import Flask, jsonify, request


app = Flask(__name__)

#Few passwords
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "warroom-db-proxy")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5433"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "warroom")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "warroom")
POSTGRES_DB = os.getenv("POSTGRES_DB", "warroom")


def get_connection():
	return psycopg2.connect(
		host=POSTGRES_HOST,
		port=POSTGRES_PORT,
		user=POSTGRES_USER,
		password=POSTGRES_PASSWORD,
		dbname=POSTGRES_DB,
	)


def initialize_database():
	with get_connection() as conn:
		with conn.cursor() as cur:
			cur.execute(
				"""
				CREATE TABLE IF NOT EXISTS checkouts (
					id SERIAL PRIMARY KEY,
					item TEXT NOT NULL,
					quantity INTEGER NOT NULL,
					total DOUBLE PRECISION NOT NULL,
					created_at TIMESTAMP NOT NULL DEFAULT NOW()
				)
				"""
			)
			cur.execute("SELECT COUNT(*) FROM checkouts")
			count_row = cur.fetchone()
			row_count = count_row[0] if count_row is not None else 0
			if row_count == 0:
				for i in range(1, 6):
					cur.execute(
						"""
						INSERT INTO checkouts (item, quantity, total, created_at)
						VALUES (%s, %s, %s, NOW())
						""",
						(f"demo-item-{i}", i, 9.99 * i),
					)
		conn.commit()


@app.post("/checkout")
def checkout():
	data = request.get_json(silent=True) or {}
	item = data.get("item", "demo-item")
	quantity = data.get("quantity", 1)
	total = data.get("total", 9.99)

	status_code = 200
	response_time_ms = 0

	try:
		start = time.perf_counter()
		with get_connection() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO checkouts (item, quantity, total, created_at)
					VALUES (%s, %s, %s, NOW())
					""",
					(item, int(quantity), float(total)),
				)
			conn.commit()
		end = time.perf_counter()
		response_time_ms = int((end - start) * 1000)

		payload = {"status": "success", "response_time": response_time_ms}
	except Exception as exc:
		status_code = 500
		payload = {
			"status": "error",
			"response_time": 0,
			"error": str(exc),
		}

	timestamp = datetime.utcnow().isoformat()
	print(f"[{timestamp}] {status_code} /checkout {response_time_ms}", flush=True)
	return jsonify(payload), status_code


@app.get("/health")
def health():
	try:
		with get_connection() as conn:
			with conn.cursor() as cur:
				cur.execute("SELECT 1")
				cur.fetchone()
		return jsonify({"status": "healthy", "db": "connected"}), 200
	except Exception:
		return jsonify({"status": "unhealthy", "db": "disconnected"}), 503


for attempt in range(1, 11):
	try:
		initialize_database()
		break
	except Exception as exc:
		if attempt < 10:
			timestamp = datetime.utcnow().isoformat()
			print(f"[{timestamp}] Waiting for database... attempt {attempt}/10", flush=True)
			time.sleep(2)
		else:
			timestamp = datetime.utcnow().isoformat()
			print(f"[{timestamp}] Database initialization failed after 10 attempts: {exc}", flush=True)
			sys.exit(1)


if __name__ == "__main__":
	app.run(host="0.0.0.0", port=5000)
