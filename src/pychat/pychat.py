#!/usr/bin/env python3

import argparse
import asyncio
import curses
import curses.ascii
import curses.panel
import enum
import json
import ssl
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Self

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
    """Class to house the runtime configuration options."""

    host: str
    port: str
    serve: bool
    ssl: bool
    certfile: str
    cafile: str


class DataType(enum.IntEnum):
    MSG = enum.auto()
    SYS_MSG = enum.auto()


class Events:
    class ServerStarted:
        ...

    class ConnectedToHost:
        ...

    @dataclass
    class NewConnection:
        remote_address: str

    @dataclass
    class LostConnection:
        remote_address: str

    @dataclass
    class IncomingData:
        cid: uuid.UUID
        data: bytes

        @cached_property
        def data_type(self):
            return DataType.from_bytes(self.data[:1], byteorder="big")

    @dataclass
    class OutgoingData:
        type: DataType
        data: bytes
        exclude_ids: set[uuid.UUID]

        def to_bytes(self):
            return self.type.to_bytes(length=1, byteorder="big") + self.data

    @dataclass
    class UserInputSubmitted:
        text: str

    @dataclass
    class Message:
        uid: uuid.UUID
        text: str

        def to_bytes(self):
            return self.uid.bytes + self.text.encode()

        @classmethod
        def from_bytes(cls, b: bytes):
            return cls(uid=uuid.UUID(bytes=b[:16]), text=b[16:].decode())

    # @dataclass
    # class JsonMessage:
    #     json: dict
    #
    #     @classmethod
    #     def from_bytes(cls, b: bytes):
    #         return cls(json=json.loads(b.decode()))
    #
    #     def to_bytes(self) -> bytes:
    #         return json.dumps(self.json).encode()


class EventPubSub:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.subscribers = defaultdict(list)

    def subscribe(self, fn, *event_types):
        for event_type in event_types:
            self.subscribers[event_type].append(fn)

    async def publish(self, event):
        for subscriber in self.subscribers[event.__class__]:
            if asyncio.iscoroutinefunction(subscriber):
                asyncio.Task(subscriber(event))
            else:
                subscriber(event)


events = EventPubSub()


class NetworkConnection:
    """
    Connection represents a single connection between a server/client.

    Each connection gets its own
     - `StreamReader` & `StreamWriter` - to read and write to the connected socket
     - `UUID` to uniquely identify the connection
     - `Queue` for messages that need sent across the wire
    """

    def __init__(self, reader: "StreamReader", writer: "StreamWriter"):
        self.cid = uuid.uuid4()
        self.reader = reader
        self.writer = writer
        self.write_queue = asyncio.Queue[bytes]()
        self.remote_address = writer.get_extra_info("peername", default=["Unknown"])[0]

    def __hash__(self):
        return hash(self.cid)

    async def close(self):
        """Closes the connection."""
        self.writer.close()
        await self.writer.wait_closed()

    async def read_loop(self):
        """Creates an infinite loop to read from the buffer and pushed incoming data to the global MessageManager."""
        while True:
            data_size = await self.reader.readexactly(4)
            if data_size:
                data = await self.reader.readexactly(int.from_bytes(data_size, byteorder="big"))
                await events.publish(Events.IncomingData(cid=self.cid, data=data))
            else:
                raise ConnectionResetError

    async def write_loop(self):
        """Creates an infinite loop to check for data in the Queue and sends it across the wire."""
        while True:
            data = await self.write_queue.get()
            self.writer.write(len(data).to_bytes(length=4, byteorder="big"))
            self.writer.write(data)
            await self.writer.drain()

    async def keep_alive(self):
        """Runs both of the infinite read & write loops."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.read_loop())
                tg.create_task(self.write_loop())
        finally:
            await self.close()

    async def send(self, data: bytes):
        await self.write_queue.put(data)


class Network:
    def __init__(self) -> None:
        self.connections: set[NetworkConnection] = set()
        self.connection_lock = asyncio.Lock()  # lock to access connections

    @classmethod
    def create_ssl_context(cls) -> ssl.SSLContext | None:
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

    async def start_server(self):
        """Starts & runs the server indefinitely adding each new client to `connections`."""
        server = await asyncio.start_server(
            client_connected_cb=self.new_connection,
            host=config.host,
            port=config.port,
            ssl=self.create_ssl_context(),
        )
        async with server:
            await events.publish(Events.ServerStarted())
            await server.serve_forever()

    async def start_client(self):
        """Starts & runs the client indefinitely and adds the connection to the server to `connections`."""
        reader, writer = await asyncio.open_connection(
            host=config.host, port=config.port, ssl=self.create_ssl_context()
        )
        await events.publish(Events.ConnectedToHost())
        await self.new_connection(reader=reader, writer=writer)

    async def start(self):
        """Starts & runs the client or server indefinitely."""
        if config.serve:
            await self.start_server()
        else:
            await self.start_client()

    async def new_connection(self, reader: "StreamReader", writer: "StreamWriter"):
        connection = NetworkConnection(reader=reader, writer=writer)
        await self.add_connection(connection)
        try:
            await connection.keep_alive()
        except* ConnectionResetError:
            pass
        except* asyncio.IncompleteReadError:
            pass
        finally:
            await self.remove_connection(connection)

    async def add_connection(self, c: NetworkConnection):
        async with self.connection_lock:
            self.connections.add(c)
            await events.publish(Events.NewConnection(remote_address=c.remote_address))

    async def remove_connection(self, c: NetworkConnection):
        async with self.connection_lock:
            self.connections.remove(c)
            await events.publish(Events.LostConnection(remote_address=c.remote_address))

    async def send(self, data: bytes, exclude_ids=()):
        async with self.connection_lock:
            for connection in self.connections:
                if connection.cid not in exclude_ids:
                    await connection.send(data)


class BaseUIWidget:
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


class ScrollableTextUIWidget(BaseUIWidget):
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

    def add_msg(self, text: str, uid: uuid.UUID):
        """Adds a new message to show for the user."""
        msg = text.strip()
        # Purge earlier messages if we've reached the max history length
        n_lines = msg.count("\n")
        y, _ = self.pad.getyx()
        if self.n_lines - y <= n_lines:
            self.purge_earliest(n_lines)

        # Show the message by adding it to the pad
        self.pad.addstr(" > " + msg + "\n", self.get_user_color(uid))

        # If the window doesn't currently have focus then go ahead and scroll the window
        # to make the latest message visible
        if not self.focus:
            self.reset_scroll()

        self.refresh()


class InputUIWidget(BaseUIWidget):
    def __init__(self, window, input_submitted_cb, n_lines=128, clear_on_submit=True):
        super().__init__(window, n_lines)
        self.input_submitted_cb = input_submitted_cb
        self.clear_on_submit = clear_on_submit

    async def handle_ch__return(self):
        """Treats the return/enter key-press as if the user were submitting their input.
        It calls the `input_submitted_cb` and empties the widget text and resets scrolling etc."""
        text = self.get_text().strip()
        if asyncio.iscoroutinefunction(self.input_submitted_cb):
            await self.input_submitted_cb(text)
        else:
            self.input_submitted_cb(text)

        if self.clear_on_submit:
            self.pad.erase()
            self.pad.move(0, 0)

    def get_text(self) -> str:
        """Returns all the text in the pad."""
        all_lines = []
        for y in range(0, self.n_lines):
            chars = []
            for x in range(0, self.pad_width - 1):
                chars.append(chr(curses.ascii.ascii(self.pad.inch(y, x))))
            all_lines.append("".join(chars).rstrip())
        return "\n".join(all_lines)

    async def handle_ch(self, ch: int) -> bool:
        """Handles a key-press of `ch` and returns whether the key was handled."""
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
                pass
            self.pad.delch()
        elif ch == curses.KEY_DC:  # delete
            self.pad.delch()
        elif ch == curses.ascii.NL:  # return / enter
            await self.handle_ch__return()
        else:
            return False

        # Scroll the window to follow the cursor (if needed)
        if y < self.scroll:
            self.scroll_up()
        elif y - self.pad_height >= self.scroll:
            self.scroll_down()

        self.refresh()
        return True


class AppUI:
    def __init__(self, stdscr):
        stdscr.nodelay(True)  # Make stdscr.getch() "non-blocking"
        self.stdscr = stdscr

        # Create an area/widget to get user input
        input_win = stdscr.subwin(5, curses.COLS, curses.LINES - 5, 0)
        self.input_widget = InputUIWidget(window=input_win, input_submitted_cb=self.handle_input_submitted)

        # Create an area/widget to show the chat messages
        chat_win = stdscr.subwin(curses.LINES - 5, curses.COLS, 0, 0)
        self.chat_messages_widget = ScrollableTextUIWidget(chat_win)

        # Widgets that can receive "focus" when the user presses the `tab` key
        self.focus_rotation = deque([self.chat_messages_widget, self.input_widget])

    @property
    def focused_widget(self):
        """The widget that currently has focus."""
        return self.focus_rotation[0]

    def rotate_focus(self):
        """Rotate focus to the next widget."""
        self.focus_rotation.rotate(1)
        self.focus_rotation[-1].set_focus(False)
        self.focus_rotation[0].set_focus(True)

    async def start(self):
        """Starts & runs the UI indefinitely."""
        self.stdscr.refresh()

        while True:
            # Give up control for a bit (STRONG correlation between this value and CPU usage)
            await asyncio.sleep(0.01)

            # Check for key-presses and handle them accordingly
            ch = self.stdscr.getch()
            if ch == curses.ERR:  # no key-press
                continue
            elif chr(ch) == "\t":  # `tab` key-press should rotate which widget has focus
                self.rotate_focus()
            else:
                await self.focused_widget.handle_ch(ch)  # pass the key-press to whichever widget has focus

    async def handle_input_submitted(self, text: str):
        await events.publish(Events.UserInputSubmitted(text=text))


async def main_app(stdscr):
    """Launch the PyChat UI and the (server or client)."""

    ui = AppUI(stdscr)
    network = Network()
    user_uid = uuid.uuid4()

    async def on_input_submitted(event: Events.UserInputSubmitted):
        msg = Events.Message(uid=user_uid, text=event.text)
        await events.publish(msg)

    async def on_connection_events(event):
        if isinstance(event, Events.ServerStarted):
            text = f"Server started on {config.host}:{config.port}"
        elif isinstance(event, Events.ConnectedToHost):
            text = f"Connected to server {config.host}:{config.port}"
        elif isinstance(event, Events.NewConnection):
            text = f"New connection: remote_address {event.remote_address}"
        elif isinstance(event, Events.LostConnection):
            text = f"Connection ended: remote_address {event.remote_address}"
        else:
            raise TypeError
        msg = Events.Message(uid=app_uid, text=text)
        await events.publish(msg)

    async def on_message(event: Events.Message):
        ui.chat_messages_widget.add_msg(text=event.text, uid=event.uid)
        if config.serve or event.uid == user_uid:
            dt = DataType.SYS_MSG if event.uid == app_uid else DataType.MSG
            await events.publish(Events.OutgoingData(type=dt, data=event.to_bytes(), exclude_ids={event.uid}))

    async def on_outgoing_data(event: Events.OutgoingData):
        await network.send(event.to_bytes(), exclude_ids=event.exclude_ids)

    async def on_incoming_data(event: Events.IncomingData):
        if event.data_type in (DataType.MSG, DataType.SYS_MSG):
            msg = Events.Message.from_bytes(event.data[1:])
            if config.serve:
                msg.uid = event.cid  # Force a user's id to be the same as the connection id if we are the server
            elif event.data_type == DataType.SYS_MSG:
                msg.uid = app_uid
            await events.publish(msg)

    events.subscribe(on_input_submitted, Events.UserInputSubmitted)
    events.subscribe(on_outgoing_data, Events.OutgoingData)
    events.subscribe(on_incoming_data, Events.IncomingData)
    events.subscribe(on_message, Events.Message)
    events.subscribe(
        on_connection_events,
        Events.ServerStarted,
        Events.ConnectedToHost,
        Events.LostConnection,
    )
    if config.serve:
        events.subscribe(on_connection_events, Events.NewConnection)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ui.start())
        tg.create_task(network.start())


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
    parser.add_argument("--ssl", action="store_true", help="Use secure connection via SSL")
    parser.add_argument("--certfile", action="store", help="Path to SSL certificate", default="./client.pem")
    parser.add_argument("--cafile", action="store", help="Path to SSL certificate authority", default="./rootCA.pem")
    args = parser.parse_args()

    # Create some globals for convenience
    config = RunConfig(**vars(args))  # Global runtime configuration
    app_uid = uuid.uuid4()  # Give pychat a UUID for when we create "system" messages

    # Launch the app
    curses.wrapper(app_launcher)
