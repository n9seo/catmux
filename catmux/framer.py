"""
catmux.framer - Protocol framers for different CAT/CI-V rig families

Supported families:
  - yaesu    : ASCII semicolon-terminated (FT-991A, FT-dx series, etc.)
  - kenwood  : ASCII semicolon-terminated (TS-590, TS-2000, etc.)
  - elecraft  : ASCII semicolon-terminated, Kenwood-heritage (K3, KX3, KX2, K4)
  - icom     : Binary CI-V framing FE FE <addr> <ctrl> <cmd...> FD

The framer is responsible for:
  1. Splitting a raw byte stream into discrete commands/responses
  2. Identifying whether a frame is a GET (query) or SET (action)
  3. Extracting a command key (e.g. "FA") for mirror cache lookups
  4. Building properly framed bytes to send to the radio
"""

import re
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class BaseFramer(ABC):
    """Abstract base for all protocol framers."""

    @abstractmethod
    def feed(self, data: bytes) -> list[bytes]:
        """
        Feed raw bytes from the serial port (or a virtual port).
        Returns a list of complete frames extracted from the data.
        Maintains internal buffer for partial frames.
        """

    @abstractmethod
    def is_get(self, frame: bytes) -> bool:
        """Return True if this frame is a query (GET) that expects a response."""

    def is_error(self, frame: bytes) -> bool:
        """
        Return True if this frame is an error response from the radio.
        Yaesu/Kenwood/Elecraft use '?;' for unsupported commands.
        Icom CI-V uses 0xFA. Default implementation handles the ASCII case.
        """
        return frame.strip() == b'?;'

    @abstractmethod
    def command_key(self, frame: bytes) -> str | None:
        """
        Extract the command identifier (e.g. "FA", "MD") from a frame.
        Used as the key into the mirror state cache.
        Returns None if the frame cannot be identified.
        """

    @abstractmethod
    def make_get(self, key: str) -> bytes:
        """Build a properly framed GET query for the given command key."""

    def reset(self):
        """Reset internal buffer (e.g. after timeout or port reconnect)."""
        self._buf = b""


# ---------------------------------------------------------------------------
# Yaesu / Kenwood / Elecraft  (ASCII semicolon-terminated)
# ---------------------------------------------------------------------------

class SemicolonFramer(BaseFramer):
    """
    Framer for ASCII CAT protocols that use ';' as command terminator.

    Covers:
      - Yaesu  : FT-991A, FT-dx101, FT-710, FT-818, FT-991, FT-450, etc.
      - Kenwood: TS-590, TS-2000, TS-890, TS-990, etc.
      - Elecraft: K3, K3S, KX3, KX2, K4  (Kenwood-heritage, minor extensions)

    Frame format:
      <CMD2-3chars>[params];<terminator>

    GET (query): exactly the 2-3 char command + ';'   e.g.  FA;
    SET        : command + param data + ';'            e.g.  FA014250000;
    Response   : command + data + ';'                  e.g.  FA014250000;

    Note: Elecraft allows 3-char commands (e.g. SWR;, BW$;) but the
    same semicolon terminator applies.
    """

    # Minimum length of a valid command name (2 for Yaesu/Kenwood, 2-3 for Elecraft)
    CMD_MIN = 2
    CMD_MAX = 4   # covers Elecraft extended like "BW$"

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames = []
        while b";" in self._buf:
            idx = self._buf.index(b";")
            frame = self._buf[: idx + 1]
            self._buf = self._buf[idx + 1 :]
            # Discard empty or whitespace-only fragments
            if frame.strip(b" \r\n\t") not in (b"", b";"):
                frames.append(frame)
        return frames

    def is_get(self, frame: bytes) -> bool:
        """
        A GET is the bare command name followed immediately by ';'.
        Examples: FA;  MD;  IF;  BW;  BW$;
        We allow optional whitespace for resilience.
        """
        s = frame.decode("ascii", errors="replace").strip().rstrip(";").strip()
        # A GET has no numeric/data payload — just the command letters (and maybe $)
        return bool(re.fullmatch(r"[A-Za-z]{2,4}[$]?", s))

    def command_key(self, frame: bytes) -> str | None:
        try:
            s = frame.decode("ascii", errors="replace").strip().rstrip(";")
            # Extract leading alpha (+ optional $) characters as the key
            m = re.match(r"([A-Za-z]{2,4}[$]?)", s)
            if m:
                return m.group(1).upper()
        except Exception:
            pass
        return None

    def make_get(self, key: str) -> bytes:
        return (key.upper() + ";").encode("ascii")

    def make_set(self, key: str, value: str) -> bytes:
        return (key.upper() + value + ";").encode("ascii")


# ---------------------------------------------------------------------------
# Icom CI-V  (binary framing)
# ---------------------------------------------------------------------------

class CIVFramer(BaseFramer):
    """
    Framer for Icom CI-V binary protocol.

    Frame structure (controller → radio):
      FE FE <radio_addr> <ctrl_addr> <cmd> [<subcmd>] [<data...>] FD

    Frame structure (radio → controller):
      FE FE <ctrl_addr> <radio_addr> <cmd> [<subcmd>] [<data...>] FD

    Special responses:
      FE FE <ctrl> <radio> FB FD  = OK
      FE FE <ctrl> <radio> FA FD  = NG (error)

    The CI-V bus is originally a single-wire shared bus (like 1-wire),
    so the radio echoes every byte it receives — we must handle echoes.

    Default IC-7100 address: 0x88
    Default controller address: 0xE0 (but software can use any unused addr)

    PTT and CW keying: CI-V command 0x1C sub 0x00 (TX/RX)
    There are NO RTS/DTR signals used for PTT on the IC-7100 over USB.
    """

    PREAMBLE    = bytes([0xFE, 0xFE])
    TERMINATOR  = 0xFD
    OK          = 0xFB
    NG          = 0xFA

    # Well-known command codes for mirror key mapping
    CMD_NAMES = {
        0x00: "FREQ",       # operating frequency
        0x01: "MODE",       # operating mode
        0x03: "FREQ",       # read frequency (alt)
        0x04: "MODE",       # read mode (alt)
        0x06: "MODE_SET",
        0x14: "LEVEL",      # AF/RF/SQL etc. (sub-command selects which)
        0x15: "METER",      # S-meter, power, SWR etc.
        0x1A: "MEMORY",
        0x1C: "TX",         # TX/RX control and antenna
        0x25: "FREQ_BAND",  # read/set frequency (extended, IC-7300+)
    }

    def __init__(self, radio_addr: int = 0x88, ctrl_addr: int = 0xE0):
        self.radio_addr = radio_addr
        self.ctrl_addr  = ctrl_addr
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames = []
        while True:
            # Find preamble
            idx = self._buf.find(self.PREAMBLE)
            if idx == -1:
                # No preamble found — discard junk but keep last byte
                # (it might be start of next preamble)
                self._buf = self._buf[-1:] if self._buf else b""
                break
            if idx > 0:
                # Discard bytes before preamble
                self._buf = self._buf[idx:]
            # Find terminator after preamble
            end = self._buf.find(bytes([self.TERMINATOR]), 2)
            if end == -1:
                # Incomplete frame — wait for more data
                break
            frame = self._buf[: end + 1]
            self._buf = self._buf[end + 1 :]
            # Minimum valid CI-V frame: FE FE addr ctrl cmd FD = 6 bytes
            if len(frame) >= 6:
                frames.append(frame)
        return frames

    def is_get(self, frame: bytes) -> bool:
        """
        CI-V GETs typically have no data payload — just:
        FE FE addr ctrl cmd [subcmd] FD
        That's 6 bytes (no subcmd) or 7 bytes (with subcmd), no data beyond that.
        """
        # Strip preamble (2) + addr (1) + ctrl (1) + terminator (1) = 5 overhead
        payload_len = len(frame) - 5
        # A GET has cmd [+ optional 1-byte subcmd] only
        return payload_len in (1, 2)

    def command_key(self, frame: bytes) -> str | None:
        if len(frame) < 6:
            return None
        cmd_byte = frame[4]
        name = self.CMD_NAMES.get(cmd_byte, f"CMD_{cmd_byte:02X}")
        # For LEVEL and METER, include subcmd to differentiate AF from RF etc.
        if cmd_byte in (0x14, 0x15) and len(frame) >= 7:
            name = f"{name}_{frame[5]:02X}"
        return name

    def make_get(self, key: str) -> bytes:
        """
        Build a CI-V GET frame for a named command.
        For common commands only — callers can also pass raw bytes.
        """
        # Reverse lookup
        cmd_map = {v: k for k, v in self.CMD_NAMES.items()}
        if key not in cmd_map:
            raise ValueError(f"Unknown CI-V command key: {key}")
        cmd = cmd_map[key]
        return bytes([0xFE, 0xFE, self.radio_addr, self.ctrl_addr, cmd, 0xFD])

    def make_set(self, cmd_byte: int, data: bytes) -> bytes:
        """Build a CI-V SET frame with arbitrary data payload."""
        return bytes([0xFE, 0xFE, self.radio_addr, self.ctrl_addr, cmd_byte]) \
               + data + bytes([self.TERMINATOR])

    def make_ptt_on(self) -> bytes:
        """CI-V command to key TX (PTT on)."""
        return self.make_set(0x1C, bytes([0x00, 0x01]))

    def make_ptt_off(self) -> bytes:
        """CI-V command to return to RX (PTT off)."""
        return self.make_set(0x1C, bytes([0x00, 0x00]))

    def is_echo(self, frame: bytes, sent: bytes) -> bool:
        """
        On the CI-V bus, the radio echoes every byte transmitted.
        Check if a received frame is just an echo of what we sent.
        """
        return frame == sent

    def is_ok(self, frame: bytes) -> bool:
        return len(frame) >= 5 and frame[4] == self.OK

    def is_ng(self, frame: bytes) -> bool:
        return len(frame) >= 5 and frame[4] == self.NG


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

FRAMER_MAP = {
    "yaesu":    SemicolonFramer,
    "kenwood":  SemicolonFramer,
    "elecraft": SemicolonFramer,
    "icom":     CIVFramer,
}

def make_framer(rig_family: str, **kwargs) -> BaseFramer:
    """
    Instantiate the correct framer for the given rig family string.
    Extra kwargs are passed to the framer constructor (e.g. radio_addr for Icom).
    """
    family = rig_family.lower()
    if family not in FRAMER_MAP:
        raise ValueError(
            f"Unknown rig family '{rig_family}'. "
            f"Choose from: {', '.join(FRAMER_MAP)}"
        )
    return FRAMER_MAP[family](**kwargs)
