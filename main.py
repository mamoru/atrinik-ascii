import socket
import select
import struct
import threading
try:
    import queue
except ImportError:
    import Queue as queue
import time
import re
import curses
import string
import logging

class ClientCommand(object):
    CONNECT, SEND, DATA, CLOSE = range(4)

    def __init__(self, type, data = None):
        self.type = type
        self.data = data

class ClientReply(object):
    ERROR, SUCCESS = range(2)

    def __init__(self, cmd_type, type, data = None):
        self.cmd_type = cmd_type
        self.type = type
        self.data = data

class MetaserverThread(threading.Thread):
    def __init__(self):
        super(MetaserverThread, self).__init__()
        self.cmd_q = queue.Queue()
        self.reply_q = queue.Queue()
        self.alive = threading.Event()
        self.alive.set()

        self.handlers = {
            ClientCommand.CONNECT: self._handle_CONNECT,
            ClientCommand.CLOSE: self._handle_CLOSE,
        }

    def join(self, timeout = None):
        self.alive.clear()
        threading.Thread.join(self, timeout)

    def _handle_CONNECT(self, cmd):
        self.reply_q.put(ClientReply(cmd.type, ClientReply.SUCCESS, cmd))
        servers = [{
            "name": "Atrinik Dev Server",
            "host": "game.atrinik.org",
            "port": 13326,
        }, {
            "name": "Localhost",
            "host": "localhost",
            "port": 13327,
        }]
        self.reply_q.put(ClientReply(ClientCommand.DATA, ClientReply.SUCCESS, servers))

    def _handle_CLOSE(self, cmd):
        self.reply_q.put(ClientReply(cmd.type, ClientReply.SUCCESS, cmd))

    def run(self):
        while self.alive.isSet():
            try:
                cmd = self.cmd_q.get(True, 0.1)
                self.handlers[cmd.type](cmd)
            except queue.Empty as e:
                pass

class SocketClientThread(threading.Thread):
    def __init__(self):
        super(SocketClientThread, self).__init__()
        self.cmd_q = queue.Queue()
        self.reply_q = queue.Queue()
        self.alive = threading.Event()
        self.alive.set()
        self.socket = None

        self.handlers = {
            ClientCommand.CONNECT: self._handle_CONNECT,
            ClientCommand.CLOSE: self._handle_CLOSE,
            ClientCommand.SEND: self._handle_SEND,
        }

    def run(self):
        buffer = bytes()
        header_len = -1
        data_len = -1

        while self.alive.isSet():
            try:
                # queue.get with timeout to allow checking self.alive
                cmd = self.cmd_q.get(True, 0.1)
                self.handlers[cmd.type](cmd)
            except queue.Empty as e:
                pass

            if not self.socket:
                continue

            r, w, e = select.select([self.socket], [], [], 1.0)

            if not r:
                continue

            try:
                buffer += self.socket.recv(4096)
            except socket.error as e:
                self.cmd_q.put(ClientCommand(ClientCommand.CLOSE, e))
                continue

            while True:
                if header_len == -1:
                    if len(buffer) >= 2:
                        header_len = 2

                        if struct.unpack("B", buffer[:1])[0] & 0x80:
                            header_len = 3

                if data_len == -1 and header_len != -1 and len(buffer) >= header_len:
                    # Unpack the header.
                    unpacked = struct.unpack("{0}B".format(header_len), buffer[:header_len])
                    data_len = 0

                    # 3-byte header.
                    if header_len == 3:
                        data_len += (unpacked[-3] & 0x7f) << 16

                    data_len += (unpacked[-2] << 8) + unpacked[-1]

                    buffer = buffer[header_len:]

                if data_len != -1 and len(buffer) >= data_len:
                    self.reply_q.put(ClientReply(ClientCommand.DATA, ClientReply.SUCCESS, buffer[:data_len]))
                    buffer = buffer[data_len:]
                    header_len = -1
                    data_len = -1
                    continue

                break

    def join(self, timeout=None):
        self.alive.clear()
        threading.Thread.join(self, timeout)

    def _handle_CONNECT(self, cmd):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.connect((cmd.data[0], cmd.data[1]))
            self.reply_q.put(ClientReply(cmd.type, ClientReply.SUCCESS, cmd))
        except IOError as e:
            self.reply_q.put(ClientReply(cmd.type, ClientReply.ERROR, str(e)))

    def _handle_CLOSE(self, cmd):
        self.socket.close()
        self.socket = None
        self.reply_q.put(ClientReply(cmd.type, ClientReply.SUCCESS, cmd))

    def _handle_SEND(self, cmd):
        try:
            self.socket.sendall(struct.pack("BB", (len(cmd.data) >> 8 & 0xFF), len(cmd.data) & 0xFF) + cmd.data)
            self.reply_q.put(ClientReply(cmd.type, ClientReply.SUCCESS, cmd))
        except IOError as e:
            self.reply_q.put(ClientReply(cmd.type, ClientReply.ERROR, str(e)))

def data_get_str(data):
    idx = data.find("\x00")
    s = ""

    if idx != -1:
        s = data[:idx]
        data = data[idx + 1:]

    return data, s

class CommandHandler:
    def handle_command_map(self, data):
        mapstat = struct.unpack("!B", data[:1])[0]
        data = data[1:]

        if mapstat != CommandHandler.MAP_UPDATE_CMD_SAME:
            data, mapname = data_get_str(data)
            data, bg_music = data_get_str(data)
            data, weather = data_get_str(data)

            if mapstat == CommandHandler.MAP_UPDATE_CMD_NEW:
                width, height, xpos, ypos = struct.unpack("!4B", data[:4])
                data = data[4:]
                self.map.set_data(width, height, xpos, ypos)
            else:
                tile, xoff, yoff, xpos, ypos = struct.unpack("!B2b2B", data[:5])
                data = data[5:]

                self.map.mapscroll(xoff, yoff, xpos, ypos)
        else:
            xpos, ypos = struct.unpack("!2B", data[:2])
            data = data[2:]

            if xpos - self.map.xpos or ypos - self.map.ypos:
                self.map.mapscroll(xpos - self.map.xpos, ypos - self.map.ypos, xpos, ypos)

        while data:
            mask, = struct.unpack("!H", data[:2])
            data = data[2:]
            x = (mask >> 11) & 0x1f
            y = (mask >> 6) & 0x1f

            x -= 17 / 2
            y -= 17 / 2

            x += self.map.pos[0]
            y += self.map.pos[1]

            if mask & CommandHandler.MAP2_MASK_CLEAR:
                self.map.tile_clear(x, y)
                continue

            if mask & CommandHandler.MAP2_MASK_DARKNESS:
                data = data[1:]

            num_layers, = struct.unpack("!B", data[:1])
            data = data[1:]

            for i in range(num_layers):
                cmd, = struct.unpack("!B", data[:1])
                data = data[1:]

                if cmd == CommandHandler.MAP2_LAYER_CLEAR:
                    layer, = struct.unpack("!B", data[:1])
                    data = data[1:]
                    self.map.tile_clear_layer(x, y, layer)
                else:
                    obj_data = {}

                    obj_data["face"], obj_data["flags"], flags = struct.unpack("!H2B", data[:4])
                    data = data[4:]

                    if flags & CommandHandler.MAP2_FLAG_MULTI:
                        obj_data["quick_pos"], = struct.unpack("!B", data[:1])
                        data = data[1:]

                    if flags & CommandHandler.MAP2_FLAG_NAME:
                        data, obj_data["player_name"] = data_get_str(data)
                        data, obj_data["player_color"] = data_get_str(data)

                    if flags & CommandHandler.MAP2_FLAG_PROBE:
                        obj_data["probe"], = struct.unpack("!B", data[:1])
                        data = data[1:]

                    if flags & CommandHandler.MAP2_FLAG_HEIGHT:
                        data = data[2:]

                    if flags & CommandHandler.MAP2_FLAG_ZOOM:
                        data = data[4:]

                    if flags & CommandHandler.MAP2_FLAG_ALIGN:
                        data = data[2:]

                    if flags & CommandHandler.MAP2_FLAG_MORE:
                        flags2, = struct.unpack("!L", data[:4])
                        data = data[4:]

                        if flags2 & CommandHandler.MAP2_FLAG2_ALPHA:
                            data = data[1:]

                        if flags2 & CommandHandler.MAP2_FLAG2_ROTATE:
                            data = data[2:]

                        if flags2 & CommandHandler.MAP2_FLAG2_TARGET:
                            obj_data["count"], obj_data["is_friend"] = struct.unpack("!LB", data[:5])
                            data = data[5:]

                    self.map.tile_update_object(x, y, cmd, obj_data)

            ext_flags, = struct.unpack("!B", data[:1])
            data = data[1:]

            if ext_flags & CommandHandler.MAP2_FLAG_EXT_ANIM:
                data = data[3:]

    def handle_command_characters(self, data):
        if len(data) == 0:
            self.state -= 1
            return

        self.state += 1
        self.characters = []

        data, account = data_get_str(data)
        data, host = data_get_str(data)
        data, last_host = data_get_str(data)
        last_time, = struct.unpack("!Q", data[:8])
        data = data[8:]

        while data:
            data, archname = data_get_str(data)
            data, name = data_get_str(data)
            data, region_name = data_get_str(data)
            anim_id, level = struct.unpack("!HB", data[:3])
            data = data[3:]

            self.characters.append({
                "name": name,
                "level": level,
            })

    def handle_command_player(self, data):
        if self.state == self.ST_WAITPLAY:
            self.state += 1

    def handle_command_compressed(self, data):
        # TODO: implement?
        pass

    def handle_command_drawinfo(self, data):
        type, color = struct.unpack("!B6s", data[:7])
        msg = data[8:-1]
        self.show_text(msg, win = "status", center = False)

    def handle_command_version(self, data):
        try:
            version, = struct.unpack("!L", data)
        except struct.error as e:
            logging.error("Bad data: {}".format(e))
            return

        self.cpl.socket_version = version

        if self.state == self.ST_WAITVERSION:
            self.state += 1

    def handle_command_setup(self, data):
        if self.state == self.ST_WAITSETUP:
            self.state += 1

    commands = [
        ("Map", handle_command_map),
        ("Drawinfo", handle_command_drawinfo),
        ("File update", None),
        ("Item", None),
        ("Sound", None),
        ("Target", None),
        ("Update item", None),
        ("Delete item", None),
        ("Player stats", None),
        ("Image", None),
        ("Animation", None),
        ("Ready skill", None),
        ("Player info", handle_command_player),
        ("Map stats", None),
        ("Skill list", None),
        ("Version", handle_command_version),
        ("Setup", handle_command_setup),
        ("Control", None),
        ("Server file data", None),
        ("Characters list", handle_command_characters),
        ("Book GUI", None),
        ("Party", None),
        ("Quickslot", None),
        ("Compressed", handle_command_compressed),
        ("Region map", None),
        ("Ambient sound", None),
        ("Interface", None),
        ("Notification", None),
    ]

    MAP_UPDATE_CMD_SAME, \
    MAP_UPDATE_CMD_NEW, \
    MAP_UPDATE_CMD_CONNECTED = range(3)

    MAP2_MASK_CLEAR = 0x2
    MAP2_MASK_DARKNESS = 0x4

    MAP2_LAYER_CLEAR = 255

    MAP2_FLAG_MULTI = 1
    MAP2_FLAG_NAME = 2
    MAP2_FLAG_PROBE = 4
    MAP2_FLAG_HEIGHT = 8
    MAP2_FLAG_ZOOM = 16
    MAP2_FLAG_ALIGN = 32
    MAP2_FLAG_DOUBLE = 64
    MAP2_FLAG_MORE = 128

    MAP2_FLAG2_ALPHA = 1
    MAP2_FLAG2_ROTATE = 2
    MAP2_FLAG2_INFRAVISION = 4
    MAP2_FLAG2_TARGET = 8

    MAP2_FLAG_EXT_ANIM = 1

class ServerCommands:
    CONTROL, \
    ASK_FACE, \
    SETUP, \
    VERSION, \
    REQUEST_FILE, \
    CLEAR, \
    REQUEST_UPDATE, \
    KEEPALIVE, \
    ACCOUNT, \
    ITEM_EXAMINE, \
    ITEM_APPLY, \
    ITEM_MOVE, \
    REPLY, \
    PLAYER_CMD, \
    ITEM_LOCK, \
    ITEM_MARK, \
    FIRE, \
    QUICKSLOT, \
    QUESTLIST, \
    MOVE_PATH, \
    ITEM_READY, \
    TALK, \
    MOVE, \
    TARGET = range(24)

    SETUP_SOUND, \
    SETUP_MAPSIZE, \
    SETUP_BOT, \
    SETUP_SERVER_FILE = range(4)

    ACCOUNT_xxx, \
    ACCOUNT_LOGIN, \
    ACCOUNT_REGISTER, \
    ACCOUNT_LOGIN_CHAR, \
    ACCOUNT_NEW_CHAR, \
    ACCOUNT_PSWD = range(6)

class ClientPlayer(object):
    def __init__(self):
        self.socket_version = 0

class GameObject(object):
    def __init__(self):
        self.x = 0
        self.y = 0
        self.env = None
        self.map = None
        self.inv = []

class MapObject(object):
    def __init__(self):
        self.tiles = {}
        self.xpos = 0
        self.ypos = 0
        self.pos = [0, 0]

    def set_data(self, width, height, xpos, ypos):
        self.xpos = xpos
        self.ypos = ypos
        self.pos = [0, 0]
        self.width = width
        self.height = height

    def mapscroll(self, xoff, yoff, xpos, ypos):
        if xoff:
            step = 1 if xoff > 0 else -1

            for x in range(self.pos[0], self.pos[0] + xoff, step):
                x += 17 / 2 * step + step

                for y in range(self.pos[1] - 17 / 2, self.pos[1] + 17 / 2):
                    self.tile_clear(x, y)

        if yoff:
            step = 1 if yoff > 0 else -1

            for y in range(self.pos[1], self.pos[1] + yoff, step):
                y += 17 / 2 * step + step

                for x in range(self.pos[0] - 17 / 2, self.pos[0] + 17 / 2):
                    self.tile_clear(x, y)

        self.pos[0] += xoff
        self.pos[1] += yoff

        self.xpos = xpos
        self.ypos = ypos

    def tile_clear_layer(self, x, y, layer):
        try:
            self.tiles[x][y][layer] = None
        except KeyError:
            logging.warning("No such layer ({}) on tile: {},{}".format(layer, x, y))

    def tile_clear(self, x, y):
        try:
            self.tiles[x][y].clear()
        except KeyError:
            logging.warning("No such tile: {},{}".format(x, y))

    def tile_update_object(self, x, y, layer, data):
        if not x in self.tiles:
            self.tiles[x] = {}

        if not y in self.tiles[x]:
            self.tiles[x][y] = {}

        if not layer in self.tiles[x][y] or not self.tiles[x][y][layer]:
            self.tiles[x][y][layer] = GameObject()

        for attr in data:
            setattr(self.tiles[x][y][layer], attr, data[attr])

    def render(self, width = 20, height = 20):
        l = [[" " for x in range(width)] for y in range(height)]

        for x2, x in enumerate(range(self.pos[0] - width / 2, self.pos[0] + width / 2)):
            for y2, y in enumerate(range(self.pos[1] - height / 2, self.pos[1] + height / 2)):
                if not x in self.tiles or not y in self.tiles[x]:
                    continue

                for layer in self.tiles[x][y]:
                    obj = self.tiles[x][y][layer]

                    if not obj:
                        continue

                    c = " "

                    if hasattr(obj, "player_name"):
                        c = "P"
                    elif hasattr(obj, "count"):
                        c = "N" if obj.is_friend else "M"
                    elif (layer + 1) % 7 == 5:
                        c = "#"

                    l[y2][x2] = c

        return "\n".join("".join(line) for line in l)

class Client(object):
    ST_INIT, \
    ST_METASERVER, \
    ST_WAITMETASERVER, \
    ST_CHOOSESERVER, \
    ST_CONNECT, \
    ST_WAITCONNECT, \
    ST_VERSION, \
    ST_WAITVERSION, \
    ST_SETUP, \
    ST_WAITSETUP, \
    ST_LOGIN, \
    ST_WAITLOGIN, \
    ST_CHARACTERS, \
    ST_WAITPLAY, \
    ST_PLAY = range(15)

    def __init__(self, screen):
        self.screen = screen

        curses.start_color()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)

        self.screen.bkgd(curses.color_pair(1))
        self.screen.refresh()
        self.screen.nodelay(1)

        self.height, self.width = screen.getmaxyx()

        self.wins = {}
        self.wins["main"] = curses.newwin(self.height - 3, self.width, 0, 0)
        self.wins["status"] = curses.newwin(3, self.width, self.height - 3, 0)

        self.socket_thread = SocketClientThread()
        self.socket_thread.start()

        self.metaserver_thread = MetaserverThread()
        self.metaserver_thread.start()

        self.map = MapObject()

        self.alive = True
        self.state = self.ST_INIT
        self.selection_keys = string.digits[1:] + string.ascii_lowercase

    def show_text(self, text, center = True, win = "main", clear = True, align = None, valign = None):
        if clear:
            self.wins[win].clear()
            self.wins[win].box()

        height, width = self.wins[win].getmaxyx()
        height -= 2
        width -= 2

        if center and not align and not valign:
            align = "center"
            valign = "middle"

        lines = text.split("\n")[-height:]

        if align == "right":
            longest = max(len(line) for line in lines)

        for y, line in enumerate(lines):
            x = 1
            y += 1

            if align == "center":
                x += width // 2 - len(line) // 2
            elif align == "right":
                x += width - longest

            if valign == "middle":
                y += height // 2 - len(lines) // 2
            elif valign == "bottom":
                y += height - len(lines)

            self.wins[win].addstr(y, x, line)

        self.wins[win].refresh()

    def connect(self, server):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.CONNECT, (server["host"], server["port"])))

    def disconnect(self):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.CLOSE, "Disconnected by user"))

    def send_command(self, cmd, data):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.SEND, struct.pack("B", cmd) + data))

    def get_metaservers(self):
        return ["meta.atrinik.org"]

    def show_intro_gfx(self):
        self.show_text(r"""
              v .   ._, |_  .,
           `-._\/  .  \ /    |/_
               \\  _\, y | \//
         _\_.___\\, \\/ -.\||
           `7-,--.`._||  / / ,
           /'     `-. `./ / |/_.'
                     |    |//
                     |_    /
                     |-   |
                     |   =|
                     |    |
                    / ,  . \
        """, align = "right", valign = "top")
        self.show_text(r"""



       _-_
    /~~   ~~\
 /~~         ~~\
{               }
 \  _-     -_  /
   ~  \\ //  ~
_- -   | | _- _
  _ -  | |   -_
      // \\
        """, align = "left", valign = "top", clear = False)

    def show_servers(self):
        self.show_intro_gfx()
        self.show_text("Select server to connect to:\n\n{}".format("\n".join("{}: {}".format(i + 1, server["name"]) for i, server in enumerate(self.servers))), clear = False)

        c = self.screen.getch()

        if c == -1:
            return

        if chr(c) in self.selection_keys:
            idx = self.selection_keys.index(chr(c))

            if idx >= len(self.servers):
                return

            self.server = self.servers[idx]
            self.state += 1

    def show_login(self):
        self.show_intro_gfx()
        self.show_text("Connected to {}.\n1: Login\n2: Register".format(self.server["name"]), clear = False)
        c = self.screen.getch()

        if c == -1:
            return

        if c == ord("1"):
            self.show_intro_gfx()
            self.show_text("Enter your account name:\n", clear = False)
            curses.echo()
            name = self.wins["main"].getstr()
            curses.noecho()
            self.show_intro_gfx()
            self.show_text("Enter your account password:\n", clear = False)
            pswd = self.wins["main"].getstr()

            self.send_command(ServerCommands.ACCOUNT, struct.pack("B", ServerCommands.ACCOUNT_LOGIN) + name + b"\0" + pswd + b"\0")
            self.state += 1
        elif c == ord("2"):
            self.show_intro_gfx()
            self.show_text("Enter new account name:\n", clear = False)
            curses.echo()
            name = self.wins["main"].getstr()
            curses.noecho()
            self.show_intro_gfx()
            self.show_text("Enter password:\n")
            pswd = self.wins["main"].getstr()
            self.show_text("Verify password:\n")
            pswd2 = self.wins["main"].getstr()

            self.send_command(ServerCommands.ACCOUNT, "".join([struct.pack("!B", ServerCommands.ACCOUNT_REGISTER), name, "\0", pswd, "\0", pswd2, "\0"]))
            self.state += 1

    def show_characters(self):
        self.show_intro_gfx()
        self.show_text("Select character:\n\n{}\n(Enter for new)".format("\n".join("{}: {char[name]} ({char[level]})".format(self.selection_keys[i], char = character) for i, character in enumerate(self.characters))), clear = False)
        c = self.screen.getch()

        if c == -1:
            return

        if chr(c) in self.selection_keys:
            idx = self.selection_keys.index(chr(c))

            if idx >= len(self.characters):
                return

            self.send_command(ServerCommands.ACCOUNT, "".join([struct.pack("!B", ServerCommands.ACCOUNT_LOGIN_CHAR), self.characters[idx]["name"], "\0"]))
            self.state += 1

    def loop(self):
        while self.alive:
            while True:
                try:
                    cmd = self.metaserver_thread.reply_q.get_nowait()

                    if cmd.cmd_type == ClientCommand.CLOSE:
                        if cmd.type == ClientReply.ERROR:
                            self.show_text("Failed to connect to metaserver: {}".format(cmd.data.data))
                    elif cmd.cmd_type == ClientCommand.DATA:
                        self.servers = cmd.data
                        self.state += 1
                except queue.Empty as e:
                    break

            while True:
                try:
                    cmd = self.socket_thread.reply_q.get_nowait()

                    if cmd.cmd_type == ClientCommand.CONNECT:
                        self.state += 1
                    elif cmd.cmd_type == ClientCommand.CLOSE:
                        logging.info("Closed due to: {}".format(cmd.data.data))
                    elif cmd.cmd_type == ClientCommand.DATA:
                        fnc = CommandHandler.commands[struct.unpack("B", cmd.data[:1])[0]][1]

                        if fnc:
                            fnc(self, cmd.data[1:])
                        else:
                            logging.warning("Unimplemented command: {}".format(int(struct.unpack("!B", cmd.data[:1])[0])))
                except queue.Empty as e:
                    break

            if self.state == self.ST_INIT:
                self.show_intro_gfx()
                self.show_text("Welcome to Atrinik!\nPlease wait, connecting to the metaserver...", clear = False)
                self.state += 1
            elif self.state == self.ST_METASERVER:
                self.metaserver_thread.cmd_q.put(ClientCommand(ClientCommand.CONNECT, self.get_metaservers()))
                self.state += 1
            elif self.state == self.ST_CHOOSESERVER:
                self.show_servers()
            elif self.state == self.ST_CONNECT:
                self.show_intro_gfx()
                self.show_text("Connecting to {}...".format(self.server["name"]), clear = False)
                self.cpl = ClientPlayer()
                self.connect(self.server)
                self.state += 1
            elif self.state == self.ST_VERSION:
                self.send_command(ServerCommands.VERSION, struct.pack("!L", 1058))
                self.state += 1
            elif self.state == self.ST_SETUP:
                self.send_command(ServerCommands.SETUP, struct.pack("!BB", ServerCommands.SETUP_SOUND, 0))
                self.state += 1
            elif self.state == self.ST_LOGIN:
                self.show_login()
            elif self.state == self.ST_CHARACTERS:
                self.show_characters()
            elif self.state == self.ST_PLAY:
                height, width = self.wins["main"].getmaxyx()
                self.show_text(self.map.render(width = width - 2, height = height))

                c = self.screen.getch()

                if c == curses.KEY_UP:
                    self.send_command(ServerCommands.MOVE, struct.pack("!2B", 1, 0))
                elif c == curses.KEY_DOWN:
                    self.send_command(ServerCommands.MOVE, struct.pack("!2B", 5, 0))
                elif c == curses.KEY_RIGHT:
                    self.send_command(ServerCommands.MOVE, struct.pack("!2B", 3, 0))
                elif c == curses.KEY_LEFT:
                    self.send_command(ServerCommands.MOVE, struct.pack("!2B", 7, 0))

            time.sleep(0.01)

def main(screen):
    logging.basicConfig(filename = "client.log",
                        filemode = "w",
                        level = logging.DEBUG,
                        format = "%(asctime)s.%(msecs).03d %(levelname)8s: %(message)s",
                        datefmt = "%Y-%m-%d %H:%M:%S")
    client = Client(screen)
    client.state = client.ST_INIT
    client.loop()

if __name__ == "__main__":
    curses.wrapper(main)
