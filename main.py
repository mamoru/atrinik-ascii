#! /usr/bin/python3.3
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
import string
import logging

class _Getch:
    """Gets ASCII code of a single character from standard input. Does not echo to the screen. Blocking function! """
    def __init__(self):
        try:
            self.impl = _GetchWindows()
        except ImportError:
            self.impl = _GetchUnix()

    def __call__(self): return ord(self.impl())

class _GetchUnix:
    def __init__(self):
        import tty, sys

    def __call__(self):
        import sys, tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

class _GetchWindows:
    def __init__(self):
        import msvcrt

    def __call__(self):
        import msvcrt
        return msvcrt.getch()


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
    idx = data.find(b"\0")
    s = ""

    if idx != -1:
        s = data[:idx].decode("ascii")
        data = data[idx + 1:]

    return data, s

class CommandHandler:
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

        self.show_text("Select character:\n\n{}\n(Enter for new)".format("\n".join("{}: {char[name]} ({char[level]})".format(self.selection_keys[i], char = character) for i, character in enumerate(self.characters))))

    def handle_command_player(self, data):
        if self.state == self.ST_WAITPLAY:
            self.state += 1

    def handle_command_compressed(self, data):
        # TODO: implement?
        pass

    def handle_command_drawinfo(self, data):
        type, color = struct.unpack("!B6s", data[:7])
        data, msg = data_get_str(data[8:])
        self.show_text(msg)

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

            self.show_text("Connected to {}.\n1: Login\n2: Register".format(self.server["name"]))

    commands = [
        ("Map", None),
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

    def __init__(self):
        self.socket_thread = SocketClientThread()
        self.socket_thread.start()

        self.metaserver_thread = MetaserverThread()
        self.metaserver_thread.start()

        self.alive = True
        self.state = self.ST_INIT
        self.selection_keys = string.digits[1:] + string.ascii_lowercase

    def show_text(self, text):
        print(text)

    def connect(self, server):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.CONNECT, (server["host"], server["port"])))

    def disconnect(self):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.CLOSE, "Disconnected by user"))

    def send_command(self, cmd, data):
        self.socket_thread.cmd_q.put(ClientCommand(ClientCommand.SEND, struct.pack("B", cmd) + data))

    def get_metaservers(self):
        return ["meta.atrinik.org"]

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
                        self.show_text("Select server to connect to:\n\n{}".format("\n".join("{}: {}".format(i + 1, server["name"]) for i, server in enumerate(self.servers))))
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
                self.show_text("Welcome to Atrinik!\nPlease wait, connecting to the metaserver...")
                self.state += 1
            elif self.state == self.ST_METASERVER:
                self.metaserver_thread.cmd_q.put(ClientCommand(ClientCommand.CONNECT, self.get_metaservers()))
                self.state += 1
            elif self.state == self.ST_CHOOSESERVER:
                c = _Getch()()

                if c != -1 and chr(c) in self.selection_keys:
                    idx = self.selection_keys.index(chr(c))

                    if idx < len(self.servers):
                        self.server = self.servers[idx]
                        self.state += 1
            elif self.state == self.ST_CONNECT:
                self.show_text("Connecting to {}...".format(self.server["name"]))
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
                c = _Getch()()

                if c == ord("1"):
                    acct = input("Enter your account name:\n").encode('ascii', 'ignore')
                    pswd = input("Enter your account password:\n").encode('ascii', 'ignore')

                    self.send_command(ServerCommands.ACCOUNT, struct.pack("B", ServerCommands.ACCOUNT_LOGIN) + acct + b"\0" + pswd + b"\0")
                    self.state += 1
                elif c == ord("2"):
                    acct = input("Enter new account name:\n").encode('ascii', 'ignore')
                    pswd = input("Enter password:\n").encode('ascii', 'ignore')
                    pswd2 = input("Verify password:\n").encode('ascii', 'ignore')

                    self.send_command(ServerCommands.ACCOUNT, "".join([struct.pack("!B", ServerCommands.ACCOUNT_REGISTER), acct, "\0", pswd, "\0", pswd2, "\0"]))
                    self.state += 1
            elif self.state == self.ST_CHARACTERS:
                c = _Getch()()

                if c != -1 and chr(c) in self.selection_keys:
                    idx = self.selection_keys.index(chr(c))

                    if idx < len(self.characters):
                        self.send_command(ServerCommands.ACCOUNT, struct.pack("!B", ServerCommands.ACCOUNT_LOGIN_CHAR) + self.characters[idx]["name"].encode("ascii"))
                        self.state += 1
            elif self.state == self.ST_PLAY:
                print("success!")
                exit()

            time.sleep(0.01)


def main():
    logging.basicConfig(filename = "client.log",
                        filemode = "w",
                        level = logging.DEBUG,
                        format = "%(asctime)s.%(msecs).03d %(levelname)8s: %(message)s",
                        datefmt = "%Y-%m-%d %H:%M:%S")
    client = Client()
    client.state = client.ST_INIT
    client.loop()

if __name__ == "__main__":
    main()
