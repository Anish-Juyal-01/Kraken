from types import SimpleNamespace

from kraken.config import Config
from kraken.main import find_hq_rooms


class Rooms:
    def __init__(self, rooms):
        self.rooms = rooms

    async def op(self, name, **args):
        assert name == "list_rooms"
        return self.rooms


async def test_hq_rooms_skip_archived_namesakes():
    client = Rooms([
        {"name": "kraken-requests", "room_id": "new-req", "archived": False},
        {"name": "kraken-requests", "room_id": "old-req", "archived": True},
        {"name": "kraken-hallmark", "room_id": "old-hall", "archived": True},
        {"name": "kraken-hallmark", "room_id": "new-hall"},
    ])
    dispatcher = SimpleNamespace(room_ids={})
    await find_hq_rooms(client, dispatcher, Config())
    assert dispatcher.room_ids == {"kraken-requests": "new-req", "kraken-hallmark": "new-hall"}
