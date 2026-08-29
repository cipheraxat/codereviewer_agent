def process(payload):
    return payload

def run_job(payload):
    try:
        process(payload)
    except Exception:
        return False
