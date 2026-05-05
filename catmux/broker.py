"""
catmux.broker - Command arbitration and response routing

The broker is the central coordinator of catmux. It:

  1. Owns the real serial port connection to the radio
  2. Receives complete frames from all VPorts via a shared priority queue
  3. For GET queries — serves from mirror cache instantly if fresh
  4. For SET commands and cache misses — serializes them to the radio one
     at a time and routes responses back to the originating VPort
  5. Runs a background poller to keep the mirror warm
  6. Monitors CTS/DSR/DCD on the real port and broadcasts to all VPorts
  7. Translates RTS/DTR changes from VPorts to real-port signals,
     OR (for Icom) to CI-V PTT commands — transparently

Threading model
  serial_reader   : reads from real port → feeds framer → updates mirror
                    → notifies dispatcher of incoming response
  cmd_dispatcher  : dequeues QueuedCommands, sends to radio, waits for reply
  poller_thread   : injects background poll commands when mirror TTL elapses
  vport_reader_N  : one thread per VPort, reads rx_queue → enqueues commands
  signal_monitor  : polls real port CTS/DSR/DCD → broadcasts to VPorts

Design notes
  - Response matching: a simple "last sent, expecting reply" model protected
    by a threading.Event. Only one command is in-flight at a time (the queue
    serializes everything). This is correct because all CAT protocols are
    strictly half-duplex.
  - Unsolicited frames (AI mode, auto-info): broadcast to all VPorts since
    we don't know which app cares.
  - The Icom CI-V path is fully stubbed with clear TODO markers so adding
    real hardware support later requires changes only in the labelled spots.
"""

import threading
import logging
import time
import serial
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .framer import BaseFramer, CIVFramer, make_framer
from .mirror import MirrorCache, Poller, POLL_SCHEDULES
from .vport  import VPortManager, VPort, RxItem

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority levels — lower number = higher priority
# ---------------------------------------------------------------------------
PRI_PTT     = 0   # PTT / CW key — never waits behind anything
PRI_SET     = 5   # freq/mode SET from an app
PRI_GET_APP = 10  # uncached GET that must go to the radio
PRI_POLL    = 20  # background mirror refresh


@dataclass(order=True)
class QueuedCommand:
    priority: int
    seq:      int                           # FIFO tie-break within same priority
    frame:    bytes         = field(compare=False)
    origin:   Optional[VPort] = field(compare=False, default=None)  # None = internal
    is_get:   bool          = field(compare=False, default=False)
    key:      Optional[str] = field(compare=False, default=None)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

class Broker:
    """
    Central CAT arbitrator.

    Typical usage:
        broker = Broker(config)
        broker.start()          # connect serial, create vports, start all threads
        signal.pause()
        broker.stop()
    """

    def __init__(self, config: dict):
        self.config = config
        self.rig_cfg = config["rig"]

        # ---- Rig family & framer -----------------------------------------
        self.rig_family   = self.rig_cfg["family"].lower()
        self._framer_kwargs: dict = {}
        if self.rig_family == "icom":
            self._framer_kwargs["radio_addr"] = int(
                self.rig_cfg.get("civ_address", "0x88"), 16)
            self._framer_kwargs["ctrl_addr"] = int(
                self.rig_cfg.get("civ_ctrl_address", "0xE0"), 16)

        self.framer: BaseFramer = make_framer(
            self.rig_family, **self._framer_kwargs)

        # ---- Unsupported command persistence ----------------------------
        # Commands the radio rejected with ?; are saved so we don't probe
        # them again on next startup.
        self._unsupported: set[str] = set()
        self._unsupported_file = Path(
            config.get("catmux", {}).get(
                "state_dir",
                Path.home() / ".catmux"
            )
        ) / f"unsupported_{self.rig_family}.txt"
        self._load_unsupported()

        # ---- Mirror cache & poller ----------------------------------------
        self.mirror = MirrorCache(ttl=self.rig_cfg.get("mirror_ttl", 1.0))
        poll_sched  = {k: v for k, v in POLL_SCHEDULES.get(self.rig_family, {}).items()
                       if k not in self._unsupported}
        self.poller = Poller(poll_sched)

        # ---- Real serial port --------------------------------------------
        self.serial: Optional[serial.Serial] = None
        self._write_lock = threading.Lock()   # guard serial.write()

        # ---- Virtual port manager ----------------------------------------
        vp_cfg = config.get("vports", {})
        sym_dir = Path(vp_cfg["symlink_dir"]) if "symlink_dir" in vp_cfg else None
        self.vport_mgr = VPortManager(symlink_dir=sym_dir)

        # ---- Command queue -----------------------------------------------
        self._cmd_queue: queue.PriorityQueue[QueuedCommand] = queue.PriorityQueue()
        self._seq      = 0
        self._seq_lock = threading.Lock()

        # ---- In-flight response tracking ---------------------------------
        # Only one command is in-flight at a time (CAT is half-duplex).
        # _pending_origin: the VPort that sent the command (or None = internal)
        # _inflight: True whenever ANY command has been sent and we are waiting
        #            for a response — including internal poll commands where
        #            _pending_origin is None. This is what serial_reader checks.
        # _response_event: set by serial_reader when any frame arrives
        self._pending_origin:  Optional[VPort] = None
        self._pending_key:     Optional[str]   = None
        self._pending_frame:   Optional[bytes] = None
        self._inflight:        bool            = False
        self._response_event   = threading.Event()
        self._inflight_lock    = threading.Lock()

        # ---- Running state -----------------------------------------------
        self._running  = False
        self._threads: list[threading.Thread] = []

        # ---- Per-vport PTT routing ---------------------------------------
        # Populated in _create_vports from config ptt_from/ptt_via settings
        self._ptt_config: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Connect to radio, create virtual ports, start all internal threads.
        This is the single start call — no separate start_vport_readers().
        """
        self._connect_serial()
        self._create_vports()
        self._running = True

        # Assert CTS/DSR on all virtual ports immediately — some apps
        # (flrig, HRD etc.) check CTS before sending anything and will
        # block forever if it isn't asserted.
        for vp in self.vport_mgr.all_ports():
            vp.set_cts(True)
            vp.set_dsr(True)

        # Core threads
        core_threads = [
            ("serial-reader",  self._serial_reader),
            ("cmd-dispatcher", self._cmd_dispatcher),
            ("poller",         self._poller_thread),
            ("signal-monitor", self._signal_monitor),
        ]
        for name, target in core_threads:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        # One reader thread per VPort
        for vp in self.vport_mgr.all_ports():
            t = threading.Thread(
                target=self._vport_reader_loop,
                args=(vp,),
                name=f"vport-rd-{vp.name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        log.info(
            f"Broker started — {self.rig_family} on "
            f"{self.rig_cfg['port']} @ {self.rig_cfg.get('baud', 38400)} baud"
        )
        log.info(f"Virtual ports under {self.vport_mgr.symlink_dir}:")
        for vp in self.vport_mgr.all_ports():
            log.info(f"  [{vp.name}]  {vp.symlink}  (priority={vp.priority})")

    def stop(self):
        """Graceful shutdown — stops all threads and closes ports."""
        log.info("Broker shutting down...")
        self._running = False
        self._save_unsupported()

        # Unblock the dispatcher if it's waiting for a response
        self._response_event.set()

        self.vport_mgr.stop_all()

        if self.serial and self.serial.is_open:
            try:
                self.serial.close()
            except Exception:
                pass

        for t in self._threads:
            t.join(timeout=2.0)

        log.info("Broker stopped.")

    # ------------------------------------------------------------------
    # Serial connection
    # ------------------------------------------------------------------

    def _load_unsupported(self):
        """Load previously discovered unsupported commands from disk."""
        try:
            if self._unsupported_file.exists():
                keys = self._unsupported_file.read_text().split()
                self._unsupported = set(keys)
                log.debug(f"Loaded {len(keys)} unsupported commands from {self._unsupported_file}")
        except Exception as e:
            log.debug(f"Could not load unsupported commands: {e}")

    def _save_unsupported(self):
        """Persist unsupported commands to disk for next startup."""
        try:
            self._unsupported_file.parent.mkdir(parents=True, exist_ok=True)
            self._unsupported_file.write_text("\n".join(sorted(self._unsupported)))
        except Exception as e:
            log.debug(f"Could not save unsupported commands: {e}")

    def _connect_serial(self):
        cfg  = self.rig_cfg
        port = cfg["port"]
        baud = int(cfg.get("baud", 38400))

        self.serial = serial.Serial(
            port     = port,
            baudrate = baud,
            bytesize = serial.EIGHTBITS,
            parity   = serial.PARITY_NONE,
            stopbits = serial.STOPBITS_ONE,
            timeout  = 0.1,
            rtscts   = False,
            dsrdtr   = False,
            exclusive = True,   # prevent other processes grabbing the port
        )

        # Yaesu FT-991A over USB: CAT only activates when RTS is asserted.
        # Elecraft/Kenwood: rts_on_connect is false — leave RTS as-is.
        # Icom CI-V: RTS has no meaning, also false.
        if cfg.get("rts_on_connect", False):
            self.serial.rts = True
            log.info(f"RTS asserted on {port} (required for Yaesu USB CAT)")
        else:
            self.serial.rts = False

        log.info(f"Opened {port} at {baud} baud")

    def _try_reconnect(self):
        """Reopen the serial port after a disconnect. Blocks until success."""
        log.warning("Serial port lost — attempting reconnect...")
        self.mirror.invalidate_all()
        while self._running:
            try:
                if self.serial.is_open:
                    self.serial.close()
                time.sleep(2.0)
                self.serial.open()
                if self.rig_cfg.get("rts_on_connect", False):
                    self.serial.rts = True
                log.info("Serial port reconnected.")
                return
            except serial.SerialException as e:
                log.warning(f"Reconnect failed: {e} — retrying...")

    # ------------------------------------------------------------------
    # Virtual port creation
    # ------------------------------------------------------------------

    def _create_vports(self):
        vp_configs = self.config.get("vports", {}).get("ports", [])
        if not vp_configs:
            log.warning("No virtual ports defined in config — check [vports.ports]")
            return

        for i, vp_cfg in enumerate(vp_configs):
            name = vp_cfg.get("name", f"vport{i}")

            # Per-vport PTT routing config
            # ptt_from: which line the APP uses to signal PTT  (rts | dtr)
            # ptt_via:  what catmux sends to the radio          (rts | dtr | cat)
            ptt_from = vp_cfg.get("ptt_from", "rts").lower()
            ptt_via  = vp_cfg.get("ptt_via",  "rts").lower()

            if ptt_from not in ("rts", "dtr"):
                log.warning(f"[{name}] ptt_from='{ptt_from}' invalid, using 'rts'")
                ptt_from = "rts"
            if ptt_via not in ("rts", "dtr", "cat"):
                log.warning(f"[{name}] ptt_via='{ptt_via}' invalid, using 'rts'")
                ptt_via = "rts"

            # Store routing config indexed by vport index for callbacks
            self._ptt_config[i] = {"ptt_from": ptt_from, "ptt_via": ptt_via}
            log.info(f"  [{name}] PTT: app={ptt_from.upper()} -> radio={ptt_via.upper()}")

            self.vport_mgr.create(
                index         = i,
                name          = name,
                priority      = vp_cfg.get("priority", PRI_GET_APP),
                rig_family    = self.rig_family,
                framer_kwargs = self._framer_kwargs,
                on_rts_change = self._on_vport_rts,
                on_dtr_change = self._on_vport_dtr,
                device        = vp_cfg.get("device", None),
            )

    # ------------------------------------------------------------------
    # Thread: serial reader
    # ------------------------------------------------------------------

    def _serial_reader(self):
        """
        Read from the real serial port, frame the bytes, update the mirror,
        and hand responses back to whoever is waiting.
        """
        while self._running:
            try:
                chunk = self.serial.read(256)
                if not chunk:
                    continue

                frames = self.framer.feed(chunk)
                for frame in frames:
                    log.debug(f"Radio -> {_fmt(frame)}")

                    # CI-V: discard echoes (frames addressed back to us)
                    if isinstance(self.framer, CIVFramer):
                        if self._is_civ_echo(frame):
                            continue

                    # ?; means the radio doesn't support this command —
                    # log it, remove it from the poll schedule, still unblock
                    if self.framer.is_error(frame):
                        with self._inflight_lock:
                            bad_key = self._pending_key
                        if bad_key:
                            log.debug(
                                f"Radio does not support '{bad_key}' — "
                                f"removing from poll schedule"
                            )
                            self.poller.remove(bad_key)
                            self._unsupported.add(bad_key)
                        with self._inflight_lock:
                            if self._inflight:
                                self._response_event.set()
                        continue

                    key = self.framer.command_key(frame)
                    if key:
                        self.mirror.update(key, frame)

                    # Route to waiting dispatcher, or broadcast if unsolicited
                    with self._inflight_lock:
                        inflight       = self._inflight
                        waiting        = self._pending_origin
                        pending_key    = self._pending_key
                        pending_frame  = getattr(self, '_pending_frame', None)

                    if inflight:
                        # A command is in-flight — this frame is its response.
                        # Special case: if the radio echoed back our SET command
                        # verbatim (e.g. MD0C; echoed after we sent MD0C;),
                        # consume it silently and keep waiting for any additional
                        # response rather than routing it to the waiting vport.
                        if (pending_frame is not None
                                and frame == pending_frame
                                and waiting is None):
                            log.debug(f"Consumed SET echo: {_fmt(frame)}")
                            # Don't set response_event — let set_timeout expire
                            continue

                        if waiting is not None:
                            waiting.send_to_app(frame)
                        self._response_event.set()
                    else:
                        # No command in-flight — unsolicited / auto-info broadcast
                        for vp in self.vport_mgr.all_ports():
                            vp.send_to_app(frame)

            except serial.SerialException as e:
                if self._running:
                    log.error(f"Serial read error: {e}")
                    self._try_reconnect()
            except Exception as e:
                if self._running:
                    log.error(f"Unexpected serial_reader error: {e}")

    def _is_civ_echo(self, frame: bytes) -> bool:
        """
        CI-V bus echoes every byte transmitted. An echo arrives addressed
        FROM the radio TO us — i.e. frame[2] == ctrl_addr, frame[3] == radio_addr.
        A real response has frame[2] == ctrl_addr but has cmd data, not echo structure.

        Simple heuristic: if the frame we just received byte-for-byte matches
        the last frame we sent, it's an echo.
        # TODO: implement proper echo detection when testing with real IC-7100
        """
        return False  # Conservative for now — mark TODO for Icom testing

    # ------------------------------------------------------------------
    # Thread: command dispatcher
    # ------------------------------------------------------------------

    def _cmd_dispatcher(self):
        """
        Dequeue commands one at a time.
        GET → mirror first. Miss or SET → send to radio, wait for reply.
        """
        while self._running:
            try:
                cmd: QueuedCommand = self._cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            log.debug(
                f"Dispatch pri={cmd.priority} key={cmd.key!r} "
                f"get={cmd.is_get} "
                f"origin={cmd.origin.name if cmd.origin else 'internal'} "
                f"frame={_fmt(cmd.frame)}"
            )

            # ---- GET: try mirror first -----------------------------------
            if cmd.is_get and cmd.key:
                cached = self.mirror.get(cmd.key)
                if cached is not None:
                    if cmd.origin is not None:
                        # App GET — answer from mirror immediately
                        log.debug(f"Mirror hit: {cmd.key!r}")
                        cmd.origin.send_to_app(cached)
                    if not self.mirror.is_stale(cmd.key):
                        # Mirror is fresh — skip radio entirely (internal or app)
                        continue
                    # Mirror is stale — fall through to radio to refresh it

            # ---- Send to radio ------------------------------------------
            # GETs always expect a response. SETs on Yaesu are silent —
            # the radio accepts them without sending anything back.
            # Use a short timeout for SETs to avoid blocking the queue.
            is_set  = not cmd.is_get
            timeout = float(self.rig_cfg.get("response_timeout", 0.2))
            if is_set:
                timeout = float(self.rig_cfg.get("set_timeout", 0.05))

            with self._inflight_lock:
                self._pending_origin = cmd.origin
                self._pending_key    = cmd.key
                self._pending_frame  = cmd.frame
                self._inflight       = True
                self._response_event.clear()

            try:
                with self._write_lock:
                    self.serial.write(cmd.frame)
                log.debug(f"-> Radio: {_fmt(cmd.frame)}")
            except serial.SerialException as e:
                log.error(f"Serial write error: {e}")
                with self._inflight_lock:
                    self._pending_origin = None
                    self._pending_key    = None
                    self._pending_frame  = None
                    self._inflight       = False
                continue

            # Wait for serial_reader to signal a response arrived
            got = self._response_event.wait(timeout=timeout)
            if not got:
                if cmd.is_get:
                    log.warning(
                        f"Timeout ({timeout}s) waiting for response to {_fmt(cmd.frame)}"
                    )
                else:
                    log.debug(
                        f"SET accepted (no response expected): {_fmt(cmd.frame)}"
                    )

            with self._inflight_lock:
                self._pending_origin = None
                self._pending_key    = None
                self._pending_frame  = None
                self._inflight       = False

    # ------------------------------------------------------------------
    # Thread: background mirror poller
    # ------------------------------------------------------------------

    def _poller_thread(self):
        """
        Injects GET commands into the queue for any mirror key whose poll
        interval has elapsed. Runs independently of app traffic.
        """
        time.sleep(1.5)  # let radio settle after connect
        while self._running:
            sleep_for = self.poller.next_due_in()
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.05))
                continue

            for key in self.poller.due_keys():
                try:
                    frame = self.framer.make_get(key)
                except Exception:
                    self.poller.mark_sent(key)
                    continue
                # Only hit the radio if the mirror is actually stale
                if self.mirror.is_stale(key):
                    self._enqueue_internal(
                        frame    = frame,
                        is_get   = True,
                        key      = key,
                        priority = PRI_POLL,
                    )
                self.poller.mark_sent(key)

    # ------------------------------------------------------------------
    # Thread: per-VPort reader
    # ------------------------------------------------------------------

    def _vport_reader_loop(self, vp: VPort):
        """
        Drain the VPort's rx_queue (which already contains complete frames
        thanks to the per-vport framer in vport._rx_loop) and push them
        into the broker's central command queue.

        PTT commands (CAT TX, TQ, TX0/TX1 etc.) bypass the queue entirely
        and are written directly to the serial port — same as RTS PTT.
        This gives minimum latency regardless of queue depth.
        """
        while self._running:
            try:
                item: RxItem = vp.rx_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            frame  = item.frame
            is_get = self.framer.is_get(frame)
            key    = self.framer.command_key(frame)

            # --- CAT PTT: preempt any in-flight transaction --------------
            # Covers all rig families:
            #   Yaesu:    TX0; TX1; TX2;  (TX0=RX, TX1=CAT TX, TX2=MIC TX)
            #   Kenwood:  TX; RX;
            #   Elecraft: TX; RX; TQ;
            if self._is_ptt_command(key, frame, is_get):
                log.info(f"[{vp.name}] CAT PTT: {_fmt(frame)} — preempting")
                # Abort any in-flight transaction so the dispatcher releases
                # the write lock immediately rather than waiting for a response
                with self._inflight_lock:
                    if self._inflight:
                        self._response_event.set()  # unblock dispatcher now
                # Write PTT directly — no queue, no waiting
                try:
                    with self._write_lock:
                        self.serial.write(frame)
                        log.debug(f"PTT -> Radio: {_fmt(frame)}")
                    # Still need to read and route the response
                    # Enqueue as PRI_PTT so response handling stays in order
                    self._enqueue(
                        frame    = frame,
                        origin   = vp,
                        is_get   = False,
                        key      = key,
                        priority = PRI_PTT,
                    )
                except Exception as e:
                    log.warning(f"CAT PTT direct write error: {e}")
                continue

            # --- Normal command: queue with appropriate priority ----------
            if is_get:
                priority = PRI_GET_APP
            else:
                priority = PRI_SET
                # Invalidate mirror for keys affected by this SET so other
                # apps get fresh values immediately rather than stale cache.
                # e.g. WSJT-X sets FB (split freq) — logger should see new value.
                if key in self._SET_INVALIDATES:
                    for stale_key in self._SET_INVALIDATES[key]:
                        self.mirror.mark_stale(stale_key)
                        log.debug(f"Mirror invalidated '{stale_key}' after SET {key}")

            # Respect per-vport priority for normal commands
            priority = max(priority, vp.priority) if is_get else min(priority, PRI_SET)

            self._enqueue(
                frame    = frame,
                origin   = vp,
                is_get   = is_get,
                key      = key,
                priority = priority,
            )

    # PTT command keys across all supported rig families
    _PTT_KEYS = frozenset({
        "TX",   # Yaesu SET (TX0/TX1/TX2), Kenwood/Elecraft TX/RX
        "TQ",   # Elecraft TX status / PTT
        "RX",   # Kenwood/Elecraft explicit RX command
    })

    # When a SET arrives for these keys, also mark related mirror keys stale
    # so other apps polling them get fresh values from the radio immediately.
    _SET_INVALIDATES: dict[str, list[str]] = {
        "FA": ["FA", "IF"],         # VFO-A freq change
        "FB": ["FB", "IF"],         # VFO-B / split freq change
        "FT": ["FT", "IF"],         # split mode change
        "PC": ["PC"],               # power change
        "SL": ["SL", "IF"],         # filter change
        "SH": ["SH", "IF"],
    }

    def _is_ptt_command(self, key: str | None, frame: bytes, is_get: bool) -> bool:
        """
        Return True if this frame is a PTT assertion or release.
        GETs are never PTT (e.g. TX; asking for TX state is a poll, not PTT).
        """
        if is_get or key is None:
            return False
        return key in self._PTT_KEYS

    # ------------------------------------------------------------------
    # Thread: real-port signal monitor
    # ------------------------------------------------------------------

    def _signal_monitor(self):
        """
        Poll CTS/DSR/DCD on the real serial port and mirror any changes
        to all virtual ports at ~20ms.
        """
        prev = {"cts": None, "dsr": None, "dcd": None}
        while self._running:
            time.sleep(0.02)
            try:
                cts = self.serial.cts
                dsr = self.serial.dsr
                dcd = self.serial.dcd
            except Exception:
                continue

            if cts != prev["cts"]:
                prev["cts"] = cts
                self.vport_mgr.broadcast_cts(cts)
                log.debug(f"Real port CTS -> {cts}")
            if dsr != prev["dsr"]:
                prev["dsr"] = dsr
                self.vport_mgr.broadcast_dsr(dsr)
                log.debug(f"Real port DSR -> {dsr}")
            if dcd != prev["dcd"]:
                prev["dcd"] = dcd
                self.vport_mgr.broadcast_dcd(dcd)
                log.debug(f"Real port DCD -> {dcd}")

    # ------------------------------------------------------------------
    # Signal callbacks from VPorts
    # ------------------------------------------------------------------

    def _on_vport_rts(self, vp: VPort, state: bool):
        """App asserted/released RTS — route PTT according to vport config."""
        cfg = self._ptt_config.get(vp.index, {})
        if cfg.get("ptt_from", "rts") == "rts":
            log.info(f"[{vp.name}] RTS {'ON' if state else 'OFF'}")
            self._ptt_action(vp, state, cfg.get("ptt_via", "rts"))

    def _on_vport_dtr(self, vp: VPort, state: bool):
        """App asserted/released DTR — route PTT according to vport config."""
        cfg = self._ptt_config.get(vp.index, {})
        if cfg.get("ptt_from", "rts") == "dtr":
            log.info(f"[{vp.name}] DTR {'ON' if state else 'OFF'} — routed as PTT")
            self._ptt_action(vp, state, cfg.get("ptt_via", "rts"))
        else:
            # DTR not configured as PTT source — treat as CW key, forward directly
            if not isinstance(self.framer, CIVFramer):
                try:
                    self.serial.dtr = state
                    log.debug(f"[{vp.name}] DTR {'ON' if state else 'OFF'} — CW key")
                except Exception as e:
                    log.warning(f"Could not set serial DTR: {e}")
            else:
                # TODO: CI-V CW keying (command 0x17) for Icom
                log.debug("Icom CI-V CW keying via DTR not yet implemented")

    def _ptt_action(self, vp: VPort, state: bool, via: str):
        """
        Execute a PTT assertion or release toward the radio.

        via = "rts" : assert/release real port RTS directly (fastest)
        via = "dtr" : assert/release real port DTR directly
        via = "cat" : send TX1;/TX0; CAT command (Yaesu/Kenwood/Elecraft)
                      or CI-V 0x1C for Icom
        """
        if via == "cat" or isinstance(self.framer, CIVFramer):
            if isinstance(self.framer, CIVFramer):
                frame = self.framer.make_ptt_on() if state else self.framer.make_ptt_off()
            else:
                # Yaesu: TX1; = CAT PTT on, TX0; = RX
                # Kenwood/Elecraft: TX; = TX, RX; = RX
                if self.rig_family in ("kenwood", "elecraft"):
                    frame = b"TX;" if state else b"RX;"
                else:
                    frame = b"TX1;" if state else b"TX0;"
            log.info(f"[{vp.name}] PTT {'ON' if state else 'OFF'} via CAT: {_fmt(frame)}")
            # Write directly for minimum latency, queue for response handling
            try:
                with self._write_lock:
                    self.serial.write(frame)
            except Exception as e:
                log.warning(f"CAT PTT write error: {e}")

        elif via == "dtr":
            try:
                self.serial.dtr = state
                log.info(f"[{vp.name}] PTT {'ON' if state else 'OFF'} via DTR")
            except Exception as e:
                log.warning(f"Could not set serial DTR: {e}")

        else:  # rts (default)
            try:
                self.serial.rts = state
                log.info(f"[{vp.name}] PTT {'ON' if state else 'OFF'} via RTS")
            except Exception as e:
                log.warning(f"Could not set serial RTS: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enqueue(
        self,
        frame:    bytes,
        origin:   Optional[VPort],
        is_get:   bool,
        key:      Optional[str],
        priority: int,
    ):
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._cmd_queue.put(
            QueuedCommand(
                priority = priority,
                seq      = seq,
                frame    = frame,
                origin   = origin,
                is_get   = is_get,
                key      = key,
            )
        )

    def _enqueue_internal(
        self,
        frame:    bytes,
        is_get:   bool,
        key:      Optional[str],
        priority: int,
    ):
        """Enqueue a command with no originating VPort (broker-internal)."""
        self._enqueue(
            frame    = frame,
            origin   = None,
            is_get   = is_get,
            key      = key,
            priority = priority,
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "rig_family":  self.rig_family,
            "port":        self.rig_cfg["port"],
            "baud":        self.rig_cfg.get("baud", 38400),
            "connected":   self.serial is not None and self.serial.is_open,
            "queue_depth": self._cmd_queue.qsize(),
            "mirror":      self.mirror.snapshot(),
            "vports": [
                {
                    "name":     vp.name,
                    "symlink":  str(vp.symlink),
                    "rts":      vp.rts,
                    "dtr":      vp.dtr,
                    "priority": vp.priority,
                }
                for vp in self.vport_mgr.all_ports()
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(frame: bytes) -> str:
    """Format a frame for logging — ASCII if printable, hex otherwise."""
    try:
        s = frame.decode("ascii")
        if s.isprintable() or s.strip(";").isprintable():
            return repr(frame.decode("ascii"))
    except UnicodeDecodeError:
        pass
    return frame.hex(" ")
