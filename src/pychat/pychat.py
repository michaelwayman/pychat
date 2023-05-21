#!/usr/bin/env python3

import argparse
import asyncio
import atexit
import contextlib
import curses
import curses.ascii
import curses.panel
import dataclasses
import enum
import itertools
import json
import ssl
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


def debug():
    """Helps with debugging while curses app is running."""
    import pdb

    curses.echo()
    curses.nocbreak()
    curses.endwin()
    pdb.set_trace()


class JsonEncoder(json.JSONEncoder):
    def default(self, o):
        """Help convert objects for serialization"""
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


class JsonDecoder(json.JSONDecoder):
    def __init__(self, **kwargs):
        super().__init__(object_hook=self.object_hook, **kwargs)

    def object_hook(self, d: dict):
        new_d = {}
        for k, v in d.items():
            if isinstance(k, str):
                with contextlib.suppress(ValueError):
                    k = uuid.UUID(k)
            if isinstance(v, str):
                with contextlib.suppress(ValueError):
                    v = uuid.UUID(v)
            elif isinstance(v, dict):
                v = self.object_hook(v)

            new_d[k] = v

        return new_d


def dict_factory(*args, **kwargs):
    """Returns a dictionary where every key has been make a string."""
    seq = args[0] if args else []
    result = []
    for key, value in itertools.chain(seq, kwargs.items()):
        key = str(key)
        if isinstance(value, dict):
            value = {str(k): v for k, v in value.items()}
        result.append((key, value))
    return dict(result)


@dataclass
class RunConfig:
    """Represents the runtime configuration options."""

    host: str
    port: str
    username: str
    color: str
    serve: bool
    ssl: bool
    certfile: str
    cafile: str


@dataclass
class JsonTransferable:
    """Base class for dataclasses that can be sent across the wire as json bytes."""

    _subclasses: ClassVar[dict] = {}  # Maps class names to the class for all subclasses

    def __init_subclass__(cls, **kwargs):
        JsonTransferable._subclasses[cls.__name__] = cls

    @classmethod
    def from_json(cls, json_data: dict):
        type = json_data.pop("type")
        obj_class = cls._subclasses[type]
        return obj_class(**json_data)

    def __post_init__(self):
        """
        Attempts some basic type coercion to recreate the expected types of the dataclass.

        Useful when going from JSON back to object instances (I chose not to use pickle)
        """
        for field in dataclasses.fields(self):
            val = getattr(self, field.name)
            if val and isinstance(val, dict):
                if dataclasses.is_dataclass(field.type):
                    setattr(self, field.name, field.type(**val))
                elif type_args := getattr(field.type, "__args__", None):
                    if len(type_args) == 2 and dataclasses.is_dataclass(type_args[1]):
                        for k, v in val.items():
                            if isinstance(v, dict):
                                val[k] = type_args[1](**v)

    def to_bytes(self):
        """Returns the instance as json bytes, but also adds a new key 'type'."""
        as_dict = dataclasses.asdict(self, dict_factory=dict_factory)
        as_dict["type"] = self.__class__.__name__
        as_str = json.dumps(as_dict, cls=JsonEncoder)
        as_bytes = as_str.encode()
        return as_bytes


@dataclass
class User(JsonTransferable):
    """Represents a user in the app."""

    uid: uuid.UUID
    username: str
    color: str

    def __post_init__(self):
        super().__post_init__()
        self.color = UIColors.clean_color(self.color)


@dataclass
class JoinRequest(JsonTransferable):
    """Once a client connects to the server, it sends one of these to get added as a new User."""

    username: str
    color: str


@dataclass
class ServerInfo(JsonTransferable):
    """Some data about the chat, the connected users, their preferred color, etc."""

    users: dict[uuid.UUID, User]
    uid: uuid.UUID  # UUID assigned to the user receiving this obj


@dataclass
class ChatMessage(JsonTransferable):
    """Represents a user-typed message that sends and displays to everyone."""

    uid: uuid.UUID
    text: str


@dataclass
class SystemMessage(JsonTransferable):
    """A message from the "app", when someone joins or leaves, connection established, etc."""

    text: str


class SendDataType(enum.IntEnum):
    """Type of data being sent across the wire."""

    JSON = enum.auto()


@dataclass
class SendData:
    """This object wraps the data that we plan to send across the wire."""

    type: SendDataType
    data: bytes

    def to_bytes(self):
        return self.type.to_bytes(length=1, byteorder="big") + self.data

    @classmethod
    def from_json_transferable(cls, dc):
        return cls(type=SendDataType.JSON, data=dc.to_bytes())


class Events:
    """
    This is a pub/sub events model.

     - Register event handlers to be called when events get pushed.
     - Push events that happen

     Due to this app being a single-file, I've namespaced all pushable events to this class.
    """

    def __init__(self):
        self.handlers = defaultdict(list)
        self.handlers__client = defaultdict(list)
        self.handlers__server = defaultdict(list)

    def subscribe(self, fn, *event_types, server_only=False, client_only=False) -> None:
        """
        Add callable `fn` as a handler for the specified event types.

        server_only: Only call this handler when running the server
        client_only: Only call this handler when running as the client
        """
        if server_only:
            handlers = self.handlers__server
        elif client_only:
            handlers = self.handlers__client
        else:
            handlers = self.handlers
        for event_type in event_types:
            handlers[event_type].append(fn)

    def register(self, *event_types, server_only=False, client_only=False):
        """Decorator version of subscribe."""

        def wrapper(fn):
            self.subscribe(fn, *event_types, server_only=server_only, client_only=client_only)
            return fn

        return wrapper

    def get_event_handlers(self, event):
        """Returns the event handlers as an iterable."""
        if config.serve:
            specific_handlers = self.handlers__server
        else:
            specific_handlers = self.handlers__client

        return itertools.chain(self.handlers[event.__class__], specific_handlers[event.__class__])

    async def push(self, event):
        """Call all the handlers for the given event."""
        for handler in self.get_event_handlers(event):
            if asyncio.iscoroutinefunction(handler):
                asyncio.Task(handler(event))
            else:
                handler(event)

    class ServerStarted:
        """The server successfully started."""

    class ConnectedToHost:
        """The client successfully connected to the server."""

    @dataclass
    class EstablishedConnection:
        """A new connection has been established and added to the network."""

        cid: uuid.UUID
        remote_address: str

    @dataclass
    class LostConnection:
        """A connection has been lost and removed from the network."""

        cid: uuid.UUID
        remote_address: str

    @dataclass
    class ReceivedData:
        """Data has been received from the wire."""

        cid: uuid.UUID
        data: bytes

        @cached_property
        def data_type(self):
            return SendDataType.from_bytes(self.data[:1], byteorder="big")

        def json(self):
            as_str = self.data[1:].decode()
            as_dict = json.loads(as_str, cls=JsonDecoder)
            return as_dict

    @dataclass
    class UserInputSubmitted:
        """The user has submitted some text input."""

        text: str


events = Events()


class NetworkConnection:
    """
    Represents a single connection between a server/client.

    Each connection gets its own
     - `StreamReader` & `StreamWriter` - to read and write to the connected socket
     - `UUID` to uniquely identify the connection
     - `Queue` for messages that need sent across the wire
    """

    # The exact number of bytes used to communicate the content-length of a message
    CONTENT_LENGTH_BYTES = 4

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

    async def recv_loop(self):
        """Creates an infinite loop to read from the buffer and pushed incoming data to the global MessageManager."""
        while True:
            # Determine how big the incoming data is
            data_size = await self.reader.readexactly(self.CONTENT_LENGTH_BYTES)
            if data_size:
                # Read the exact amount of data to receive the completed data
                data = await self.reader.readexactly(int.from_bytes(data_size, byteorder="big"))
                await events.push(Events.ReceivedData(cid=self.cid, data=data))
            else:
                raise ConnectionResetError

    async def send_loop(self):
        """Creates an infinite loop to read from the write Queue and send it across the wire."""
        while True:
            data = await self.write_queue.get()
            # Send the content-length of the message
            self.writer.write(len(data).to_bytes(length=self.CONTENT_LENGTH_BYTES, byteorder="big"))
            # Send the message
            self.writer.write(data)
            await self.writer.drain()

    async def keep_alive(self):
        """Runs both of the infinite send & receive loops."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.recv_loop())
                tg.create_task(self.send_loop())
        finally:
            await self.close()

    async def send(self, data: bytes):
        """Adds `data` to the write queue."""
        await self.write_queue.put(data)


class Network:
    """
    Handles all network related stuff for the app.
     - Manages each individual connection
     - Starts/runs the server
     - Starts/runs the client
     - Sending/receiving data
     - SSL
    """

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

    async def run_server_forever(self):
        """Starts & runs the server indefinitely adding each new client to `connections`."""
        server = await asyncio.start_server(
            client_connected_cb=self.manage_connection_forever,
            host=config.host,
            port=config.port,
            ssl=self.create_ssl_context(),
        )
        async with server:
            await events.push(Events.ServerStarted())
            await server.serve_forever()

    async def run_client_forever(self):
        """Starts & runs the client indefinitely and adds the connection to the server to `connections`."""
        reader, writer = await asyncio.open_connection(
            host=config.host, port=config.port, ssl=self.create_ssl_context()
        )
        await events.push(Events.ConnectedToHost())
        await self.manage_connection_forever(reader=reader, writer=writer)

    async def run_forever(self):
        """Starts & runs the client or server indefinitely."""
        if config.serve:
            await self.run_server_forever()
        else:
            await self.run_client_forever()

    async def manage_connection_forever(self, reader: "StreamReader", writer: "StreamWriter"):
        """Tells the Network class to manage the connection represented by the given streams."""
        connection = NetworkConnection(reader=reader, writer=writer)
        await self._add_connection(connection)
        try:
            await connection.keep_alive()
        except* ConnectionResetError:
            pass
        except* asyncio.IncompleteReadError:
            pass
        finally:
            await self._remove_connection(connection)

    async def _add_connection(self, c: NetworkConnection):
        """Add a new connection to the set of connections."""
        async with self.connection_lock:
            self.connections.add(c)
            await events.push(Events.EstablishedConnection(remote_address=c.remote_address, cid=c.cid))

    async def _remove_connection(self, c: NetworkConnection):
        """Removes a connection from the set of connections."""
        async with self.connection_lock:
            self.connections.remove(c)
            await events.push(Events.LostConnection(remote_address=c.remote_address, cid=c.cid))

    async def send(self, data: bytes, exclude: set[uuid.UUID] | None = None, include: set[uuid.UUID] | None = None):
        """
        Sends the given bytes to each of the connections managed by the network.

        include: Only send the data to the provided connection ids
        exclude: Send the data to all connections excluding the provided connection ids
        """
        async with self.connection_lock:
            for connection in self.connections:
                if include and connection.cid not in include:
                    continue
                if exclude and connection.cid in exclude:
                    continue
                await connection.send(data)


class BaseUIWidget:
    """
    Establishes some base functionality for creating other widgets.

    Features include:
     - Giving "focus" (when in focus widget has a padded border)
     - Scrolling
    """

    def __init__(self, window, n_scrollable_lines: int = 128) -> None:
        """
        window: the window to draw this widget in (widget will assume full size of window)
        n_scrollable_lines: the number of scrollable lines (internal pad height)
        """
        self.background = window
        self.height, self.width = window.getmaxyx()
        self.beg_y, self.beg_x = window.getbegyx()
        self.pad_height = self.height - 2
        self.pad_width = self.width - 2
        self.pad = curses.newpad(n_scrollable_lines, self.pad_width)
        self.pad_beg_y, self.pad_beg_x = self.beg_y + 1, self.beg_x + 1
        self.pad_end_y, self.pad_end_x = self.beg_y + self.pad_height, self.beg_x + self.pad_width
        self.scroll = 0
        self.focus = False
        self.n_scrollable_lines = n_scrollable_lines
        self.refresh()

    def refresh(self) -> None:
        """Refresh the visible area with the content that should be displayed."""
        # Upper left corner of pad
        # Upper left corner of window area to fill
        # Lower right corner of window area to fill
        self.pad.refresh(self.scroll, 0, self.pad_beg_y, self.pad_beg_x, self.pad_end_y, self.pad_end_x)

    def scroll_up(self) -> None:
        self.scroll = max(0, self.scroll - 1)
        self.refresh()

    def scroll_down(self) -> None:
        self.scroll = min(self.n_scrollable_lines - self.pad_height, self.scroll + 1)
        self.refresh()

    def reset_scroll(self) -> None:
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
        """
        Make the widget "respond" to the `ch` character event and return a bool whether the ch was handled.

        ch: represents a keypress
        """
        if ch == curses.KEY_UP:
            self.scroll_up()
        elif ch == curses.KEY_DOWN:
            self.scroll_down()
        else:
            return False
        return True

    def set_focus(self, focus: bool) -> None:
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
    """Widget to display text and allow scrolling."""

    def __init__(self, window, n_scrollable_lines: int = 128) -> None:
        """
        window: the window to draw this widget in (widget will assume full size of window)
        n_scrollable_lines: the number of scrollable lines (internal pad height)
        """
        super().__init__(window, n_scrollable_lines)

    def append_text(self, text: str, color_pair):
        """
        Appends the given text to display with the given colors.

         - new text will erase older text FIFO style if all n_scrollable_lines are used up
         - if this widget is NOT in focus, appending text will move the visible area to the cursor so the new text
           becomes visible
        """
        # Purge earlier text if we've reached the max scrollable lines
        text = text + "\n"
        new_lines = text.count("\n")
        y, _ = self.pad.getyx()
        if self.n_scrollable_lines - y <= new_lines:
            self.purge_earliest(new_lines)

        # Append the text by adding it to the pad
        self.pad.addstr(text, color_pair)

        # If the window doesn't currently have focus then scroll the window the newly added text
        if not self.focus:
            self.reset_scroll()
        self.refresh()


class InputUIWidget(BaseUIWidget):
    """Widget to get multi-line scrollable input from the user."""

    def __init__(self, window, input_submitted_cb, n_scrollable_lines: int = 128, clear_on_submit: bool = True):
        """
        window: the window to draw this widget in (widget will assume full size of window)
        n_scrollable_lines: the number of scrollable lines (internal pad height)
        input_submitted_cb: callback function for when input is submitted
        clear_on_submit: whether to wipe the pad clean when input is submitted
        """
        super().__init__(window, n_scrollable_lines)
        self.input_submitted_cb = input_submitted_cb
        self.clear_on_submit = clear_on_submit

    async def handle_ch__return(self):
        """Handles the return/enter keypress."""
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
        for y in range(0, self.n_scrollable_lines):
            chars = []
            for x in range(0, self.pad_width - 1):
                chars.append(chr(curses.ascii.ascii(self.pad.inch(y, x))))
            all_lines.append("".join(chars).rstrip())
        return "\n".join(all_lines)

    async def handle_ch(self, ch: int) -> bool:
        """Handles a keypress of `ch` and returns whether the key was handled."""
        y, x = self.pad.getyx()
        if curses.ascii.isprint(ch):
            self.pad.addch(ch)
        elif ch == curses.KEY_UP:
            if y > 0:
                self.pad.move(y - 1, x)
        elif ch == curses.KEY_DOWN:
            if y + 1 < self.n_scrollable_lines:
                self.pad.move(y + 1, x)
        elif ch == curses.KEY_LEFT:
            if x > 0:
                self.pad.move(y, x - 1)
            elif x == 0 and y > 0:
                self.pad.move(y - 1, self.pad_width - 1)
        elif ch == curses.KEY_RIGHT:
            if x + 1 < self.pad_width:
                self.pad.move(y, x + 1)
            elif x + 1 == self.pad_width and y < self.n_scrollable_lines:
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
        y, x = self.pad.getyx()
        if y < self.scroll:
            self.scroll_up()
        elif y - self.pad_height >= self.scroll:
            self.scroll_down()

        self.refresh()
        return True


class UIColors:
    """
    Class to register and initialize colors and color pairs in the terminal.

    Curses uses numbers to keep track of registered colors.
    Curses uses color pairs, foreground and background, when displaying text (also tracked as a number).
    Therefore, to use colors correctly we need to track color numbers and color-pair numbers.

    By default, curses already has 8 colors registered (0-7), so when we create new colors and assign them a number
    we start counting at 8. The system only allows up to curses.COLORS (int) colors. (xterm-256 == 256 colors)
    By default, curses already has 1 color-pair registered 0, when we create new pairs, we start counting at 1.
    The system only allows up tp curses.COLOR_PAIRS (int) color-pairs.
    """

    def __init__(self):
        self._color_number = iter(range(8, curses.COLORS))
        self._pair_number = iter(range(1, curses.COLOR_PAIRS))
        self.colors = {}
        self.color_pairs = {}

    @classmethod
    def clean_color(cls, color: str):
        """
        Normalizes the color string.

        "#CCff33" -> "ccff33"
        """
        color = color.lower()
        if color[0] == "#":
            return color[1:]
        return color

    @classmethod
    def hex_to_curses_tuple(cls, color: str):
        """
        Converts a hex color string to a curses rgb tuple.

        Curses registers new colors on an RGB (0-1000) scale. I don't know why, and I didn't take the time to ask yet.
        So as an example we need to do this
        "ff00ff" -> (1000, 0, 1000)
        "cccccc" -> (800, 800, 800)
        """
        color = cls.clean_color(color)
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        curses_ratio = 1000 / 255
        r, g, b = int(r * curses_ratio), int(g * curses_ratio), int(b * curses_ratio)
        return r, g, b

    def get_color(self, color: str) -> int:
        """Returns the number for the given color (registers the color if needed)."""
        color = self.clean_color(color)
        if color not in self.colors:
            color_number = next(self._color_number)
            curses.init_color(color_number, *self.hex_to_curses_tuple(color))
            self.colors[color] = color_number
        return self.colors[color]

    def get_pair(self, fg_color: str, bg_color: str):
        """Returns curses color-pair for the given colors (registers the individual colors + the pair if needed)"""
        fg_color, bg_color = self.clean_color(fg_color), self.clean_color(bg_color)
        pair = (fg_color, bg_color)
        if pair not in self.color_pairs:
            pair_number = next(self._pair_number)
            curses.init_pair(pair_number, self.get_color(fg_color), self.get_color(bg_color))
            self.color_pairs[pair] = pair_number
        return curses.color_pair(self.color_pairs[pair])


class AppUI:
    """
    Handles all UI related stuff for the app.
     - Colors
     - Curses & windows
     - Displaying chat messages
     - Getting input from the user
     - Listening to key events & scrolling
    """

    def __init__(self):
        self.stdscr = self.init_curses()

        self.colors = UIColors()

        # Create an area/widget to get user input
        input_win = self.stdscr.subwin(5, curses.COLS, curses.LINES - 5, 0)
        self.input_widget = InputUIWidget(window=input_win, input_submitted_cb=self.handle_input_submitted)

        # Create an area/widget to show the chat messages
        chat_win = self.stdscr.subwin(curses.LINES - 5, curses.COLS, 0, 0)
        self.chat_messages_widget = ScrollableTextUIWidget(window=chat_win, n_scrollable_lines=32)

        # Widgets that can receive "focus" when the user presses the `tab` key
        self.focus_rotation = deque([self.chat_messages_widget, self.input_widget])

    @staticmethod
    def init_curses():
        """Initialize curses and get the standard screen and have cleanup happen automatically."""

        # Register the cleanup to run when the program exits
        @atexit.register
        def cleanup():
            """Makes the terminal go back to normal."""
            stdscr.keypad(False)
            curses.echo()
            curses.nocbreak()
            curses.endwin()

        stdscr = curses.initscr()
        stdscr.nodelay(True)  # Make stdscr.getch() "non-blocking"
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        with contextlib.suppress(Exception):
            curses.start_color()
        curses.use_default_colors()
        return stdscr

    @property
    def focused_widget(self):
        """The widget that currently has focus."""
        return self.focus_rotation[0]

    def rotate_focus(self):
        """Rotate focus to the next widget."""
        self.focus_rotation.rotate(1)
        self.focus_rotation[-1].set_focus(False)
        self.focus_rotation[0].set_focus(True)

    async def run_forever(self):
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
        """Callback for the the user submits some text."""
        await events.push(Events.UserInputSubmitted(text=text))

    def append_user_message(self, text: str, user: User):
        text = f"{user.username:>10}:  {text}"
        self.chat_messages_widget.append_text(text=text, color_pair=self.colors.get_pair(user.color, "ffffff"))

    def append_system_message(self, text: str):
        self.chat_messages_widget.append_text(text=text, color_pair=curses.color_pair(0))


class App:
    """
    The chat app at the highest level.
    Contains the
     - ui: messages, user input, colors
     - network: server/client, connections, reading/writing network data
     - users: the users that are in the chat
     - user: the local user
    """

    def __init__(self):
        self.ui = AppUI()
        self.network = Network()
        self.users = {}
        self.user: User | None = None

        if config.serve:
            # When running the server we can immediately create/add the host's user
            self.user = User(uid=uuid.uuid4(), username=config.username, color=config.color)
            self.users = {self.user.uid: self.user}

    async def add_user(self, jr: JoinRequest, cid: uuid.UUID):
        """Creates & adds a new user from the given join request and connection id."""
        user = User(uid=cid, username=jr.username, color=jr.color)
        self.users[user.uid] = user
        await app.send_server_info()
        await app.system_message(f"{user.username} joined the chat", send_to_channel=True)

    async def remove_user(self, uid: uuid.UUID):
        """Removes a user with the given id from the chat."""
        user = self.users.pop(uid)
        await app.send_server_info()
        await app.system_message(f"{user.username} left the chat", send_to_channel=True)

    async def run_forever(self):
        """Starts the app and runs it indefinitely."""
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.ui.run_forever())
            tg.create_task(self.network.run_forever())

    async def send_dataclass(self, dc, exclude: set[uuid.UUID] | None = None, include: set[uuid.UUID] | None = None):
        """
        Sends the given dataclass across the network.

        include: User ids to send the data
        exclude: User ids not to send the data
        """
        data = SendData.from_json_transferable(dc)
        await self.network.send(data.to_bytes(), exclude=exclude, include=include)

    async def system_message(self, sm: str | SystemMessage, send_to_channel=False):
        """
        Append a new system message to make visible in the UI.

        send_to_channel: When `True` send the system message to the entire network
        """
        if isinstance(sm, str):
            sm = SystemMessage(text=sm)
        self.ui.append_system_message(text=sm.text)
        if send_to_channel:
            await self.send_dataclass(sm)

    async def chat_message(self, cm: ChatMessage):
        """Append a new chat message to make visible in the UI & send it across the network when appropriate."""
        self.ui.append_user_message(text=cm.text, user=self.users[cm.uid])
        if config.serve or self.user and cm.uid == self.user.uid:
            await self.send_dataclass(cm, exclude={cm.uid})

    async def send_server_info(self):
        """Sends the latest ServerInfo to connected clients."""
        for uid, user in self.users.items():
            # Each user should receive a version that specifies their own user id
            si = ServerInfo(users=self.users, uid=uid)
            await self.send_dataclass(si, include={uid})

    async def sync_server_info(self, si: ServerInfo):
        """Takes the given info and updates the appropriate variables/information."""
        self.users = si.users
        self.user = si.users[si.uid]


@events.register(Events.UserInputSubmitted)
async def on_input_submitted(event: Events.UserInputSubmitted):
    assert app.user is not None
    await app.chat_message(ChatMessage(uid=app.user.uid, text=event.text))


@events.register(Events.ServerStarted)
async def on_server_started(event: Events.ServerStarted):
    await app.system_message(f"Server started on {config.host}:{config.port}")


@events.register(Events.ConnectedToHost)
async def on_connected_to_host(event: Events.ConnectedToHost):
    await app.system_message(f"Connected to server {config.host}:{config.port}")
    await app.send_dataclass(JoinRequest(username=config.username, color=config.color))


@events.register(Events.EstablishedConnection)
async def on_new_connection(event: Events.EstablishedConnection):
    await app.system_message(f"Established connection: remote_address {event.remote_address}")


@events.register(Events.LostConnection)
async def on_lost_connection(event: Events.LostConnection):
    await app.system_message(f"Connection ended: remote_address {event.remote_address}")
    if config.serve:
        await app.remove_user(event.cid)


@events.register(Events.ReceivedData, server_only=True)
async def on_received_data__server(event: Events.ReceivedData):
    if event.data_type != SendDataType.JSON:
        return

    # Convert json dict to dataclass instance
    obj = JsonTransferable.from_json(event.json())

    # Do something depending on type(obj)
    match obj:
        case ChatMessage():  # This syntax DOES NOT instantiate a new instance
            obj.uid = event.cid  # Prevents clients from trying to change the uid of their messages
            await app.chat_message(obj)
        case JoinRequest():
            await app.add_user(obj, cid=event.cid)


@events.register(Events.ReceivedData, client_only=True)
async def on_received_data__client(event: Events.ReceivedData):
    if event.data_type != SendDataType.JSON:
        return

    # Convert json dict to dataclass instance
    obj = JsonTransferable.from_json(event.json())

    # Do something depending on type(obj)
    match obj:
        case ChatMessage():
            await app.chat_message(obj)
        case ServerInfo():
            await app.sync_server_info(obj)
        case SystemMessage():
            await app.system_message(obj)


if __name__ == "__main__":
    # Get command-line run options
    parser = argparse.ArgumentParser(description="PyChat :)")
    parser.add_argument("-H", "--host", action="store", help="Host of sever", default="0.0.0.0")
    parser.add_argument("-P", "--port", action="store", help="Port of sever", default="8080")
    parser.add_argument("-u", "--username", action="store", help="Display name to use in the chat", default="Anonymous")
    parser.add_argument(
        "-c", "--color", action="store", help="Display color to use for your messages", default="000000"
    )
    parser.add_argument("-s", "--serve", action="store_true", help="Run the chat server for others to connect")
    parser.add_argument("--ssl", action="store_true", help="Use secure connection via SSL")
    parser.add_argument("--certfile", action="store", help="Path to SSL certificate", default="./client.pem")
    parser.add_argument("--cafile", action="store", help="Path to SSL certificate authority", default="./rootCA.pem")
    parsed_args = parser.parse_args()

    config = RunConfig(**vars(parsed_args))  # Global runtime configuration

    # Launch the app
    with contextlib.suppress(KeyboardInterrupt):
        app = App()
        asyncio.run(app.run_forever())
