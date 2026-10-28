#!/usr/bin/env python3
"""
SmokeShield - Raspberry Pi 3 B+ controller
==========================================
 
What it does
  * Reads PM2.5 from the PMS5003 particle sensor.
  * AUTO mode: closes the flap when PM2.5 stays high, reopens when it stays low.
  * MANUAL mode: the flap only moves when the phone says so.
  * Talks to the phone page (index.html, opened in Bluefy) over Bluetooth
    using the Nordic UART Service.
 
Run modes
  sudo python3 smokeshield.py                 normal operation
  sudo python3 smokeshield.py --sensor-test   print PM2.5 readings, nothing else
  sudo python3 smokeshield.py --servo-test 1500
                                              hold the servo at a pulse width
                                              (microseconds) to find the open
                                              and closed positions
 
Messages
  Pi -> phone : one JSON line, for example
                {"state":"open","mode":"auto","pm25":12,"smoke":false,"sensor_ok":true}
  phone -> Pi : OPEN  CLOSE  AUTO  MANUAL  STATUS
                (OPEN and CLOSE also switch to MANUAL.)
"""
 
import argparse
import json
import logging
import os
import threading
import time
 
# ======================================================================
# SETTINGS - change these, nothing below needs editing for normal use
# ======================================================================
 
DEVICE_NAME = "SmokeShield"          # name the phone sees
 
# --- Particle sensor (PMS5003) ---
# "/dev/serial0"  = sensor wired to Pi pins 8 and 10
# "/dev/ttyUSB0"  = sensor plugged in through a USB adapter
PMS_PORT = "/dev/serial0"
PMS_BAUD = 9600                      # fixed by the PMS5003
SENSOR_TIMEOUT_S = 30                # no reading for this long = sensor problem
 
# --- Smoke levels ---
# TEMPORARY TEST VALUES. They do not come from any standard.
# Replace them with your own numbers after the incense calibration test.
CLOSE_PM25 = 50       # ug/m3: close when PM2.5 is at or above this...
CLOSE_DELAY_S = 10    # ...for this many seconds in a row
CLEAR_PM25 = 20       # ug/m3: reopen when PM2.5 is below this...
CLEAR_DELAY_S = 300   # ...for this many seconds in a row (300 s = 5 min)
 
# --- Servo (hardware PWM on GPIO18 = physical pin 12) ---
# Find the right values with --servo-test before mounting the flap.
SERVO_OPEN_US = 1500      # pulse width (microseconds) for OPEN
SERVO_CLOSED_US = 2000    # pulse width (microseconds) for CLOSED
SERVO_MIN_US = 500        # safety limits: no pulse outside this range is sent.
SERVO_MAX_US = 2500       # (Your servo's exact range is not known - stay inside.)
SERVO_MOVE_S = 1.0        # how long the phone shows "moving"
SERVO_RELEASE_AFTER_MOVE = False   # True = stop pulses after a move (quieter,
                                   # but the servo no longer holds the flap)
PWM_CHIP = None           # None = use the first /sys/class/pwm/pwmchipN found
PWM_CHANNEL = 0
 
# --- Bluetooth ---
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # phone -> Pi
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # Pi -> phone
STATUS_EVERY_S = 5        # send status to the phone this often
CHUNK_BYTES = 20          # bytes per Bluetooth notification (20 always fits)
 
# ======================================================================
 
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smokeshield")
 
 
# ----------------------------------------------------------------------
# Servo: Linux hardware PWM through /sys/class/pwm
# Needs "dtoverlay=pwm,pin=18,func=2" in /boot/firmware/config.txt
# ----------------------------------------------------------------------
class Servo:
    PERIOD_NS = 20_000_000          # 50 Hz, standard for hobby servos
 
    def __init__(self):
        base = "/sys/class/pwm"
        chip = PWM_CHIP
        if chip is None:
            chips = sorted(d for d in os.listdir(base)
                           if d.startswith("pwmchip")) if os.path.isdir(base) else []
            if not chips:
                raise RuntimeError(
                    "No hardware PWM found. Add 'dtoverlay=pwm,pin=18,func=2' "
                    "to /boot/firmware/config.txt and reboot.")
            chip = chips[0]
        self.chip_dir = os.path.join(base, chip)
        self.pwm_dir = os.path.join(self.chip_dir, f"pwm{PWM_CHANNEL}")
 
        if not os.path.isdir(self.pwm_dir):
            self._write(os.path.join(self.chip_dir, "export"), PWM_CHANNEL)
            for _ in range(50):                 # wait up to 1 s for it to appear
                if os.path.isdir(self.pwm_dir):
                    break
                time.sleep(0.02)
            time.sleep(0.2)                     # let file permissions settle
 
        try:
            self._write("duty_cycle", 0)        # duty must never exceed period
        except OSError:
            pass
        self._write("period", self.PERIOD_NS)
        self.enabled = False
        log.info("Servo PWM ready: %s", self.pwm_dir)
 
    def _write(self, name, value):
        path = name if name.startswith("/") else os.path.join(self.pwm_dir, name)
        with open(path, "w") as f:
            f.write(str(value))
 
    def pulse(self, us):
        us = max(SERVO_MIN_US, min(SERVO_MAX_US, int(us)))
        self._write("duty_cycle", us * 1000)
        if not self.enabled:
            self._write("enable", 1)
            self.enabled = True
 
    def release(self):
        if self.enabled:
            self._write("enable", 0)
            self.enabled = False
 
 
# ----------------------------------------------------------------------
# PMS5003 reader: runs in its own thread and keeps the latest PM2.5
# Frame: 0x42 0x4D, length (28), 13 data words, checksum = sum of bytes 0-29
# ----------------------------------------------------------------------
class ParticleSensor(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.pm25 = None
        self.last_ok = 0.0
 
    def ok(self):
        return self.pm25 is not None and time.monotonic() - self.last_ok < SENSOR_TIMEOUT_S
 
    def run(self):
        import serial  # from the python3-serial package
        while True:
            try:
                with serial.Serial(self.port, PMS_BAUD, timeout=2) as ser:
                    log.info("Particle sensor port open: %s", self.port)
                    while True:
                        frame = self.read_frame(ser)
                        if frame:
                            # bytes 12-13 = PM2.5 "atmospheric environment" value
                            self.pm25 = (frame[12] << 8) | frame[13]
                            self.last_ok = time.monotonic()
            except Exception as e:  # unplugged, wrong port, etc.
                log.warning("Particle sensor error: %s (retrying in 5 s)", e)
                time.sleep(5)
 
    @staticmethod
    def read_frame(ser):
        if ser.read(1) != b"\x42":
            return None
        if ser.read(1) != b"\x4d":
            return None
        rest = ser.read(30)
        if len(rest) != 30:
            return None
        frame = b"\x42\x4d" + rest
        if ((frame[2] << 8) | frame[3]) != 28:
            return None
        if (sum(frame[:30]) & 0xFFFF) != ((frame[30] << 8) | frame[31]):
            return None                       # corrupted frame, skip it
        return frame
 
 
# ----------------------------------------------------------------------
# Shutter logic: modes, smoke timing, moving the servo
# ----------------------------------------------------------------------
class Shutter:
    def __init__(self, servo, sensor):
        self.servo = servo
        self.sensor = sensor
        self.closed = False
        self.mode = "auto"
        self.smoke = False
        self.above_since = None
        self.below_since = None
        self.moving_until = 0.0
        self.move_reported = True
        self.release_at = None
        self.on_change = lambda: None          # set to the phone sender later
 
    def move(self, closed, reason):
        self.closed = closed
        self.servo.pulse(SERVO_CLOSED_US if closed else SERVO_OPEN_US)
        now = time.monotonic()
        self.moving_until = now + SERVO_MOVE_S
        self.move_reported = False
        self.release_at = now + SERVO_MOVE_S if SERVO_RELEASE_AFTER_MOVE else None
        log.info("Flap -> %s (%s)", "CLOSED" if closed else "OPEN", reason)
        self.on_change()
 
    def command(self, cmd):
        cmd = cmd.strip().upper()
        log.info("Phone command: %s", cmd)
        if cmd == "OPEN":
            self.mode = "manual"
            self.move(False, "phone")
        elif cmd == "CLOSE":
            self.mode = "manual"
            self.move(True, "phone")
        elif cmd == "AUTO":
            self.mode = "auto"
            self.tick()                         # act on the current reading now
            self.on_change()
        elif cmd == "MANUAL":
            self.mode = "manual"
            self.on_change()
        elif cmd == "STATUS":
            self.on_change()
        else:
            log.warning("Unknown command ignored: %r", cmd)
 
    def tick(self):
        """Called once per second."""
        now = time.monotonic()
        pm = self.sensor.pm25 if self.sensor.ok() else None
 
        if pm is not None:
            # how long has PM2.5 been above the close level / below the clear level?
            if pm >= CLOSE_PM25:
                if self.above_since is None:
                    self.above_since = now
            else:
                self.above_since = None
            if pm < CLEAR_PM25:
                if self.below_since is None:
                    self.below_since = now
            else:
                self.below_since = None
 
            was = self.smoke
            if not self.smoke and self.above_since is not None \
                    and now - self.above_since >= CLOSE_DELAY_S:
                self.smoke = True
            elif self.smoke and self.below_since is not None \
                    and now - self.below_since >= CLEAR_DELAY_S:
                self.smoke = False
            if self.smoke != was:
                log.info("Smoke %s (PM2.5 = %s)", "DETECTED" if self.smoke else "CLEARED", pm)
                self.on_change()
        # pm is None = sensor not working: keep everything as it is
 
        if self.mode == "auto" and pm is not None and self.closed != self.smoke:
            self.move(self.smoke, "auto")
 
        if not self.move_reported and now >= self.moving_until:
            self.move_reported = True
            self.on_change()                    # phone: "moving" -> final position
 
        if self.release_at is not None and now >= self.release_at:
            self.release_at = None
            self.servo.release()
 
    def status(self):
        moving = time.monotonic() < self.moving_until
        ok = self.sensor.ok()
        return {
            "state": "moving" if moving else ("closed" if self.closed else "open"),
            "mode": self.mode,
            "pm25": self.sensor.pm25 if ok else None,
            "smoke": self.smoke,
            "sensor_ok": ok,
        }
 
 
# ----------------------------------------------------------------------
# Bluetooth link to the phone (Nordic UART Service via BlueZ)
# ----------------------------------------------------------------------
class PhoneLink:
    def __init__(self, shutter):
        self.shutter = shutter
        self.tx = None                         # set while the phone listens
 
    def on_write(self, value, options):
        text = bytes(value).decode("utf-8", "ignore")
        for line in text.splitlines():
            if line.strip():
                self.shutter.command(line)
 
    def on_notify(self, notifying, characteristic):
        self.tx = characteristic if notifying else None
        if notifying:
            log.info("Phone is listening")
            self.send_status()
 
    def on_connect(self, adapter_address, device_address):
        log.info("Phone connected: %s", device_address)
 
    def on_disconnect(self, adapter_address, device_address):
        log.info("Phone disconnected: %s", device_address)
        self.tx = None
 
    def send_status(self):
        if self.tx is None:
            return
        data = (json.dumps(self.shutter.status(), separators=(",", ":")) + "\n").encode()
        try:
            for i in range(0, len(data), CHUNK_BYTES):
                self.tx.set_value(list(data[i:i + CHUNK_BYTES]))
        except Exception as e:
            log.warning("Could not send to phone: %s", e)
 
 
# ----------------------------------------------------------------------
def run_sensor_test():
    sensor = ParticleSensor(PMS_PORT)
    sensor.start()
    print(f"Reading {PMS_PORT}. Ctrl+C to stop.")
    while True:
        time.sleep(2)
        print("PM2.5 =", sensor.pm25 if sensor.ok() else "no data yet", "ug/m3")
 
 
def run_servo_test(us):
    servo = Servo()
    servo.pulse(us)
    print(f"Holding servo at {max(SERVO_MIN_US, min(SERVO_MAX_US, us))} us. "
          "Press Enter to stop.")
    input()
    servo.release()
 
 
def run():
    from bluezero import adapter, peripheral   # imported here so the tests work without it
    from gi.repository import GLib
 
    servo = Servo()
    sensor = ParticleSensor(PMS_PORT)
    sensor.start()
    shutter = Shutter(servo, sensor)
    link = PhoneLink(shutter)
    shutter.on_change = link.send_status
    shutter.move(False, "startup")             # start open, in AUTO
 
    def every_second():
        try:
            shutter.tick()
        except Exception:
            log.exception("Error in control loop")
        return True                            # keep the timer running
 
    def every_status():
        link.send_status()
        return True
 
    dongle = next(adapter.Adapter.available())
    # Bluetooth must be powered on before advertising can start.
    # (If this fails with "Blocked", run: sudo rfkill unblock bluetooth)
    for _ in range(10):
        try:
            if not dongle.powered:
                dongle.powered = True
            if dongle.powered:
                break
        except Exception as e:
            log.warning("Bluetooth power-on failed: %s (retrying)", e)
        time.sleep(1)
    if not dongle.powered:
        raise RuntimeError("Bluetooth is off. Run: sudo rfkill unblock bluetooth")
    try:
        dongle.alias = DEVICE_NAME          # name phones show (instead of "raspberrypi")
    except Exception as e:
        log.warning("Could not rename Bluetooth adapter: %s", e)
    p = peripheral.Peripheral(dongle.address, local_name=DEVICE_NAME)
    p.add_service(srv_id=1, uuid=NUS_SERVICE, primary=True)
    p.add_characteristic(srv_id=1, chr_id=1, uuid=NUS_RX, value=[], notifying=False,
                         flags=["write", "write-without-response"],
                         write_callback=link.on_write)
    p.add_characteristic(srv_id=1, chr_id=2, uuid=NUS_TX, value=[], notifying=False,
                         flags=["notify"], notify_callback=link.on_notify)
    p.on_connect = link.on_connect
    p.on_disconnect = link.on_disconnect
 
    GLib.timeout_add(1000, every_second)
    GLib.timeout_add_seconds(STATUS_EVERY_S, every_status)
    log.info("Advertising as '%s'. Waiting for the phone...", DEVICE_NAME)
    p.publish()                                # runs forever
 
 
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SmokeShield controller")
    ap.add_argument("--sensor-test", action="store_true", help="print PM2.5 readings")
    ap.add_argument("--servo-test", type=int, metavar="US",
                    help="hold the servo at this pulse width (microseconds)")
    args = ap.parse_args()
    if args.sensor_test:
        run_sensor_test()
    elif args.servo_test is not None:
        run_servo_test(args.servo_test)
    else:
        run()
 
