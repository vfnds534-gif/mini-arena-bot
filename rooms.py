import random
import time

MAX_GUESTS = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 5


class RoomError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Connection:
    def __init__(self, ws, room_code, role, user_id, name, slot=None):
        self.ws = ws
        self.room_code = room_code
        self.role = role  # 'host' | 'guest'
        self.user_id = user_id
        self.name = name
        self.slot = slot


class Room:
    def __init__(self, code, host_conn):
        self.code = code
        self.host = host_conn
        self.guests = {}  # slot -> Connection
        self.started = False
        self.created_at = time.time()

    def roster(self):
        result = []
        for slot in range(MAX_GUESTS):
            conn = self.guests.get(slot)
            if conn:
                result.append({
                    "slot": slot,
                    "userId": conn.user_id,
                    "name": conn.name,
                    "controlType": "network",
                    "connected": True,
                })
            else:
                result.append({
                    "slot": slot,
                    "userId": None,
                    "name": None,
                    "controlType": "bot",
                    "connected": False,
                })
        return result


class RoomRegistry:
    def __init__(self):
        self.rooms = {}  # code -> Room
        self.connections = {}  # ws -> Connection

    def _generate_code(self):
        for _ in range(50):
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self.rooms:
                return code
        raise RoomError("code_generation_failed")

    def create_room(self, ws, user_id, name):
        code = self._generate_code()
        conn = Connection(ws, code, "host", user_id, name)
        room = Room(code, conn)
        self.rooms[code] = room
        self.connections[ws] = conn
        return room

    def join_room(self, ws, code, user_id, name):
        room = self.rooms.get(code)
        if not room:
            raise RoomError("not_found")
        if room.started:
            raise RoomError("already_started")
        free_slot = None
        for slot in range(MAX_GUESTS):
            if slot not in room.guests:
                free_slot = slot
                break
        if free_slot is None:
            raise RoomError("full")
        conn = Connection(ws, code, "guest", user_id, name, slot=free_slot)
        room.guests[free_slot] = conn
        self.connections[ws] = conn
        return room, free_slot

    def mark_started(self, room):
        room.started = True

    def get_connection(self, ws):
        return self.connections.get(ws)

    def get_room(self, code):
        return self.rooms.get(code)

    def remove_connection(self, ws):
        conn = self.connections.pop(ws, None)
        if not conn:
            return None, None, []
        room = self.rooms.get(conn.room_code)
        if not room:
            return None, None, []
        if conn.role == "host":
            del self.rooms[conn.room_code]
            targets = [g.ws for g in room.guests.values()]
            return "room_closed", None, targets
        else:
            if room.guests.get(conn.slot) is conn:
                del room.guests[conn.slot]
            targets = [room.host.ws] + [g.ws for g in room.guests.values()]
            return "roster", room, targets
