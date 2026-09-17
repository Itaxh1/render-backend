"""Signed, scoped, expiring keyset cursors. No transcript text is embedded."""
import base64
import hashlib
import hmac
import json
import time

from .day_contract import ExpiredStoryCursor, InvalidStoryCursor


def encode_cursor(payload, key):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    signature = hmac.new(key.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + raw).decode().rstrip('=')


def decode_cursor(token, key, user_id, day, session_id):
    try:
        if len(token) > 4096:
            raise ValueError('oversized')
        data = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        signature, raw = data[:32], data[32:]
        if not hmac.compare_digest(signature, hmac.new(key.encode(), raw, hashlib.sha256).digest()):
            raise ValueError('signature')
        payload = json.loads(raw)
        if (payload['v'], payload['u'], payload['d'], payload['s']) != (1, str(user_id), day.isoformat(), session_id):
            raise ValueError('scope')
        if payload['exp'] < time.time():
            raise ExpiredStoryCursor('Story cursor expired')
        return payload
    except ExpiredStoryCursor:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise InvalidStoryCursor('Invalid story cursor') from error
