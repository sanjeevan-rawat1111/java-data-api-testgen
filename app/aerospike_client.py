import aerospike
from aerospike import exception as aerospike_ex


class AerospikeClient:
    def __init__(self, config, logger):
        self.logger = logger
        host = config["host"]
        port = int(config.get("port", 3000))
        self.client = aerospike.client({"hosts": [(host, port)]}).connect()
        self.logger.debug("Connected to Aerospike %s:%s", host, port)

    def is_healthy(self):
        try:
            self.client.info_all("build")
            return True
        except Exception:
            return False

    def set(self, query):
        key = (str(query["namespace"]), str(query["set"]), str(query["key"]))
        self.client.put(key, query["record"], meta=query.get("meta", {}), policy=query.get("policy", {}))
        self.logger.debug("Aerospike SET key=%s", query["key"])

    def get(self, query):
        key = (str(query["namespace"]), str(query["set"]), str(query["key"]))
        try:
            _, _, record = self.client.get(key)
            return record
        except aerospike_ex.RecordNotFound:
            return None

    def delete(self, query):
        """Truncate an entire set."""
        self.client.truncate(query["namespace"], query.get("set", None), query.get("nanos", 0))

    def delete_single(self, query):
        key = (str(query["namespace"]), str(query["set"]), str(query["key"]))
        try:
            self.client.remove(key)
        except aerospike_ex.RecordNotFound:
            pass

    def scan_all(self, namespace, set_name, limit=100):
        results = []

        def callback(record):
            if len(results) >= limit:
                return False
            _, _, bins = record
            results.append(bins)

        self.client.scan(namespace, set_name).foreach(callback)
        return results
