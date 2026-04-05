"""
catmux.vport - Virtual serial port management

Creates PTY pairs using os.openpty(). One end (the "app side") is exposed
to the connecting application as a symlink under /dev/catmux/vportN.
The other end (the "broker side") is read/written by catmux internally.

Key fix over v0.1:
  - _rx_loop now owns its OWN framer instance and accumulates raw bytes
    until complete frames are available before placing anything on rx_queue.
    This eliminates the split-read race where a command arriving across two
    OS read() calls was handed upstream as two broken fragments.

Signal handling:
  - RTS/DTR from the app are monitored via TIOCMGET polling at ~50ms
  - Changes invoke broker callbacks (PTT, CW keying)
  - CTS/DSR/DCD from the real port are mirrored back to all vports

Icom note:
  IC-7100 USB firmware ignores RTS/DTR entirely (pins unconnected).
  catmux intercepts the RTS callback in the broker and translates it to
  a CI-V 0x1C PTT command. The vport layer is unaware of this — it just
  fires the callback as normal, keeping the abstraction clean.
"""

import os
import termios
import fcntl
import struct
import threading
import logging
import select
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TIOCM ioctl constants (Linux x86/arm)
# ---------------------------------------------------------------------------
TIOCMGET = 0x5415
TIOCMSET = 0x5418

TIOCM_RTS = 0x004
TIOCM_CTS = 0x020
TIOCM_DTR = 0x002
TIOCM_DSR = 0x100
TIOCM_DCD = 0x200
TIOCM_RI  = 0x080

_TIOCM_STRUCT = struct.Struct("I")


def _tiocmget(fd: int) -> int:
    buf = bytearray(4)
    try:
        fcntl.ioctl(fd, TIOCMGET, buf)
        return _TIOCM_STRUCT.unpack(buf)[0]
    except OSError:
        return 0


def _tiocmset(fd: int, state: int):
    try:
        fcntl.ioctl(fd, TIOCMSET, _TIOCM_STRUCT.pack(state))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# RxItem — a fully framed command from a vport, ready for the broker
# ---------------------------------------------------------------------------

class RxItem:
    __slots__ = ("priority", "frame", "vport_index")

    def __init__(self, priority: int, frame: bytes, vport_index: int):
        self.priority    = priority
        self.frame       = frame
        self.vport_index = vport_index

    # Allow PriorityQueue sorting
    def __lt__(self, other: "RxItem") -> bool:
        return self.priority < other.priority


# ---------------------------------------------------------------------------
# VPort
# ---------------------------------------------------------------------------

class VPort:
    """
    One virtual serial port — a PTY pair exposing a rig-compatible
    interface to a single application.
    """

    SYMLINK_DIR = Path("/dev/catmux")

    def __init__(
        self,
        index:         int,
        name:          str  = "",
        priority:      int  = 10,
        symlink_dir:   Optional[Path] = None,
        rig_family:    str  = "yaesu",
        framer_kwargs: Optional[dict] = None,
        device:        Optional[str] = None,
    ):
        self.index         = index
        self.name          = name or f"vport{index}"
        self.priority      = priority
        self.rig_family    = rig_family
        self.framer_kwargs = framer_kwargs or {}
        self.symlink_dir   = symlink_dir or self.SYMLINK_DIR
        self.device        = device  # real device path e.g. /dev/tnt0

        if device:
            # Open a real device (tty0tty, etc.) directly — no PTY needed.
            # broker_fd is the catmux side, app_fd is unused (app opens device directly)
            self.broker_fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            self.app_fd    = -1
            self._configure_raw(self.broker_fd)
            # Symlink points directly to the device for reference
            self.symlink = self.symlink_dir / f"vport{index}"
            self.symlink_dir.mkdir(parents=True, exist_ok=True)
            self.symlink.unlink(missing_ok=True)
            self.symlink.symlink_to(device)
            log.info(f"VPort '{self.name}' idx={index} device={device} (direct)")
            log.info(f"  {self.symlink} -> {device}")
        else:
            # Normal PTY pair
            self.broker_fd, self.app_fd = os.openpty()
            self._configure_raw(self.broker_fd)
            self._configure_raw(self.app_fd)
            self.symlink = self.symlink_dir / f"vport{index}"
            self._make_symlink()
            log.info(
                f"VPort '{self.name}' idx={index} "
                f"broker_fd={self.broker_fd} app_fd={self.app_fd} "
                f"symlink={self.symlink}"
            )

        # Shared state regardless of PTY or device mode
        self.rx_queue: Queue[RxItem] = Queue()
        self._tx_queue: Queue[bytes] = Queue(maxsize=128)
        self.rts = False
        self.dtr = False
        self._prev_rts = False
        self._prev_dtr = False
        self._on_rts_change: Optional[Callable] = None
        self._on_dtr_change: Optional[Callable] = None
        self._running  = False
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # PTY configuration
    # ------------------------------------------------------------------

    def _configure_raw(self, fd: int):
        """Set PTY to raw 8N1 — apps see a clean serial-like device."""
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                        # iflag: no processing
            attrs[1] = 0                                        # oflag: no processing
            attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD  # cflag: 8N1
            attrs[3] = 0                                        # lflag: no canonical/echo
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error as e:
            log.warning(f"PTY raw config fd={fd}: {e}")

    def _make_symlink(self):
        self.symlink_dir.mkdir(parents=True, exist_ok=True)
        try:
            pty_name = os.ttyname(self.app_fd)
        except OSError as e:
            log.error(f"Cannot resolve PTY name for app_fd={self.app_fd}: {e}")
            return
        self.symlink.unlink(missing_ok=True)
        self.symlink.symlink_to(pty_name)
        log.info(f"  {self.symlink} -> {pty_name}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        on_rts_change: Optional[Callable] = None,
        on_dtr_change: Optional[Callable] = None,
    ):
        self._on_rts_change = on_rts_change
        self._on_dtr_change = on_dtr_change
        self._running = True

        self._threads = [
            threading.Thread(target=self._rx_loop,
                             name=f"rx-{self.name}", daemon=True),
            threading.Thread(target=self._tx_loop,
                             name=f"tx-{self.name}", daemon=True),
            threading.Thread(target=self._signal_loop,
                             name=f"sig-{self.name}", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._running = False
        for fd in (self.broker_fd, self.app_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.symlink.unlink(missing_ok=True)
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()
        log.info(f"VPort '{self.name}' stopped")

    # ------------------------------------------------------------------
    # Public API (called by broker)
    # ------------------------------------------------------------------

    def send_to_app(self, data: bytes):
        """Queue response bytes to be written back to the application."""
        try:
            self._tx_queue.put_nowait(data)
        except Exception:
            log.warning(
                f"VPort '{self.name}' tx queue full — dropping {len(data)} bytes"
            )

    def set_cts(self, state: bool):
        """Mirror real-port CTS to the app-facing PTY."""
        self._set_tiocm_bit(TIOCM_CTS, state)

    def set_dsr(self, state: bool):
        """Mirror real-port DSR to the app-facing PTY."""
        self._set_tiocm_bit(TIOCM_DSR, state)

    def set_dcd(self, state: bool):
        """Mirror real-port DCD (carrier detect) to the app-facing PTY."""
        self._set_tiocm_bit(TIOCM_DCD, state)

    def _set_tiocm_bit(self, bit: int, state: bool):
        current = _tiocmget(self.broker_fd)
        new = (current | bit) if state else (current & ~bit)
        if new != current:
            _tiocmset(self.broker_fd, new)

    # ------------------------------------------------------------------
    # Internal threads
    # ------------------------------------------------------------------

    def _rx_loop(self):
        """
        Read raw bytes from the app (via broker_fd).

        CRITICAL: we maintain our own framer instance here so that partial
        frames split across multiple OS read() calls are reassembled before
        anything hits the broker's rx_queue. Only complete, valid frames
        are enqueued as RxItem objects.
        """
        from catmux.framer import make_framer
        framer = make_framer(self.rig_family, **self.framer_kwargs)

        while self._running:
            try:
                ready, _, _ = select.select([self.broker_fd], [], [], 0.1)
                if not ready:
                    continue

                chunk = os.read(self.broker_fd, 512)
                if not chunk:
                    log.info(f"VPort '{self.name}': app side closed (EOF)")
                    break

                for frame in framer.feed(chunk):
                    self.rx_queue.put(
                        RxItem(self.priority, frame, self.index)
                    )

            except OSError as e:
                if self._running:
                    log.warning(f"VPort '{self.name}' rx error: {e}")
                break

        log.debug(f"VPort '{self.name}' _rx_loop exit")

    def _tx_loop(self):
        """Drain _tx_queue, writing response bytes back to the application."""
        while self._running:
            try:
                data = self._tx_queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                written = 0
                while written < len(data):
                    written += os.write(self.broker_fd, data[written:])
            except OSError as e:
                if self._running:
                    log.warning(f"VPort '{self.name}' tx error: {e}")
                break

        log.debug(f"VPort '{self.name}' _tx_loop exit")

    def _signal_loop(self):
        """
        Poll broker_fd for PTT signal changes from the app every 10ms.

        PTY mode: monitor RTS and DTR directly — the app sets these on
        the pts device and we read them back.

        Device mode (tty0tty): the null-modem pair crosses signals, so
        the app's RTS on tnt1 appears as CTS on tnt0 (our broker_fd).
        Similarly DTR on tnt1 appears as DSR on tnt0.
        We monitor CTS/DSR in device mode and fire the RTS/DTR callbacks
        so the rest of the broker is unaware of the difference.
        """
        while self._running:
            time.sleep(0.01)
            try:
                state = _tiocmget(self.broker_fd)

                if self.device:
                    # Device mode: read crossed signals
                    new_rts = bool(state & TIOCM_CTS)   # app RTS → our CTS
                    new_dtr = bool(state & TIOCM_DSR)   # app DTR → our DSR
                else:
                    # PTY mode: read direct signals
                    new_rts = bool(state & TIOCM_RTS)
                    new_dtr = bool(state & TIOCM_DTR)

            except OSError:
                break

            if new_rts != self._prev_rts:
                self._prev_rts = new_rts
                self.rts       = new_rts
                log.debug(f"VPort '{self.name}' RTS={'ON' if new_rts else 'OFF'}"
                          f"{' (via CTS/tty0tty)' if self.device else ''}")
                if self._on_rts_change:
                    try:
                        self._on_rts_change(self, new_rts)
                    except Exception as e:
                        log.error(f"VPort '{self.name}' RTS callback: {e}")

            if new_dtr != self._prev_dtr:
                self._prev_dtr = new_dtr
                self.dtr       = new_dtr
                log.debug(f"VPort '{self.name}' DTR={'ON' if new_dtr else 'OFF'}"
                          f"{' (via DSR/tty0tty)' if self.device else ''}")
                if self._on_dtr_change:
                    try:
                        self._on_dtr_change(self, new_dtr)
                    except Exception as e:
                        log.error(f"VPort '{self.name}' DTR callback: {e}")

        log.debug(f"VPort '{self.name}' _signal_loop exit")


# ---------------------------------------------------------------------------
# VPortManager
# ---------------------------------------------------------------------------

class VPortManager:
    """Manages the full set of active VPort instances."""

    def __init__(self, symlink_dir: Optional[Path] = None):
        self.symlink_dir = symlink_dir or VPort.SYMLINK_DIR
        self._ports: dict[int, VPort] = {}
        self._lock = threading.Lock()

    def create(
        self,
        index:         int,
        name:          str  = "",
        priority:      int  = 10,
        rig_family:    str  = "yaesu",
        framer_kwargs: Optional[dict] = None,
        on_rts_change: Optional[Callable] = None,
        on_dtr_change: Optional[Callable] = None,
        device:        Optional[str] = None,
    ) -> VPort:
        vp = VPort(
            index         = index,
            name          = name,
            priority      = priority,
            symlink_dir   = self.symlink_dir,
            rig_family    = rig_family,
            framer_kwargs = framer_kwargs or {},
            device        = device,
        )
        vp.start(on_rts_change=on_rts_change, on_dtr_change=on_dtr_change)
        with self._lock:
            self._ports[index] = vp
        return vp

    def get(self, index: int) -> Optional[VPort]:
        with self._lock:
            return self._ports.get(index)

    def all_ports(self) -> list[VPort]:
        with self._lock:
            return list(self._ports.values())

    def stop_all(self):
        with self._lock:
            ports, self._ports = list(self._ports.values()), {}
        for vp in ports:
            try:
                vp.stop()
            except Exception as e:
                log.warning(f"Error stopping vport '{vp.name}': {e}")

    # Signal broadcast helpers
    def broadcast_cts(self, state: bool):
        for vp in self.all_ports():
            vp.set_cts(state)

    def broadcast_dsr(self, state: bool):
        for vp in self.all_ports():
            vp.set_dsr(state)

    def broadcast_dcd(self, state: bool):
        for vp in self.all_ports():
            vp.set_dcd(state)
