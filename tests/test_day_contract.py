import json
from datetime import date
from pathlib import Path
import time

import pytest

from backend.day_contract import DayRibbonResponse, DayExtrasResponse, DayStoryResponse, ExpiredStoryCursor, InvalidStoryCursor
from backend.story_cursor import decode_cursor, encode_cursor


def test_shared_synthetic_fixture_validates_and_preserves_string_ids():
    data = json.loads((Path(__file__).parents[1]/'contracts/day-api-v1.fixture.json').read_text())
    for name,model in [('ribbon',DayRibbonResponse),('extras',DayExtrasResponse),('story',DayStoryResponse)]:
        parsed = model.model_validate(data[name])
        assert parsed.model_dump(mode='json',by_alias=True) == data[name]
    assert int(data['ribbon']['events'][0]['id']) > 2**53


def test_cursor_expiry_signature_scope_and_key_rotation():
    day = date(2026,9,16)
    payload = dict(v=1,u='user',d=str(day),s=None,exp=int(time.time())+60)
    encoded = encode_cursor(payload,'key')
    assert decode_cursor(encoded,'key','user',day,None) == payload
    for key,user,scope in [('other','user',None),('key','foreign',None),('key','user','session')]:
        with pytest.raises(InvalidStoryCursor):
            decode_cursor(encoded,key,user,day,scope)
    with pytest.raises(ExpiredStoryCursor):
        decode_cursor(encode_cursor(dict(payload,exp=0),'key'),'key','user',day,None)
