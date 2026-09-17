"""Keep receipt hashes backward-compatible when optional transport fields grow."""
def batch_payload(batch):
    payload = batch.model_dump(mode='json')
    for record in payload['records']:
        if record['event'].get('native_session_id') is None:
            record['event'].pop('native_session_id', None)
    return payload
