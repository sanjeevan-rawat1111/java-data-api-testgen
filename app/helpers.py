import os
from flask import Blueprint, current_app, jsonify, request
from aerospike_client import AerospikeClient
from mysql_client import MysqlClient

app = Blueprint("helpers", __name__)

mysql_client: MysqlClient = None
aerospike_client: AerospikeClient = None


def _ok(data=None, message="success"):
    return jsonify({"status": "ok", "message": message, "data": data}), 200


def _err(message, status=500):
    return jsonify({"status": "error", "message": message}), status


@app.route("/healthcheck", methods=["GET"])
def healthcheck():
    mysql_ok = mysql_client.is_healthy() if mysql_client else False
    aero_ok = aerospike_client.is_healthy() if aerospike_client else False
    return jsonify({
        "status": "healthy" if (mysql_ok and aero_ok) else "degraded",
        "services": {"mysql": {"healthy": mysql_ok}, "aerospike": {"healthy": aero_ok}},
    }), 200


# ── MySQL ─────────────────────────────────────────────────────────────────────

@app.route("/setdb", methods=["POST"])
def setdb():
    global mysql_client
    db_config = request.get_json(force=True)
    try:
        if mysql_client:
            mysql_client.rollback()
        mysql_client = MysqlClient(db_config, current_app.logger)
        return "Success: DB connected", 200
    except Exception as e:
        return f"FAIL: {e}", 500


@app.route("/query", methods=["POST"])
def query_db():
    """Run numbered SQL statements. Body: {"1": "SQL...", "2": "SQL..."}"""
    if not mysql_client:
        return _err("DB not connected — call /setdb first", 400)
    queries = request.get_json(force=True)
    results = {}
    for key in sorted(queries.keys(), key=lambda k: int(k)):
        results[key] = mysql_client.run_query(queries[key])
    try:
        mysql_client.commit()
    except Exception as e:
        current_app.logger.error("commit failed: %s", e)
    return jsonify(results), 200


@app.route("/select_query", methods=["POST"])
def select_query():
    """Run a SELECT and return rows. Body: {"1": "SELECT ..."}"""
    if not mysql_client:
        return _err("DB not connected — call /setdb first", 400)
    queries = request.get_json(force=True)
    results = {}
    for key in sorted(queries.keys(), key=lambda k: int(k)):
        rows = mysql_client.run_select_query(queries[key])
        results[key] = rows if rows is not None else []
    return jsonify(results), 200


@app.route("/query/commit", methods=["GET"])
def commit_db():
    try:
        mysql_client.commit()
        return "success", 200
    except Exception as e:
        return str(e), 400


@app.route("/query/rollback", methods=["GET"])
def rollback_db():
    try:
        mysql_client.rollback()
        return "success", 200
    except Exception as e:
        return str(e), 400


# ── Aerospike ─────────────────────────────────────────────────────────────────

@app.route("/aerospike/connect", methods=["POST"])
def aerospike_connect():
    global aerospike_client
    try:
        aerospike_client = AerospikeClient(request.get_json(force=True), current_app.logger)
        return "Success: Aerospike connected", 200
    except Exception as e:
        return f"FAIL: {e}", 500


@app.route("/aerospike/set", methods=["POST"])
def aerospike_set():
    if not aerospike_client:
        return _err("Aerospike not connected — call /aerospike/connect first", 400)
    try:
        aerospike_client.set(request.get_json(force=True))
        return _ok(message="Record stored")
    except Exception as e:
        return _err(str(e))


@app.route("/aerospike/get", methods=["POST"])
def aerospike_get():
    if not aerospike_client:
        return _err("Aerospike not connected — call /aerospike/connect first", 400)
    try:
        record = aerospike_client.get(request.get_json(force=True))
        return jsonify({"status": "ok", "data": record}), 200
    except Exception as e:
        return _err(str(e))


@app.route("/aerospike/delete", methods=["POST"])
def aerospike_delete():
    if not aerospike_client:
        return _err("Aerospike not connected — call /aerospike/connect first", 400)
    try:
        aerospike_client.delete(request.get_json(force=True))
        return _ok(message="Set truncated")
    except Exception as e:
        return _err(str(e))


@app.route("/aerospike/deleteSingle", methods=["POST"])
def aerospike_delete_single():
    if not aerospike_client:
        return _err("Aerospike not connected — call /aerospike/connect first", 400)
    try:
        aerospike_client.delete_single(request.get_json(force=True))
        return _ok(message="Record deleted")
    except Exception as e:
        return _err(str(e))


@app.route("/aerospike/scanAll", methods=["POST"])
def aerospike_scan_all():
    if not aerospike_client:
        return _err("Aerospike not connected — call /aerospike/connect first", 400)
    body = request.get_json(force=True)
    try:
        records = aerospike_client.scan_all(body["namespace"], body["set"], body.get("limit", 100))
        return jsonify({"status": "ok", "data": records, "count": len(records)}), 200
    except Exception as e:
        return _err(str(e))
