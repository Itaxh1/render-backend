from uuid import UUID


def identity_keys(principal, source, source_id, records):
    keys = {f'device:{principal.device_id}:{source_id}'}
    for record in records:
        native = record.event.native_session_id
        if native is None and source == 'claude-code':
            try:
                native = UUID(source_id)
            except ValueError:
                pass
        if native is not None:
            keys.add(f'native:{native}')
        # Compatibility with already-published clients: Codex's first metadata
        # record includes its native UUID and timestamp in the full-line hash.
        if source == 'codex' and record.sequence == 0 and record.item_index == 20000 and record.event.type == 'usage':
            keys.add(f'header:{record.payload_hash}')
    return sorted(keys)
