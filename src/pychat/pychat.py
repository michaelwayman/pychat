#!/usr/bin/env python3

import argparse
import asyncio
import curses
import curses.ascii
import curses.panel
import enum
import ssl
import uuid
from collections import deque
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


def debug():
    """Starts a pdb set_trace after turning off the curses features that would otherwise prevent standard debugging."""
    import pdb

    curses.echo()
    curses.nocbreak()
    curses.endwin()
    pdb.set_trace()


@dataclass
class RunConfig:
    """Class to house the run-time configuration options."""
    host: str
    port: str
    serve: bool
    ssl: bool
    certfile: str
    cafile: str
    debug: bool


class MsgType(enum.IntEnum):
    MSG = 0
    INFO = 1


@dataclass
class ChatMsg:
    type: MsgType
    uid: uuid.UUID
    data: str

    def to_bytes(self):
        return self.type.to_bytes() + self.uid.bytes + self.data.encode()

    @classmethod
    def from_bytes(cls, b: bytes, uid_override=None):
        type = b[:1]
        if uid_override is None:
            uid_override = uuid.UUID(bytes=b[1:17])
        data = b[17:].decode()
        return cls(type=MsgType.from_bytes(type), uid=uid_override, data=data)


class MessageManager:
    def __init__(self):
        self.messages = asyncio.Queue[ChatMsg]()

    async def new_system_message(self, msg: str):
        await self.messages.put(ChatMsg(uid=app_uid, type=MsgType.MSG, data=msg))

    async def new_user_message(self, msg: str):
        cm = ChatMsg(uid=user_uid, type=MsgType.MSG, data=msg)
        await self.messages.put(cm)
        await connections.send_msg(cm.to_bytes())

    async def new_inbound_message(self, data: bytes, uid_override: uuid.UUID | None):
        cm = ChatMsg.from_bytes(b=data, uid_override=uid_override)
        if cm.type == MsgType.MSG:
            await self.messages.put(cm)
            if config.serve:
                await connections.send_msg(data, exclude_ids={cm.uid})

    def get_message(self) -> ChatMsg | None:
        try:
            return self.messages.get_nowait()
        except asyncio.QueueEmpty:
            return None


class Connection:
    def __init__(self, reader: "StreamReader", writer: "StreamWriter"):
        self.id = uuid.uuid4()
        self.reader = reader
        self.writer = writer
        self.write_queue = asyncio.Queue[bytes]()
        self.forced_uid = self.id if config.serve else None
        self.remote_address = writer.get_extra_info("peername", default=["Unknown"])[0]

    def __hash__(self):
        return hash(self.id)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()

    async def read_loop(self):
        while True:
            data = await self.reader.read(1024)
            if data:
                await messages.new_inbound_message(data=data, uid_override=self.forced_uid)
            else:
                raise ConnectionResetError

    async def write_loop(self):
        while True:
            data = await self.write_queue.get()
            self.writer.write(data)
            await self.writer.drain()

    async def serve_forever(self):
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.read_loop())
                tg.create_task(self.write_loop())
        finally:
            await self.close()

    async def send(self, data: bytes):
        await self.write_queue.put(data)


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: set[Connection] = set()
        self._lock = asyncio.Lock()  # lock to access connections

    async def new_connection(self, reader: "StreamReader", writer: "StreamWriter"):
        c = Connection(reader=reader, writer=writer)
        await self.add(c)
        try:
            await c.serve_forever()
        except* ConnectionResetError:
            pass
        finally:
            await self.remove(c)

    async def add(self, c: Connection):
        async with self._lock:
            self.connections.add(c)
            await messages.new_system_message(f"New connection: remote_address {c.remote_address}")

    async def remove(self, c: Connection):
        async with self._lock:
            self.connections.remove(c)
            await messages.new_system_message(f"Connection ended: remote_address {c.remote_address}")

    async def send_msg(self, data: bytes, exclude_ids=()):
        async with self._lock:
            for connection in self.connections:
                if connection.id not in exclude_ids:
                    await connection.send(data)


class BaseWindow:
    def __init__(self, w, n_lines=128):
        self.background = w
        self.height, self.width = w.getmaxyx()
        self.beg_y, self.beg_x = w.getbegyx()
        self.pad_height = self.height - 2
        self.pad_width = self.width - 2
        self.pad = curses.newpad(n_lines, self.pad_width)
        self.pad_beg_y, self.pad_beg_x = self.beg_y + 1, self.beg_x + 1
        self.pad_end_y, self.pad_end_x = self.beg_y + self.pad_height, self.beg_x + self.pad_width
        self.scroll = 0
        self.focus = False
        self.n_lines = n_lines
        self.refresh()

    def refresh(self):
        # Upper left corner of pad
        # Upper left corner of window area to fill
        # Lower right corner of window area to fill
        self.pad.refresh(self.scroll, 0, self.pad_beg_y, self.pad_beg_x, self.pad_end_y, self.pad_end_x)

    def scroll_up(self):
        self.scroll = max(0, self.scroll - 1)
        self.refresh()

    def scroll_down(self):
        self.scroll = min(self.n_lines, self.scroll + 1)
        self.refresh()

    def reset_scroll(self):
        """Scroll the window to wherever the cursor is."""
        y, _ = self.pad.getyx()
        self.scroll = max(0, y - self.pad_height + 1)
        self.refresh()

    def purge_earliest(self, n: int):
        """Removes the first n lines of text from the pad."""
        y, _ = self.pad.getyx()
        self.pad.move(0, 0)
        for i in range(n):
            self.pad.deleteln()
        self.pad.move(y - n, 0)

    async def handle_ch(self, ch: int) -> bool:
        if ch == curses.KEY_UP:
            self.scroll_up()
        elif ch == curses.KEY_DOWN:
            self.scroll_down()
        else:
            return False
        return True

    def set_focus(self, focus):
        """Set the focus of the window."""
        if focus == self.focus:
            return

        if focus:
            self.background.border()  # Add border
        else:
            self.background.clear()  # Remove border
            self.reset_scroll()
        self.background.refresh()
        self.refresh()
        self.focus = focus


class ChatWindow(BaseWindow):
    def __init__(self, stdscr, lines=128):
        super().__init__(stdscr, lines)
        # Init curses color pairs
        curses.init_color(curses.COLOR_WHITE, 1000, 1000, 1000)
        curses.init_pair(1, curses.COLOR_RED, curses.COLOR_WHITE)
        curses.init_pair(2, curses.COLOR_BLUE, curses.COLOR_WHITE)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(4, curses.COLOR_MAGENTA, curses.COLOR_WHITE)
        curses.init_pair(5, curses.COLOR_GREEN, curses.COLOR_WHITE)
        curses.use_default_colors()

        # Maps a UUID -> a specific color so the same user shows as the same color each time
        self.color_map = {
            app_uid: curses.color_pair(0),
        }

    @cached_property
    def next_color(self):
        """A generator of color pairs"""
        for n in range(1, 6):
            yield curses.color_pair(n)

    def get_user_color(self, uid: uuid.UUID):
        """Returns the color to use for the given uid."""
        if uid not in self.color_map:
            self.color_map[uid] = next(self.next_color)
        return self.color_map[uid]

    def add_msg(self, cm: ChatMsg):
        """Adds a new message to show for the user."""
        msg = cm.data.strip()

        # Purge earlier messages if we've reached the max history length
        n_lines = msg.count("\n")
        y, _ = self.pad.getyx()
        if self.n_lines - y <= n_lines:
            self.purge_earliest(n_lines)

        # Show the message by adding it to the pad
        self.pad.addstr(" > " + msg + "\n", self.get_user_color(cm.uid))

        # If the window doesn't currently have focus then go ahead and scroll the window
        # to make the latest message visible
        if not self.focus:
            self.reset_scroll()

        self.refresh()


class InputWindow(BaseWindow):
    def __init__(self, window, n_lines=128):
        super().__init__(window, n_lines)

    async def on_input_submit(self):
        """Callback for when the user presses enter, passes the text to the global MessageManager."""
        text = self.get_pad_text()
        text = text.strip()
        await messages.new_user_message(text)

    def get_pad_text(self) -> str:
        """Returns all the text in the pad."""
        all_lines = []
        for y in range(0, self.n_lines):
            chars = []
            for x in range(0, self.pad_width - 1):
                chars.append(chr(curses.ascii.ascii(self.pad.inch(y, x))))
            all_lines.append("".join(chars).rstrip())
        return "\n".join(all_lines)

    async def handle_ch(self, ch: int) -> bool:
        """Handle a key-press of `ch` and return whether the key was handled."""
        y, x = self.pad.getyx()
        if curses.ascii.isprint(ch):
            self.pad.addch(ch)
        elif ch == curses.KEY_UP:
            if y > 0:
                self.pad.move(y - 1, x)
        elif ch == curses.KEY_DOWN:
            if y + 1 < self.n_lines:
                self.pad.move(y + 1, x)
        elif ch == curses.KEY_LEFT:
            if x > 0:
                self.pad.move(y, x - 1)
            elif x == 0 and y > 0:
                self.pad.move(y - 1, self.pad_width - 1)
        elif ch == curses.KEY_RIGHT:
            if x + 1 < self.pad_width:
                self.pad.move(y, x + 1)
            elif x + 1 == self.pad_width and y < self.n_lines:
                self.pad.move(y + 1, 0)
        elif ch in (curses.ascii.BS, curses.KEY_BACKSPACE, curses.ascii.DEL):  # backspace
            if x > 0:
                self.pad.move(y, x - 1)
            elif y == 0:
                return True
            self.pad.delch()
        elif ch == curses.KEY_DC:  # delete
            self.pad.delch()
        elif ch == curses.ascii.NL:  # return
            await self.on_input_submit()
            self.pad.erase()
            self.pad.move(0, 0)
        else:
            if config.debug:
                await messages.new_system_message(f"Key not handled - {ch} -- {curses.keyname(ch).decode()}")
            return False

        # Scroll the window to follow the cursor (if needed)
        if y < self.scroll:
            self.scroll_up()
        elif y - self.pad_height >= self.scroll:
            self.scroll_down()

        self.refresh()
        return True


class ChatUI:
    def __init__(self, stdscr):
        stdscr.nodelay(True)  # Make stdscr.getch() "non-blocking"
        self.stdscr = stdscr

        # Create our "input window" to get user input
        input_win = stdscr.subwin(5, curses.COLS, curses.LINES - 5, 0)
        self.input_window = InputWindow(input_win)

        # Create our "chat window" to show the chat messages
        chat_win = stdscr.subwin(curses.LINES - 5, curses.COLS, 0, 0)
        self.chat_window = ChatWindow(chat_win)

        self.focus_rotation = deque([self.chat_window, self.input_window])  # Focusable windows

    @property
    def focused_window(self):
        """The window that currently has focus."""
        return self.focus_rotation[0]

    def rotate_focus(self):
        """Rotate the window focus to the next window."""
        self.focus_rotation.rotate(1)
        self.focus_rotation[-1].set_focus(False)
        self.focus_rotation[0].set_focus(True)

    async def run(self):
        """Runs the infinite UI loop."""
        self.stdscr.refresh()

        while True:
            # Give up control for a bit (STRONG correlation between this value and CPU usage)
            await asyncio.sleep(0.01)

            # Check the global message queue for any messages that need to appear in the chat window
            if cm := messages.get_message():  # TODO: Put this into its own thing
                self.chat_window.add_msg(cm)

            # Check for key-presses and handle them accordingly
            ch = self.stdscr.getch()
            if ch == curses.ERR:  # no key-press
                continue
            elif chr(ch) == "\t":  # `tab` key-press should rotate which window has focus
                self.rotate_focus()
            else:
                await self.focused_window.handle_ch(ch)  # pass the key-press to whichever window has focus


def create_ssl_context() -> ssl.SSLContext | None:
    """Returns an SSL context to use based on the global RunConfig or None."""
    if not config.ssl:
        return None

    if config.serve:
        protocol = ssl.PROTOCOL_TLS_SERVER
        default_certs = ssl.Purpose.CLIENT_AUTH
    else:
        protocol = ssl.PROTOCOL_TLS_CLIENT
        default_certs = ssl.Purpose.SERVER_AUTH

    c = ssl.SSLContext(protocol)
    c.load_cert_chain(certfile=config.certfile)
    c.load_verify_locations(cafile=config.cafile)
    c.load_default_certs(default_certs)
    c.verify_mode = ssl.CERT_REQUIRED
    return c


async def run_server():
    """Starts the server and adds each incoming client connection to the global ConnectionManager."""
    async def on_client_connected(reader: "StreamReader", writer: "StreamWriter"):
        await connections.new_connection(reader=reader, writer=writer)

    server = await asyncio.start_server(
        client_connected_cb=on_client_connected,
        host=config.host,
        port=config.port,
        ssl=create_ssl_context(),
    )
    async with server:
        await messages.new_system_message("Server started...")
        await server.serve_forever()


async def run_client():
    """Opens a client connection to the server and adds it to the global ConnectionManager."""
    reader, writer = await asyncio.open_connection(host=config.host, port=config.port, ssl=create_ssl_context())
    await messages.new_system_message(f"Connected to server {config.host}")
    await connections.new_connection(reader=reader, writer=writer)


async def main_app(stdscr):
    """Launch the PyChat UI and the (server or client)."""
    ui = ChatUI(stdscr)
    run_side = run_server if config.serve else run_client

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ui.run())
        tg.create_task(run_side())


def app_launcher(stdscr):
    """Launch the asyncio app."""
    try:
        asyncio.run(main_app(stdscr))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # Get command-line run options
    parser = argparse.ArgumentParser(description="PyChat :)")
    parser.add_argument("-H", "--host", action="store", help="Host of sever", default="0.0.0.0")
    parser.add_argument("-P", "--port", action="store", help="Port of sever", default="8080")
    parser.add_argument("-s", "--serve", action="store_true", help="Run the chat server for others to connect")
    parser.add_argument("--debug", action="store_true", help="Turn on debug mode")
    parser.add_argument("--ssl", action="store_true", help="Use secure connection via SSL")
    parser.add_argument("--certfile", action="store", help="Path to SSL certificate", default="./client.pem")
    parser.add_argument("--cafile", action="store", help="Path to SSL certificate authority", default="./rootCA.pem")
    args = parser.parse_args()

    # Create some globals for convenience
    config = RunConfig(**vars(args))
    app_uid = uuid.uuid4()
    user_uid = uuid.uuid4()
    messages = MessageManager()
    connections = ConnectionManager()

    # Launch the app
    curses.wrapper(app_launcher)
